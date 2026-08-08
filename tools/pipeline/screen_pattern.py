"""编排层:形态选股(模块二)全市场/子集扫描。

两阶段采集(全A 可扩展前提:先便宜筛、再只对候选做贵活):
  阶段①便宜筛(可全A,只碰 K线):全A 逐票只采/读 K线 → 算 形态+双层RS+量能 → 得候选;
        护栏/正向确认所需的基本面+公告**此阶段不采**(对全 5539 只提前全采是耗时真凶)。
  阶段②贵活(只对候选,通常几百只):只对便宜门选出的候选采基本面+公告 →
        负向护栏 + 正向确认 → 达标池。K线/基本面/公告均 load-first(skip-if-cached),重跑不重采。

数据流(采集/读数 → 策略 → 落库):
  沪深300 指数 K线(collectors.index)= RS 顶层基准
  + 各票日 K线(collectors.market)                          ← 阶段①全A
  + 个股→行业 映射(collectors.board,baostock 证监会行业)
  + 基本面(collectors.fundamental:PE分位/净利增速)+ 公告标题(collectors.announcement)← 阶段②仅候选
  → RS 双层:
      个股 vs 板块 = 个股 win 日收益 − 同业成分等权均值收益
      板块 vs 沪深300 = 同业等权均值收益 − 沪深300 收益
    (成分/板块基准都不走被墙的东财;板块基准用「同业等权均值」合成,全A扫描时精确)
  → 便宜门(形态+RS+量能)筛候选 → pattern_screener.screen.is_qualified(硬规则 AND + 负向护栏 + 正向确认)
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

from tools.analysis import industry_map
from tools.analysis.pattern_screener import pattern, rs, screen as ps
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


def _sector(code: str, membership: dict) -> str:
    """达标票所属板块:证监会门类(baostock membership)→ 申万一级(industry_map),
    对齐不上则回退证监会门类名,再拿不到 → 「未分类」(供选股页 region② 按板块分组)。"""
    zjh = membership.get(code)
    if not zjh:
        return "未分类"
    return industry_map.to_sw(zjh) or zjh


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


def _cheap_gate(kdf, rs_stock_vs_board: float, rs_board_vs_hs300: float | None,
                cfg: dict) -> tuple[bool, dict]:
    """便宜门(阶段①,可全A):只用 K线判 形态+RS+量能——三者皆过才是候选。

    护栏/正向确认(需基本面+公告)留到候选阶段再采数据,避免对全A提前全采。
    RS 双/单层口径与 is_qualified 完全一致(同一 cfg),保证便宜门与最终判定不背离。
    返回 (是否候选, pattern.detect 结果)。
    """
    pat = pattern.detect(kdf, cfg)
    if cfg["RS"].get("启用板块层", True):
        rs_ok = rs.is_strong(rs_stock_vs_board, "个股vs板块") and \
            rs.is_strong(rs_board_vs_hs300, "板块vs沪深300")
    else:
        rs_ok = rs.is_strong(rs_stock_vs_board, "个股vs板块")   # 单层:个股 vs 沪深300
    return bool(pat["达标"] and rs_ok and ps.volume_ok(kdf, cfg)), pat


# —— 接近达标(达标0降级数据:平盘日区块②不空页)——
# 每板块保留的「接近达标」条数(按合议分/接近度 top-N);pipeline 阶段会用合议分重排。
_NEAR_TOP_PER_SECTOR = 3


def _pattern_gap(name: str, feat: dict) -> tuple[str, int] | None:
    """某形态未达标时,判断其结构是否「已成、只差突破/放量」→ 返回 (差距说明, 接近度) 或 None。

    接近度越大越接近达标(结构成型且仅差临门一脚的放量确认最高)。仅用 K线形态明细,不触网。
    """
    if name == "箱体" and feat.get("窄幅"):
        if not feat.get("突破"):
            return "箱体窄幅已成,待向上突破箱顶", 2
        if not feat.get("放量"):
            return "箱体已突破箱顶,待放量确认", 3
    if name == "楔形" and feat.get("收敛"):
        if not feat.get("突破"):
            return "楔形收敛已成,待向上突破", 2
        if not feat.get("放量"):
            return "楔形已突破,待放量确认", 3
    if name == "杯柄" and feat.get("回补"):
        if not feat.get("突破"):
            return "杯柄杯体回补,待突破左沿", 2
        if not feat.get("放量"):
            return "杯柄已突破左沿,待放量确认", 3
    if name == "旗形":
        if feat.get("旗杆") and not feat.get("旗面"):
            return "旗杆急涨已成,待旗面横盘收敛", 1
        if feat.get("旗面") and not feat.get("旗杆"):
            return "旗面横盘已成,待旗杆动能确认", 1
    return None


def _near_miss(code: str, membership: dict, pat: dict,
               cand_result: dict | None) -> dict | None:
    """票未达标时判定是否「接近达标」,是则返回条目(合议分留 None,pipeline 后填),否则 None。

    两类接近(取更接近者):
      · 达标接近:形态+突破+RS 均命中(过便宜门)但护栏/正向确认未过(cand_result 提供各项);
      · 形态接近:某形态结构已成、只差突破/放量(_pattern_gap,不需基本面/公告)。
    """
    # 达标接近(候选票:已过便宜门,仅差护栏/正向确认这道门)
    if cand_result is not None and pat["达标"] and not cand_result.get("达标"):
        items = cand_result.get("各项", {})
        if items.get("形态") and items.get("RS") and items.get("量能"):
            if not items.get("正向确认"):
                gap, score = "形态+突破+RS 已成,待基本面或事件正向确认", 5
            elif not items.get("护栏"):
                gap, score = "形态+突破+RS 已成,受负向护栏压制(高估/业绩/合规)", 4
            else:
                gap, score = "接近达标", 4
            return {"code": code, "行业": _sector(code, membership),
                    "最接近形态": pat["命中形态"], "差距说明": gap,
                    "合议分": None, "_接近度": score}
    # 形态接近(结构已成、待突破/放量;RS 是否达标不影响"形态接近"的展示价值)
    best: tuple[str, str, int] | None = None
    for name, r in pat.get("明细", {}).items():
        if r.get("达标"):
            continue
        g = _pattern_gap(name, r.get("特征", {}))
        if g and (best is None or g[1] > best[2]):
            best = (name, g[0], g[1])
    if best:
        return {"code": code, "行业": _sector(code, membership),
                "最接近形态": [best[0]], "差距说明": best[1],
                "合议分": None, "_接近度": best[2]}
    return None


def _group_near_miss(near: list[dict], top: int = _NEAR_TOP_PER_SECTOR) -> dict[str, list[dict]]:
    """接近达标按板块分组,每板块按 (合议分, 接近度) 降序取 top;剔除内部排序键 _接近度。

    合议分为 None(screen 独立跑、pipeline 未回填)时退化为按接近度排序。恒返回(可能为空 dict)。
    """
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for x in near:
        by[x["行业"]].append(x)

    def key(x):
        return (x.get("合议分") if x.get("合议分") is not None else -1.0, x.get("_接近度", 0))

    out: dict[str, list[dict]] = {}
    for sector, xs in by.items():
        ranked = sorted(xs, key=key, reverse=True)[:top]
        out[sector] = [{k: v for k, v in x.items() if k != "_接近度"} for x in ranked]
    return out


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

    # 成分映射(baostock 证监会门类):RS 双层用它,达标清单「行业」也用它(全A达标票无中心记录,
    # 靠这张图给板块归属)。故无论单/双层都尝试加载;缺失且双层 → 降级单层告警。
    try:
        membership = board.load_membership()
    except FileNotFoundError:
        membership = {}
        if two_layer:
            logger.warning("双层已开但成分映射缺失(先跑 collectors.board.fetch_membership_baostock),"
                           "本次全体降级单层;达标清单行业将落「未分类」")
    cfg_single = copy.deepcopy(_CFG)
    cfg_single["RS"]["启用板块层"] = False        # 逐票降级用

    # —— pass 1(阶段①便宜筛,可全A):只采/读 K线 + 个股 win 日收益 + 所属行业;不碰基本面/公告 ——
    loaded: dict[str, tuple] = {}
    skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < win + 1:
            skipped += 1
            logger.warning("%s K线不足(<%d),跳过", code, win + 1)
            continue
        sret = rs.period_return(kdf["close"].tolist(), win)
        loaded[code] = (kdf, sret, membership.get(code))

    # —— 合成板块基准:同业成分等权均值收益(仅样本达标行业)——
    by_board: dict[str, list[float]] = collections.defaultdict(list)
    for _c, (_k, sret, ind) in loaded.items():
        if ind:
            by_board[ind].append(sret)
    board_mean = {b: sum(v) / len(v) for b, v in by_board.items() if len(v) >= min_n}

    # —— pass 2(两阶段):便宜门(形态+RS+量能)筛候选 → 只对候选采护栏/正向确认数据 → is_qualified ——
    results: dict[str, dict] = {}
    degraded = 0
    candidates: list[str] = []
    guard_covered = 0
    near: list[dict] = []            # 接近达标(达标0降级数据):形态匹配但突破/正向确认门未过
    for code, (kdf, sret, ind) in loaded.items():
        if two_layer and ind in board_mean:
            bmean = board_mean[ind]
            rs_sb, rs_bh, cfg = round(sret - bmean, 4), round(bmean - hs300_ret, 4), _CFG
        else:
            if two_layer:
                degraded += 1
            rs_sb, rs_bh, cfg = round(sret - hs300_ret, 4), None, cfg_single  # 单层:个股 vs 沪深300
        passed, pat = _cheap_gate(kdf, rs_sb, rs_bh, cfg)
        if not passed:
            results[code] = {"达标": False, "命中形态": pat["命中形态"]}   # 便宜门淘汰,不采贵数据
            nm = _near_miss(code, membership, pat, None)                 # 形态接近(结构成型待突破)
            if nm:
                near.append(nm)
            continue
        candidates.append(code)
        grd = _guardrail_inputs(code, fetch)          # 阶段②:只对候选采基本面/公告
        if grd["有数据"]:
            guard_covered += 1
        r = ps.is_qualified(
            kdf, rs_stock_vs_board=rs_sb, rs_board_vs_hs300=rs_bh,
            pe_percentile=grd["pe_percentile"], net_profit_growth=grd["净利增速"],
            ann_titles=grd["ann_titles"], cfg=cfg)
        results[code] = r
        if not r.get("达标"):
            nm = _near_miss(code, membership, pat, r)                    # 达标接近(仅差护栏/正向确认)
            if nm:
                near.append(nm)

    breadth = ps.market_breadth(results)
    near_by_sector = _group_near_miss(near)          # 按板块 top-N(合议分待 pipeline 回填后重排)
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
        "达标清单": [{"code": c, "行业": _sector(c, membership),
                      "命中形态": results[c]["命中形态"],
                      "正向确认依据": results[c].get("正向确认依据", [])}
                     for c in breadth["达标清单"]],
        "RS模式": rs_mode, "板块数": len(board_mean), "单层降级票数": degraded,
        "候选数": len(candidates),
        # 接近达标(达标0降级数据):形态匹配但突破/正向确认门未过,按板块 top3,恒输出(达标>0 也给)。
        # 平盘日达标0时,前端区块②靠此有内容可展示。合议分此刻为 None,pipeline 阶段回填并按合议分重排。
        "接近达标": near_by_sector,
        "接近达标数": len(near),
        "护栏覆盖": f"{guard_covered}/{len(candidates)}",
        "采集": (f"两阶段:阶段①全A只采K线({len(loaded)}只有效)→便宜门筛候选({len(candidates)}只)"
                 "→阶段②只对候选采基本面/公告(护栏+正向确认)。K线/基本面/公告均skip-if-cached"),
        "纪律": "突破不裸用:达标须叠加基本面或事件正向确认(A股动量弱/反转强)",
        "降级": {
            "RS": rs_note,
            "护栏": ("已接入(PE近一年分位/净利增速/监管类公告),仅对候选采;"
                     f"{len(candidates) - guard_covered} 只候选无基本面或公告数据→该票护栏缺数据不误杀"),
            "正向确认": ("已接入(基本面净利增速或事件:增持/回购/业绩预增等,取公告标题);"
                         "缺确认数据的票视为未确认→不计入达标(保守,不裸用)。"
                         "事件源后续可升级 stock_yjyg_em/stock_ggcg_em"),
        },
    }
    p = store.put_view("形态选股", view)
    logger.info("形态选股:扫描 %d / 有效 %d / 候选 %d / 达标 %d(占比 %.2f%%)/ 接近达标 %d(%d板块)/ 板块 %d / 降级 %d / 护栏覆盖 %d → %s",
                len(codes), breadth["有效样本"], len(candidates), breadth["达标数"],
                breadth["达标占比"] * 100, len(near), len(near_by_sector),
                len(board_mean), degraded, guard_covered, p)
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
