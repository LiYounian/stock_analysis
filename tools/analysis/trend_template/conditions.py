"""趋势模板 8 条件判定(单票,无未来函数)。

移植自 `feat/s02-trend-filter` 的 `_trend_template`,拆成 A1–A8 独立布尔并对齐需求口径:
A3 回看窗走配置 `trend_lookback_days`(默认 20);A8 的 `rps250` 为**当日横截面百分位**,
由调用方(rps.py)预计算后喂入——单票函数不做横截面。

异常(需求 §10):
  - ValidBars < min_bars           → INSUFFICIENT_DATA(不参与)
  - 收盘价为空/NaN/≤0              → INVALID_DATA(不参与)
  - 当日成交额缺失                  → 不报错,仅令**增强模式**不通过(§3.3.2)

pass_mode:通过的最高模式(增强 ⊃ 完整 ⊃ 基础);基础模式 A1–A7 不依赖 RPS。
⚠️ 非投资建议。
"""
from __future__ import annotations

from tools.analysis.trend_template import indicators as ind
from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["趋势模板"]


def _empty_conditions() -> dict:
    return {f"a{i}": None for i in range(1, 9)}


def evaluate(kline, t: int | None = None, rps250: float | None = None,
             amount: float | None = None, cfg: dict | None = None) -> dict:
    """判 kline 第 t 根是否符合趋势模板。返回条件/取值/异常/pass_mode。

    kline: 需含 close/high/low 列的 DataFrame。
    rps250: 当日横截面 RPS(0–100);None → A8 无法判(基础模式仍可跑)。
    amount: 当日成交额(元);None → 增强模式不通过(不报错)。
    """
    c = cfg or _CFG
    n = len(kline) if kline is not None else 0
    if n == 0:
        return {"conditions": _empty_conditions(), "values": {},
                "异常": "INSUFFICIENT_DATA", "pass_mode": None}
    if t is None:
        t = n - 1
    if t < 0 or t >= n:
        return {"conditions": _empty_conditions(), "values": {},
                "异常": "INVALID_DATA", "pass_mode": None}

    min_bars = int(c["min_bars"])
    if ind.valid_bars(kline, t) < min_bars:
        return {"conditions": _empty_conditions(), "values": {},
                "异常": "INSUFFICIENT_DATA", "pass_mode": None}

    close = kline["close"].to_numpy(dtype=float)
    high = kline["high"].to_numpy(dtype=float)
    low = kline["low"].to_numpy(dtype=float)
    px = float(close[t])
    if not ind._valid(px) or px <= 0:
        return {"conditions": _empty_conditions(), "values": {},
                "异常": "INVALID_DATA", "pass_mode": None}

    p50, p150, p200 = int(c["ma_short"]), int(c["ma_medium"]), int(c["ma_long"])
    look = int(c["trend_lookback_days"])
    win = int(c["week52_window"])
    lo_mult = float(c["min_gain_from_52w_low"])
    hi_mult = float(c["max_distance_from_52w_high"])
    rps_thr = float(c["min_rps"])

    ma50 = ind.ma(close, t, p50)
    ma150 = ind.ma(close, t, p150)
    ma200 = ind.ma(close, t, p200)
    ma200_prev = ind.ma(close, t - look, p200)
    low52 = ind.lowest_low(low, t, win)
    high52 = ind.highest_high(high, t, win)
    ret250 = ind.return_n(close, t, win)

    def _has(*xs) -> bool:
        return all(x is not None for x in xs)

    a1 = _has(ma150, ma200) and px > ma150 and px > ma200
    a2 = _has(ma150, ma200) and ma150 > ma200
    a3 = _has(ma200, ma200_prev) and ma200 > ma200_prev
    a4 = _has(ma50, ma150, ma200) and ma50 > ma150 and ma50 > ma200
    a5 = _has(ma50) and px > ma50
    a6 = _has(low52) and px >= low52 * lo_mult
    a7 = _has(high52) and px >= high52 * hi_mult
    a8 = None if rps250 is None else (float(rps250) >= rps_thr)

    conditions = {"a1": bool(a1), "a2": bool(a2), "a3": bool(a3), "a4": bool(a4),
                  "a5": bool(a5), "a6": bool(a6), "a7": bool(a7),
                  "a8": (None if a8 is None else bool(a8))}

    base_pass = all([a1, a2, a3, a4, a5, a6, a7])
    full_pass = base_pass and (a8 is True)
    amount_ok = amount is not None and ind._valid(amount) and float(amount) >= float(c["min_amount"])
    enh_pass = full_pass and px >= float(c["min_price"]) and amount_ok
    pass_mode = "增强" if enh_pass else ("完整" if full_pass else ("基础" if base_pass else None))

    values = {
        "close": round(px, 4),
        "ma50": (round(ma50, 4) if ma50 is not None else None),
        "ma150": (round(ma150, 4) if ma150 is not None else None),
        "ma200": (round(ma200, 4) if ma200 is not None else None),
        "ma200_prev": (round(ma200_prev, 4) if ma200_prev is not None else None),
        "lowest_low_250": (round(low52, 4) if low52 is not None else None),
        "highest_high_250": (round(high52, 4) if high52 is not None else None),
        "return250": (round(ret250, 6) if ret250 is not None else None),
        "rps250": (round(float(rps250), 2) if rps250 is not None else None),
        "amount": (float(amount) if amount is not None and ind._valid(amount) else None),
    }
    return {"conditions": conditions, "values": values, "异常": None, "pass_mode": pass_mode}
