"""编排层:形态选股(模块二)全市场/子集扫描。

数据流(采集/读数 → 策略 → 落库):
  沪深300 指数 K线(collectors.index)= RS 顶层基准
  + 各票日 K线(collectors.market)
  + 个股→行业 映射(collectors.board,baostock 证监会行业)
  + 基本面(collectors.fundamental:PE分位/净利增速)+ 公告标题(collectors.announcement)
  → RS 双层:
      个股 vs 板块 = 个股 win 日收益 − 同业成分等权均值收益
      板块 vs 沪深300 = 同业等权均值收益 − 沪深300 收益
    (成分/板块基准都不走被墙的东财;板块基准用「同业等权均值」合成,全A扫描时精确)
  → pattern_screener.screen.is_qualified(硬规则 AND + 负向护栏 + 正向确认)
  → market_breadth(全市场达标占比)
  → store.put_view("形态选股", ...)

负向护栏(F2.3,批次A 已接入):PE 近一年分位 >阈值 / 净利增速为负 / 近期监管类公告 → 剔除。
正向确认(A股动量弱/反转强铁律):突破不裸用,达标须叠加基本面(净利增速)或事件(增持/回购/
业绩预增等,取公告标题)确认;缺确认数据的票不计入达标(保守)。二者共用本层已取的基本面+公告。
护栏输入缺数据(采集失败/未采)不误杀,并在 view「护栏覆盖」计数、缺失诚实声明。

降级(诚实声明,写进 view「降级」,不静默):
  · 成分映射缺失(未先跑 board.fetch_membership_baostock)→ 全体降级单层(个股 vs 沪深300)。
  · 某行业在本批扫描成分数 < 板块最小样本 → 该行业个股降级单层(均值不可信)。

采集层只取数、策略层只算、本层只编排;三层解耦。
入口:`python -m tools.pipeline.screen_pattern --universe N [--date YYYY-MM-DD] [--no-fetch]`。
"""
from __future__ import annotations

import collections
import copy
import logging

from tools.analysis.pattern_screener import rs, screen as ps
from tools.collectors import announcement, board, fundamental, index, market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_pattern")

_CFG = THRESHOLDS["形态选股"]
_BENCH = "000300"        # 沪深300


def _load_or_fetch_kline(code: str, fetch: bool):
    """读本地 K线;缺失且 fetch=True 时采集。返回 df 或 None。"""
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def _benchmark_close(fetch: bool, win: int) -> list[float]:
    """沪深300 收盘序列(RS 基准)。不足/取不到抛 RuntimeError。"""
    try:
        bdf = index.load_index(_BENCH)
    except FileNotFoundError:
        bdf = index.fetch_index(["沪深300"]).get(_BENCH) if fetch else None
    if bdf is None or len(bdf) < win + 1:
        raise RuntimeError("沪深300 基准 K线不足,无法算 RS(先采集指数)")
    return bdf["close"].tolist()


def _guardrail_inputs(code: str, fetch: bool) -> dict:
    """取单票护栏输入:PE 分位 / 净利增速 / 近期公告标题。

    读本地缓存;缺失且 fetch=True 时采集。任一源失败降级为缺数据(None/[]),
    由 guardrail 决定"缺数据不误杀"。返回 {pe_percentile, 净利增速, ann_titles, 有数据}。
    """
    pe_pct = growth = None
    try:
        fund = fundamental.load_fundamental(code)
    except FileNotFoundError:
        fund = fundamental.fetch_fundamental([code]).get(code) if fetch else None
    if fund:
        pe_pct, growth = fund.get("PE分位"), fund.get("净利增速")

    try:
        anns = announcement.load_announcements(code)
    except FileNotFoundError:
        anns = announcement.fetch_announcements([code]).get(code) if fetch else None
    titles = [a.get("title", "") for a in (anns or [])]

    has_data = fund is not None or anns is not None
    return {"pe_percentile": pe_pct, "净利增速": growth,
            "ann_titles": titles, "有数据": has_data}


def run_pattern_screen(codes: list[str], as_of: str | None = None,
                       fetch: bool = True) -> dict:
    """扫描 codes,落 view「形态选股」。返回 summary(达标池 + 达标占比 + 降级声明)。

    fetch=True:缺 K线/基准自动采集;False:只读本地缓存(离线复算,不触网)。
    RS 双层(Config 启用板块层)时:板块基准 = 同业成分等权均值收益;成分缺失或行业样本
    不足时逐票降级单层(个股 vs 沪深300)。
    """
    if as_of:
        store.set_active_date(as_of)
    win = int(_CFG["RS"]["窗口"])
    min_n = int(_CFG["RS"].get("板块最小样本", 3))
    two_layer = bool(_CFG["RS"].get("启用板块层", False))
    bench_close = _benchmark_close(fetch, win)
    hs300_ret = rs.period_return(bench_close, win)

    membership = {}
    if two_layer:
        try:
            membership = board.load_membership()
        except FileNotFoundError:
            logger.warning("双层已开但成分映射缺失(先跑 collectors.board.fetch_membership_baostock),"
                           "本次全体降级单层")
    cfg_single = copy.deepcopy(_CFG)
    cfg_single["RS"]["启用板块层"] = False        # 逐票降级用

    # —— pass 1:K线 + 个股 win 日收益 + 所属行业 + 护栏输入 ——
    loaded: dict[str, tuple] = {}
    skipped = 0
    guard_covered = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < win + 1:
            skipped += 1
            logger.warning("%s K线不足(<%d),跳过", code, win + 1)
            continue
        sret = rs.period_return(kdf["close"].tolist(), win)
        grd = _guardrail_inputs(code, fetch)
        if grd["有数据"]:
            guard_covered += 1
        loaded[code] = (kdf, sret, membership.get(code), grd)

    # —— 合成板块基准:同业成分等权均值收益(仅样本达标行业)——
    by_board: dict[str, list[float]] = collections.defaultdict(list)
    for _c, (_k, sret, ind, _g) in loaded.items():
        if ind:
            by_board[ind].append(sret)
    board_mean = {b: sum(v) / len(v) for b, v in by_board.items() if len(v) >= min_n}

    # —— pass 2:双层 / 逐票降级单层;护栏输入喂 is_qualified ——
    results: dict[str, dict] = {}
    degraded = 0
    for code, (kdf, sret, ind, grd) in loaded.items():
        if two_layer and ind in board_mean:
            bmean = board_mean[ind]
            results[code] = ps.is_qualified(
                kdf, rs_stock_vs_board=round(sret - bmean, 4),
                rs_board_vs_hs300=round(bmean - hs300_ret, 4),
                pe_percentile=grd["pe_percentile"], net_profit_growth=grd["净利增速"],
                ann_titles=grd["ann_titles"])
        else:
            if two_layer:
                degraded += 1
            results[code] = ps.is_qualified(
                kdf, rs_stock_vs_board=round(sret - hs300_ret, 4),
                rs_board_vs_hs300=None,
                pe_percentile=grd["pe_percentile"], net_profit_growth=grd["净利增速"],
                ann_titles=grd["ann_titles"], cfg=cfg_single)   # 单层:个股 vs 沪深300

    breadth = ps.market_breadth(results)
    # RS模式反映**实际**:双层已开且真用到板块→双层;否则(未开/成分缺/全样本不足)→等效单层
    if two_layer and board_mean:
        rs_mode = "双层(同业等权均值)"
        rs_note = ("双层:成分=baostock证监会行业,板块基准=同业等权均值;"
                   f"{degraded} 票因无行业/样本不足降级单层")
    elif two_layer:
        rs_mode = "单层(降级:成分缺失或全部行业样本不足)"
        rs_note = "双层已开但无可用板块基准(成分缺失/样本不足),全体降级单层(个股 vs 沪深300)"
    else:
        rs_mode = "单层(个股vs沪深300)"
        rs_note = "配置为单层(个股 vs 沪深300)"
    view = {
        "as_of": as_of,
        "扫描数": len(codes), "跳过数": skipped,
        "有效样本": breadth["有效样本"], "达标数": breadth["达标数"],
        "达标占比": breadth["达标占比"],
        "达标清单": [{"code": c, "命中形态": results[c]["命中形态"],
                      "正向确认依据": results[c].get("正向确认依据", [])}
                     for c in breadth["达标清单"]],
        "RS模式": rs_mode, "板块数": len(board_mean), "单层降级票数": degraded,
        "护栏覆盖": f"{guard_covered}/{len(loaded)}",
        "纪律": "突破不裸用:达标须叠加基本面或事件正向确认(A股动量弱/反转强)",
        "降级": {
            "RS": rs_note,
            "护栏": ("已接入(PE近一年分位/净利增速/监管类公告);"
                     f"{len(loaded) - guard_covered} 票无基本面或公告数据→该票护栏缺数据不误杀"),
            "正向确认": ("已接入(基本面净利增速或事件:增持/回购/业绩预增等,取公告标题);"
                         "缺确认数据的票视为未确认→不计入达标(保守,不裸用)。"
                         "事件源后续可升级 stock_yjyg_em/stock_ggcg_em"),
        },
    }
    p = store.put_view("形态选股", view)
    logger.info("形态选股:扫描 %d / 有效 %d / 达标 %d(占比 %.2f%%)/ 板块 %d / 降级 %d / 护栏覆盖 %d → %s",
                len(codes), breadth["有效样本"], breadth["达标数"],
                breadth["达标占比"] * 100, len(board_mean), degraded, guard_covered, p)
    return view


def _main(argv: list[str] | None = None) -> int:
    """独立入口:python -m tools.pipeline.screen_pattern --universe N [--date D] [--no-fetch]。"""
    import argparse

    import pandas as pd

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="形态选股扫描")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        codes = universe.universe_codes(limit=a.universe)
    logger.info("形态选股扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_pattern_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:达标 %d / 有效 %d(占比 %.2f%%)| RS模式 %s | 护栏覆盖 %s",
                v["达标数"], v["有效样本"], v["达标占比"] * 100, v["RS模式"], v["护栏覆盖"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
