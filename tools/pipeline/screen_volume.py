"""策略 S04「量价放量」入场 Screener(3 个可勾选布尔子信号,共享指标)。

定位=放量突破/上涨型,与 S02「放量后缩量回踩」**互补**(S02 买放量*之后*的缩量回踩;
本策略买放量突破/上涨*本身*),故并存不去重。命中任一子信号即入选,`组合` 记命中哪几个;
/selection 面板给 3 个子信号勾选框做**并集过滤**(复用 combined-section,非 council 投票)。

子信号(参数全读 THRESHOLDS["量价放量"];当日 = 第 t 根,均前复权 OHLCV):
  · 单日放量:turnover(t) > 倍数×turnover(t-1) ∧ C(t) > C(t-1)×(1+涨幅) ∧ MA长上行 ∧ MA快>MA慢
      —— 换手取自 master `turnover` 列(免费源 baostock/akshare-spot 已填;见方案 J4);
         该列缺失(NA)则本子信号对该票**不适用**(跳过,不误选)。
  · 低位放量:C > 全部日均线 ∧ 上穿30周线(周线动态、无未来函数)∧ V(t)=近N根最大量
  · 连续放量:连续两日走高 ∧ 较前两日各涨>阈值 ∧ V(t)>V(t-1) ∧ C>日均线 ∧ MA5,MA10>MA20

防未来函数:只用 t 及之前;周线聚合按 ISO 自然周,MA30 用"当周截至今收 + 此前完整周",
  不引入 t 之后交易日。历史 < 最少历史根数 不选。⚠️ 非投资建议。
"""
from __future__ import annotations

import logging
from collections import OrderedDict

import pandas as pd

from tools.analysis.trend_template import indicators as ind
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_volume")

_CFG = THRESHOLDS["量价放量"]
_SUBS = ("单日放量", "低位放量", "连续放量")


def min_history() -> int:
    return int(_CFG["最少历史根数"])


def _weekly_ma(kdf: pd.DataFrame, t: int, n: int) -> float | None:
    """截至第 t 根(含)的 n 周周线 MA(周收=各 ISO 自然周的**最后一根收盘**;
    当周用截至第 t 根的今收)。不足 n 周 → None。只用 [0, t](防未来函数)。"""
    if t < 0 or t >= len(kdf):
        return None
    dates = pd.to_datetime(kdf["date"].iloc[: t + 1])
    closes = kdf["close"].iloc[: t + 1].to_numpy(dtype=float)
    iso = dates.dt.isocalendar()
    keys = list(zip(iso["year"].tolist(), iso["week"].tolist()))
    wk: "OrderedDict[tuple, float]" = OrderedDict()
    for k, c in zip(keys, closes):
        wk[k] = float(c)                 # 保序覆盖 → 每周最后一根收盘;末项=当周今收
    weekly = list(wk.values())
    if len(weekly) < n:
        return None
    return sum(weekly[-n:]) / n


def _single(kdf, t, close, vol, c) -> tuple[bool, bool, dict]:
    """单日放量。返回 (适用, 命中, 明细)。换手 NA → 不适用,但**明确标降级**(不静默)。"""
    cs = c["单日放量"]
    turn = kdf["turnover"].to_numpy(dtype=float)
    tv, tv_prev = turn[t], turn[t - 1]
    if not (pd.notna(tv) and pd.notna(tv_prev) and tv_prev > 0):
        # 有声降级:换手缺失时本子信号无从判定。绝不静默返回"不适用"——那会让整批
        # 「单日放量」哑火而无告警(#20:94/94 全 换手=None、命中 0)。由 run_volume_screen
        # 汇总降级票数并 warning;近端缺失先跑 ops.backfill_turnover 回填。
        return False, False, {"换手": None, "降级": "换手缺失(turnover NaN)"}
    ma_up = int(cs["MA上行周期"])
    ma_up_t, ma_up_p = ind.ma(close, t, ma_up), ind.ma(close, t - 1, ma_up)
    ma_fast, ma_slow = ind.ma(close, t, int(cs["MA快"])), ind.ma(close, t, int(cs["MA慢"]))
    if None in (ma_up_t, ma_up_p, ma_fast, ma_slow):
        return True, False, {"换手": round(float(tv), 3), "原因": "均线历史不足"}
    hit = (tv > float(cs["换手放大倍数"]) * tv_prev
           and close[t] > close[t - 1] * (1 + float(cs["涨幅阈值"]))
           and ma_up_t > ma_up_p and ma_fast > ma_slow)
    return True, bool(hit), {
        "换手": round(float(tv), 3), "换手前值": round(float(tv_prev), 3),
        f"MA{ma_up}上行": bool(ma_up_t > ma_up_p),
        f"MA{int(cs['MA快'])}>MA{int(cs['MA慢'])}": bool(ma_fast > ma_slow),
    }


def _low(kdf, t, close, vol, c) -> tuple[bool, bool, dict]:
    """低位放量。返回 (适用, 命中, 明细)。"""
    cs = c["低位放量"]
    above = True
    for p in cs["日均线"]:
        m = ind.ma(close, t, int(p))
        if m is None or not (close[t] > m):
            above = False
            break
    wma_t = _weekly_ma(kdf, t, int(cs["周线周期"]))
    wma_p = _weekly_ma(kdf, t - 1, int(cs["周线周期"]))
    cross = (wma_t is not None and wma_p is not None
             and close[t] > wma_t and close[t - 1] <= wma_p)
    mw = int(cs["最大量窗口"])
    start = max(0, t - mw + 1)
    max_vol = vol[t] >= max(vol[start: t + 1])
    hit = bool(above and cross and max_vol)
    return True, hit, {"站上全部日均线": bool(above), "上穿30周线": bool(cross),
                       f"近{mw}日最大量": bool(max_vol)}


def _continuous(kdf, t, close, vol, c) -> tuple[bool, bool, dict]:
    """连续放量。返回 (适用, 命中, 明细)。"""
    cs = c["连续放量"]
    if t < 2:
        return True, False, {"原因": "历史不足"}
    p1, p2 = close[t - 1], close[t - 2]
    thr = float(cs["涨幅阈值"])
    rising = close[t] > p1 and close[t] > p2
    up2 = (p1 > 0 and p2 > 0 and (close[t] / p1 - 1) > thr and (close[t] / p2 - 1) > thr)
    vol_up = vol[t] > vol[t - 1]
    above = True
    for p in cs["日均线上"]:
        m = ind.ma(close, t, int(p))
        if m is None or not (close[t] > m):
            above = False
            break
    base = ind.ma(close, t, int(cs["快线基准"]))
    fast_ok = base is not None and all(
        (ind.ma(close, t, int(f)) is not None and ind.ma(close, t, int(f)) > base)
        for f in cs["快线"])
    hit = bool(rising and up2 and vol_up and above and fast_ok)
    return True, hit, {"连续走高": bool(rising), "较前两日各涨": bool(up2),
                       "量递增": bool(vol_up), "站上日均线": bool(above),
                       "MA5_10在MA20上": bool(fast_ok)}


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None) -> dict:
    """判第 t 根命中哪些子信号。返回 {SELECT, 组合:[命中子信号], 子信号:{...}, 明细}。

    SELECT = 命中任一子信号。历史不足 → SELECT=False + 原因。只用 t 及之前(防未来函数)。
    """
    c = cfg or _CFG
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "原因": "索引越界"}
    need = int(c["最少历史根数"])
    if t + 1 < need or t < 1:
        return {"SELECT": False, "原因": f"历史不足({t + 1}<{need})"}

    close = kdf["close"].to_numpy(dtype=float)
    vol = kdf["volume"].to_numpy(dtype=float)

    hits: list[str] = []
    detail: dict = {}
    subflags: dict = {}
    for name, fn in (("单日放量", _single), ("低位放量", _low), ("连续放量", _continuous)):
        applicable, hit, d = fn(kdf, t, close, vol, c)
        subflags[name] = bool(hit)
        detail[name] = d
        if applicable and hit:
            hits.append(name)
    return {"SELECT": bool(hits), "组合": hits, "子信号": subflags,
            "明细": {"命中": hits, "close": round(float(close[t]), 4), **detail}}


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, cfg)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_volume_screen(codes: list[str], as_of: str | None = None,
                      fetch: bool = True) -> dict:
    """扫描 codes,判每票最后一根命中哪些子信号,落 view「量价放量」。返回 summary。

    命中任一子信号即入选;`入选清单[].组合` 记命中的子信号名,供面板勾选并集过滤。
    """
    if as_of:
        store.set_active_date(as_of)
    need = min_history()
    selected: list[dict] = []
    scanned = skipped = 0
    single_degraded = 0                       # 「单日放量」因换手缺失而无法判定的票数(有声降级)
    counts = {s: 0 for s in _SUBS}
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = screen_latest(kdf)
        if r.get("明细", {}).get("单日放量", {}).get("降级"):
            single_degraded += 1
        if r.get("SELECT"):
            for s in r["组合"]:
                counts[s] += 1
            selected.append({"code": code, "组合": r["组合"], "明细": r["明细"]})

    view = {
        "as_of": as_of,
        "策略": "量价放量(S04)",
        "方向": "看多",
        "子信号": list(_SUBS),
        "子信号命中数": counts,
        "单日放量降级数(换手缺失)": single_degraded,
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": ("命中任一子信号即入选:单日放量(换手>1.7×前值∧涨>3%∧MA200上行∧MA50>MA200)/ "
                 "低位放量(站上MA5/10/20/30/200∧上穿30周线∧近10日最大量)/ "
                 "连续放量(连续走高∧较前两日各涨>4%∧量递增∧站上MA20/50/200∧MA5,MA10>MA20)"),
        "防未来函数": "只用 t 及之前;周线按 ISO 自然周、当周用今收;日线<201 不选",
    }
    p = store.put_view("量价放量", view)
    logger.info("量价放量:扫描 %d / 有效 %d / 跳过 %d / 入选 %d(单日 %d 低位 %d 连续 %d)→ %s",
                len(codes), scanned, skipped, len(selected),
                counts["单日放量"], counts["低位放量"], counts["连续放量"], p)
    if single_degraded:
        # 有声降级:换手缺失让「单日放量」子信号整批哑火,必须告警而非静默(#20)
        lvl = logger.error if scanned and single_degraded >= scanned * 0.5 else logger.warning
        lvl("量价放量:「单日放量」%d/%d 只因换手缺失无法判定(子信号哑火);"
            "近端 turnover 缺失请先跑 ops.backfill_turnover 回填", single_degraded, scanned)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S04 量价放量 入场扫描(3 子信号)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    else:
        codes = universe.universe_codes(limit=a.universe)
    logger.info("S04 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_volume_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
