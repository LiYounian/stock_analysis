"""回测绩效指标(纯函数,易单测)。骨架待填(见 需求.md)。"""
from __future__ import annotations

import pandas as pd


def cum_return(returns: pd.Series) -> float:
    """累计收益 = ∏(1+r) − 1。"""
    raise NotImplementedError("BT.1")


def annualized(returns: pd.Series, periods_per_year: int = 244) -> float:
    """年化收益。"""
    raise NotImplementedError("BT.1")


def max_drawdown(equity: pd.Series) -> float:
    """最大回撤(净值曲线)。"""
    raise NotImplementedError("BT.1")


def sharpe(returns: pd.Series, rf: float = 0.0, periods_per_year: int = 244) -> float:
    """夏普比率。"""
    raise NotImplementedError("BT.1")


def win_rate(trades: list) -> float:
    """胜率 = 盈利笔数 / 总笔数。"""
    raise NotImplementedError("BT.1")
