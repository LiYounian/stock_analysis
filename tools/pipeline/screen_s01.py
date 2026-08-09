"""策略 S01「趋势深跌反包」入场 Screener(规则型布尔组合)。

区别于「形态选股」的合议专家路线:本策略是**独立的规则型 screener**——当日盘后逐票,
四条硬规则 C1..C4 全满足即入选(SELECT = C1 AND C2 AND C3 AND C4,单日触发)。
不注册为合议专家、不复用四形态识别。

规格(均前复权收盘,当日 = t;整数索引 t 指 kdf 第 t 根):
  C1 均线完整多头:MA5>MA10>MA20>MA30>MA60>MA200 且 C≥MA5
  C2 贴近/突破52周高:H52 = max(HIGH, t-250…t-1)(**不含当日**);下界·H52 ≤ C ≤ 上界·H52
  C3 近10日偏强:COUNT(C>O,10) > COUNT(C<O,10)(平盘 C==O 不计)
  C4 当日深跌收阳:(LOW−C₍ₜ₋₁₎)/C₍ₜ₋₁₎ ≤ 深跌阈值 且 C>O(盘中较昨收跌≥4% 但收阳 K)

防未来函数红线:H52 **不含当日**;所有量只用 t 及之前。历史需 ≥ 最少历史根数(251),不足不选。

参数全读 `THRESHOLDS["趋势深跌反包"]["入场"]`,不散写硬编码。
数据只读复用 `collectors.market.load_kline`(优先滚动主档、回退 raw)。
入口:`python -m tools.pipeline.screen_s01 [--codes ...|--universe N] [--date D] [--no-fetch]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_s01")

_CFG = THRESHOLDS["趋势深跌反包"]["入场"]


def _ma(close, t: int, period: int) -> float:
    """close 第 t 根的 period 日简单均线(用 t 及之前的 period 根)。t-period+1<0 视为不足→NaN。"""
    if t - period + 1 < 0:
        return float("nan")
    return float(close[t - period + 1: t + 1].mean())


def min_history() -> int:
    """入场判定所需最少历史根数(不足不选)。"""
    return int(_CFG["最少历史根数"])


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否入选,返回逐条布尔明细 + SELECT。

    t 需 ≥ 最少历史根数−1(即前面至少有 H52窗 根 + 够算 MA200);不足 → SELECT=False + 原因。
    只用 t 及之前的数据(防未来函数);H52 明确不含当日。
    """
    c = cfg or _CFG
    n = len(kdf)
    need = int(c["最少历史根数"])
    if t < 0 or t >= n:
        return {"SELECT": False, "原因": "索引越界"}
    if t + 1 < need:
        return {"SELECT": False, "原因": f"历史不足({t + 1}<{need})"}

    close = kdf["close"].to_numpy(dtype=float)
    open_ = kdf["open"].to_numpy(dtype=float)
    high = kdf["high"].to_numpy(dtype=float)
    low = kdf["low"].to_numpy(dtype=float)

    # —— C1 均线完整多头(周期升序 → 均线严格递减)+ C≥MA5 ——
    periods = list(c["均线多头周期"])
    mas = [_ma(close, t, p) for p in periods]
    c1 = all(mas[i] > mas[i + 1] for i in range(len(mas) - 1)) and close[t] >= mas[0]

    # —— C2 贴近/突破 52 周高(H52 不含当日)——
    win = int(c["H52窗口"])
    h52 = float(high[t - win: t].max())            # [t-win, t-1],不含 t
    lo_k, hi_k = float(c["贴近高下界"]), float(c["贴近高上界"])
    c2 = (lo_k * h52) <= close[t] <= (hi_k * h52)

    # —— C3 近 10 日偏强(平盘不计)——
    w = int(c["近强窗口"])
    seg = range(t - w + 1, t + 1)
    up = sum(1 for i in seg if close[i] > open_[i])
    dn = sum(1 for i in seg if close[i] < open_[i])
    c3 = up > dn

    # —— C4 当日深跌收阳 ——
    prev_c = close[t - 1]
    drop = (low[t] - prev_c) / prev_c if prev_c else 0.0
    c4 = (drop <= float(c["深跌阈值"])) and (close[t] > open_[t])

    select = bool(c1 and c2 and c3 and c4)
    return {
        "SELECT": select,
        "C1_均线多头": bool(c1), "C2_贴近52周高": bool(c2),
        "C3_近强": bool(c3), "C4_深跌收阳": bool(c4),
        "明细": {
            "MA": {p: (round(m, 4) if m == m else None) for p, m in zip(periods, mas)},
            "close": round(float(close[t]), 4), "H52": round(h52, 4),
            "近强_涨/跌": [up, dn], "当日跌幅": round(float(drop), 4),
            "收阳": bool(close[t] > open_[t]),
        },
    }


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, cfg)


# ———————————————————— 可选入场确认(向后兼容,不改默认行为)————————————————————
# 默认 confirm=None → 信号日 t 即入场(原行为)。开启后减少「接飞刀」。
def confirm_entry(kdf: pd.DataFrame, t: int, mode: str | None = None) -> int | None:
    """信号日 t 已满足 C1..C4 后,按 mode 决定真正入场的整数索引;不满足确认→None(放弃该信号)。

    mode:
      None       : 无确认——信号日 t 当日入场(返回 t,保持原行为)。
      "t1_nobreak": T+1 确认——次日(t+1)最低价不跌破信号日 T 的最低价才入场;
                    满足→返回 t+1(在 T+1 建仓,P0=T+1 收盘);t+1 越界或破低→None。

    只读价格、不改任何离场逻辑。未知 mode 按无确认处理(向后兼容)。
    """
    if mode is None or mode == "" or mode == "none":
        return t
    n = len(kdf)
    if mode == "t1_nobreak":
        if t + 1 >= n:
            return None                                # 无次日数据,无法确认
        low = kdf["low"].to_numpy(dtype=float)
        return (t + 1) if low[t + 1] >= low[t] else None
    return t                                            # 未知 mode → 退回无确认


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_s01_screen(codes: list[str], as_of: str | None = None,
                   fetch: bool = True) -> dict:
    """扫描 codes,对每票判最后一根是否入选,落 view「趋势深跌反包」。返回 summary。

    fetch=True:缺 K 线自动采集;False:只读本地缓存(离线复算,不触网)。
    历史不足(<最少历史根数)的票记入「跳过数」,不入选(不足不选)。
    """
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
        r = screen_latest(kdf)
        if r.get("SELECT"):
            selected.append({"code": code, "明细": r["明细"]})

    view = {
        "as_of": as_of,
        "策略": "趋势深跌反包(S01)",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": ("C1 均线完整多头(MA5>MA10>MA20>MA30>MA60>MA200 且 C≥MA5)AND "
                 "C2 0.9·H52≤C≤1.2·H52(H52 不含当日)AND "
                 "C3 近10日 COUNT(C>O)>COUNT(C<O)(平盘不计)AND "
                 "C4 (LOW−C_prev)/C_prev≤−0.04 且 C>O(深跌收阳)"),
        "防未来函数": "H52 不含当日;所有量只用 t 及之前;历史<251 不选",
    }
    p = store.put_view("趋势深跌反包", view)
    logger.info("趋势深跌反包:扫描 %d / 有效 %d / 跳过(历史不足)%d / 入选 %d → %s",
                len(codes), scanned, skipped, len(selected), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S01 趋势深跌反包 入场扫描")
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
    logger.info("S01 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_s01_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
