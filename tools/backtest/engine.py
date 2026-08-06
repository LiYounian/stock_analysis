"""回测引擎:历史回放策略 → 绩效。骨架待填(见 需求.md / docs/计划/回测层.md)。

命门:walk-forward,第 t 日只用 ≤t 数据,严禁未来函数(必测)。
依赖:tools.store(历史 raw)+ tools.strategy(被测策略)。
"""
from __future__ import annotations


def backtest(strategy_name: str, codes: list[str], start: str, end: str, *,
             rebalance_days: int = 5, cost_bps: float = 5.0, price: str = "close",
             benchmark: str = "equal") -> dict:
    """回放策略 → 绩效。返回 {策略,类型,区间,绩效,基准,超额,明细ref}。

    选股策略:每 rebalance_days 用当时可见数据选股 → 等权持有 → 组合收益。
    信号策略:单票逐日买/卖/持 → 次日成交 → 收益。
    严禁未来函数。
    """
    raise NotImplementedError("BT.1:选股回测(见 需求.md)")
