"""趋势模板单票指标(纯函数,无网络、无未来函数)。

一律只用第 t 根及之前的数据(索引 ≤ t)。数据不足 → 返回 None(**绝不填 0** 参与计算)。
移植自 `feat/s02-trend-filter` 的 `_ma` 口径(SMA=最近 period 根均值),并补 52 周高/低、
Return250、ValidBars。⚠️ 非投资建议。
"""
from __future__ import annotations

import math
from typing import Sequence


def _seq(arr) -> list[float]:
    """DataFrame 列 / Series / np.array / 序列 → list[float]。"""
    if hasattr(arr, "to_numpy"):
        arr = arr.to_numpy()
    return [float(x) for x in arr]


def _valid(x: float) -> bool:
    """有限实数(非 NaN、非 inf)。"""
    return isinstance(x, (int, float)) and math.isfinite(x)


def ma(close: Sequence[float], t: int, n: int) -> float | None:
    """第 t 根的 n 日简单均线(用 t 及之前的 n 根)。不足 n 根 → None。"""
    xs = _seq(close)
    if t < 0 or t >= len(xs) or t - n + 1 < 0:
        return None
    seg = xs[t - n + 1: t + 1]
    if not seg or any(not _valid(v) for v in seg):
        return None
    return sum(seg) / len(seg)


def lowest_low(low: Sequence[float], t: int, win: int = 250) -> float | None:
    """截至第 t 根(含)、往前 win 根窗口内的最低价(52 周低)。窗口不足 → None。"""
    xs = _seq(low)
    if t < 0 or t >= len(xs) or t - win + 1 < 0:
        return None
    seg = [v for v in xs[t - win + 1: t + 1] if _valid(v)]
    if len(seg) < win:
        return None
    return min(seg)


def highest_high(high: Sequence[float], t: int, win: int = 250) -> float | None:
    """截至第 t 根(含)、往前 win 根窗口内的最高价(52 周高)。窗口不足 → None。"""
    xs = _seq(high)
    if t < 0 or t >= len(xs) or t - win + 1 < 0:
        return None
    seg = [v for v in xs[t - win + 1: t + 1] if _valid(v)]
    if len(seg) < win:
        return None
    return max(seg)


def return_n(close: Sequence[float], t: int, n: int = 250) -> float | None:
    """n 日累计涨幅 Close(t)/Close(t-n) - 1。基准价缺失或 ≤0 → None。"""
    xs = _seq(close)
    if t < 0 or t >= len(xs) or t - n < 0:
        return None
    now, base = xs[t], xs[t - n]
    if not (_valid(now) and _valid(base)) or base <= 0:
        return None
    return now / base - 1.0


def valid_bars(kline, t: int | None = None) -> int:
    """截至第 t 根(含)的**有效** K 线根数。

    有效 = 收盘价为有限正数,且(若有 is_trading 列)标记正常交易。
    停牌 / 缺失 / 无效(NaN、≤0)不计入,绝不填 0 冒充有效根(符合需求 §4.2)。
    """
    if kline is None or len(kline) == 0:
        return 0
    n = len(kline)
    if t is None:
        t = n - 1
    if t < 0:
        return 0
    t = min(t, n - 1)
    closes = _seq(kline["close"]) if hasattr(kline, "__getitem__") else _seq(kline)
    trading = None
    if hasattr(kline, "columns") and "is_trading" in getattr(kline, "columns"):
        trading = [bool(x) for x in kline["is_trading"].tolist()]
    cnt = 0
    for i in range(0, t + 1):
        if not (_valid(closes[i]) and closes[i] > 0):
            continue
        if trading is not None and not trading[i]:
            continue
        cnt += 1
    return cnt
