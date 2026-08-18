"""SEPA 技术合格池:原书均线 3 条硬条件(不调整)。

必须同时满足:
  1. 股价 > MA50 > MA150 > MA200
  2. MA200 向上(当前 > 20 日前)
  3. 股价在 MA50、MA200 上方(与 1 对 MA50/MA200 重复,仍按原文三条都判)

无未来函数:MA 只用 t 及之前。历史不足 → 不入池。
⚠️ 非投资建议。
"""
from __future__ import annotations

import math

import pandas as pd

from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["SEPA_VCP"]


def _ma(close, t: int, period: int) -> float:
    if t - period + 1 < 0:
        return float("nan")
    return float(close[t - period + 1: t + 1].mean())


def sepa_pass(kline: pd.DataFrame, t: int | None = None, cfg: dict | None = None) -> dict:
    """判 kline 第 t 根是否过 SEPA。返回 {入池, 明细, 原因}。"""
    c = cfg or _CFG
    n = len(kline)
    if n == 0:
        return {"入池": False, "原因": "空 K 线"}
    if t is None:
        t = n - 1
    if t < 0 or t >= n:
        return {"入池": False, "原因": "索引越界"}
    need = int(c["最少历史根数"])
    look = int(c["MA200向上回看"])
    p50, p150, p200 = (int(x) for x in c["MA周期"])
    if t + 1 < need:
        return {"入池": False, "原因": f"历史不足({t + 1}<{need})"}

    close = kline["close"].to_numpy(dtype=float)
    px = float(close[t])
    ma50 = _ma(close, t, p50)
    ma150 = _ma(close, t, p150)
    ma200 = _ma(close, t, p200)
    ma200_ago = _ma(close, t - look, p200)
    if any(math.isnan(x) for x in (ma50, ma150, ma200, ma200_ago)):
        return {"入池": False, "原因": "均线不足"}

    stack = px > ma50 > ma150 > ma200
    ma200_up = ma200 > ma200_ago
    above = px > ma50 and px > ma200
    ok = bool(stack and ma200_up and above)
    return {
        "入池": ok,
        "明细": {
            "close": round(px, 4),
            "MA50": round(ma50, 4), "MA150": round(ma150, 4), "MA200": round(ma200, 4),
            "MA200_20日前": round(ma200_ago, 4),
            "排列": stack, "MA200向上": ma200_up, "站上MA50与MA200": above,
        },
    }


def screen_latest(kline: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判最后一根。"""
    if kline is None or len(kline) == 0:
        return {"入池": False, "原因": "空 K 线"}
    return sepa_pass(kline, len(kline) - 1, cfg)
