"""策略 S03「最大范围选股」入场 Screener(规则型布尔组合,纯 OHLC)。

看多型:高位强势 + 均线多头 + 近期有过大阳 + 当日未大跌。当日盘后逐票,单日触发,
SELECT = 全条件 AND(见下)。均前复权 OHLCV,当日 = 第 t 根。**不依赖 Tushare**。

规格(参数全读 THRESHOLDS["最大范围选股"]):
  ① 距高点:C/HHV(C,N) ≥ 距高点下限 且 C ≤ HHV(C,N)×距高点上限(HHV 含当日)
  ② 均线多头:C > MA(各周期)(默认 MA10/20/50)
  ③ 近期大阳:COUNT(C > REF(C,1)×(1+单日上涨阈值), 大阳窗口) ≥ 大阳次数下限
  ④ 当日回撤:(REF(C,1)/C − 1) ≤ 最大单日回撤(当日相对昨收跌幅上限)
  ⑤ 非北交所:code 前缀 ∉ 排除前缀(等价 TDX FINANCE(3)!=2;保留 002)
  ⑥ C > LOW(非最低收盘)

防未来函数:所有量只用 t 及之前;HHV/MA 窗口末位=t;历史 < 最少历史根数 不选。
数据只读复用 collectors.market.load_kline_recent;指标复用 analysis.trend_template.indicators。
入口:python -m tools.pipeline.screen_max_range [--codes ...|--universe N] [--date D] [--no-fetch]
⚠️ 非投资建议。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.trend_template import indicators as ind
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_max_range")

_CFG = THRESHOLDS["最大范围选股"]


def min_history() -> int:
    """入场判定所需最少日线根数(供 HHV(高点窗口) + MA(max 均线周期))。"""
    return int(_CFG["最少历史根数"])


def _is_excluded_prefix(code: str | None, prefixes) -> bool:
    """北交所(排除前缀)判定:code 以任一排除前缀开头 → True(应排除)。code=None → False。"""
    if not code:
        return False
    return any(str(code).startswith(p) for p in prefixes)


def signal_at(kdf: pd.DataFrame, t: int, code: str | None = None,
              cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否入选,返回逐条布尔明细 + SELECT。

    历史不足 / 索引越界 → SELECT=False + 原因。只用 t 及之前的数据(防未来函数)。
    code 传入则做⑤非北交所判定;不传(纯 K 线单测)视作通过⑤。
    """
    c = cfg or _CFG
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "原因": "索引越界"}
    win = int(c["高点窗口"])
    need = max(win, int(c["最少历史根数"]), max(int(p) for p in c["均线周期"]))
    if t + 1 < need or t < 1:
        return {"SELECT": False, "原因": f"历史不足({t + 1}<{need})"}

    close = kdf["close"].to_numpy(dtype=float)
    low = kdf["low"].to_numpy(dtype=float)

    # ① 距高点(HHV 含当日)
    hhv = ind.highest_high(close, t, win)
    if hhv is None or hhv <= 0:
        return {"SELECT": False, "原因": "HHV 不可用"}
    ratio = close[t] / hhv
    c1 = (ratio >= float(c["距高点下限"])) and (close[t] <= hhv * float(c["距高点上限"]))

    # ② 均线多头:C > 各均线
    mas = {}
    c2 = True
    for p in c["均线周期"]:
        m = ind.ma(close, t, int(p))
        mas[int(p)] = m
        if m is None or not (close[t] > m):
            c2 = False

    # ③ 近期大阳:窗口内单日涨幅 > 阈值 的次数 ≥ 下限
    up_thr = 1.0 + float(c["单日上涨阈值"])
    bw = int(c["大阳窗口"])
    start = max(1, t - bw + 1)
    surge = sum(1 for i in range(start, t + 1) if close[i - 1] > 0 and close[i] > close[i - 1] * up_thr)
    c3 = surge >= int(c["大阳次数下限"])

    # ④ 当日回撤:(REF(C,1)/C − 1) ≤ 最大单日回撤
    retrace = (close[t - 1] / close[t] - 1.0) if close[t] > 0 else float("inf")
    c4 = retrace <= float(c["最大单日回撤"])

    # ⑤ 非北交所
    c5 = not _is_excluded_prefix(code, c["排除前缀"])

    # ⑥ C > LOW
    c6 = close[t] > low[t]

    select = bool(c1 and c2 and c3 and c4 and c5 and c6)
    return {
        "SELECT": select,
        "C1_距250日高": bool(c1), "C2_均线多头": bool(c2), "C3_32日大阳": bool(c3),
        "C4_当日回撤": bool(c4), "C5_非北交所": bool(c5), "C6_收盘高于最低": bool(c6),
        "明细": {
            "距250日高点%": round(ratio * 100, 2),
            "HHV250": round(float(hhv), 4),
            "close": round(float(close[t]), 4), "low": round(float(low[t]), 4),
            "32日涨超6%次数": int(surge),
            "当日回撤%": round(retrace * 100, 2),
            **{f"MA{p}": (round(m, 4) if m is not None else None) for p, m in mas.items()},
        },
    }


def screen_latest(kdf: pd.DataFrame, code: str | None = None, cfg: dict | None = None) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, code=code, cfg=cfg)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_max_range_screen(codes: list[str], as_of: str | None = None,
                         fetch: bool = True) -> dict:
    """扫描 codes,判每票最后一根是否入选,落 view「最大范围选股」。返回 summary。"""
    if as_of:
        store.set_active_date(as_of)
    need = min_history()
    selected: list[dict] = []
    scanned = skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = screen_latest(kdf, code=code)
        if r.get("SELECT"):
            selected.append({"code": code, "明细": r["明细"]})

    view = {
        "as_of": as_of,
        "策略": "最大范围选股(S03)",
        "方向": "看多",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": ("C/HHV(C,250)≥0.82 且 C≤HHV×1.10 AND C>MA10/MA20/MA50 AND "
                 "COUNT(C>REF(C,1)×1.06,32)≥1 AND (REF(C,1)/C−1)≤0.04 AND 非北交所 AND C>LOW"),
        "防未来函数": "只用 t 及之前;HHV/MA 窗口末位=t;日线<250 不选",
    }
    p = store.put_view("最大范围选股", view)
    logger.info("最大范围选股:扫描 %d / 有效 %d / 跳过 %d / 入选 %d → %s",
                len(codes), scanned, skipped, len(selected), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S03 最大范围选股 入场扫描")
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
    logger.info("S03 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_max_range_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
