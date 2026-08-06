"""回测绩效指标(纯函数,易单测)。

全部只吃 pandas.Series / list[float],不碰 IO、不依赖上层。断言语义见
tests/test_backtest.py。口径约定(锁在测试里,勿随意改):
  - cum_return / annualized 吃**逐期收益率**序列(r_t = P_t/P_{t-1}-1)。
  - max_drawdown 吃**净值曲线**(equity,如 (1+r).cumprod()),返回**带符号的负值**
    (最大跌幅,-0.2 表示从峰值回撤 20%;无回撤为 0.0)。
  - sharpe 吃逐期收益率,按 sqrt(periods_per_year) 年化;std=0 或样本不足返回 0.0。
  - win_rate 吃**每笔交易收益**列表(round-trip 收益率),盈利笔数/总笔数。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# 年化默认每年交易日数(与 config/strategy.py['回测'] 对齐)
_PERIODS_PER_YEAR = 244


def cum_return(returns: pd.Series) -> float:
    """累计收益 = ∏(1+r) − 1。空序列返回 0.0。"""
    r = pd.Series(returns, dtype=float).dropna()
    if r.empty:
        return 0.0
    return float((1.0 + r).prod() - 1.0)


def annualized(returns: pd.Series, periods_per_year: int = _PERIODS_PER_YEAR) -> float:
    """年化收益 = (1+累计收益)^(每年期数/期数) − 1。

    空序列返回 0.0;若累计收益 ≤ −100%(本金归零)返回 −1.0(防复数/负底数幂)。
    """
    r = pd.Series(returns, dtype=float).dropna()
    n = len(r)
    if n == 0:
        return 0.0
    total = cum_return(r)
    base = 1.0 + total
    if base <= 0.0:
        return -1.0
    return float(base ** (periods_per_year / n) - 1.0)


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤(净值曲线)。返回带符号负值:峰值到谷底最大跌幅,无回撤为 0.0。

    dd_t = equity_t / cummax(equity)_t − 1;取最小(最负)。
    """
    e = pd.Series(equity, dtype=float).dropna()
    if e.empty:
        return 0.0
    peak = e.cummax()
    dd = e / peak - 1.0
    return float(dd.min())


def sharpe(returns: pd.Series, rf: float = 0.0, periods_per_year: int = _PERIODS_PER_YEAR) -> float:
    """夏普 = mean(r−rf) / std(r) × sqrt(每年期数)。

    rf 为**逐期**无风险利率(默认 0)。样本 < 2 或 std 为 0/NaN 返回 0.0(无波动无夏普)。
    std 用样本标准差(ddof=1),与 pandas 默认一致。
    """
    r = pd.Series(returns, dtype=float).dropna()
    if len(r) < 2:
        return 0.0
    excess = r - rf
    sd = float(excess.std(ddof=1))
    if not np.isfinite(sd) or sd == 0.0:
        return 0.0
    return float(excess.mean() / sd * np.sqrt(periods_per_year))


def win_rate(trades: list) -> float:
    """胜率 = 盈利笔数 / 总笔数。trades 为每笔交易收益率列表。空列表返回 0.0。"""
    vals = [float(x) for x in (trades or []) if x is not None]
    if not vals:
        return 0.0
    wins = sum(1 for x in vals if x > 0.0)
    return float(wins / len(vals))
