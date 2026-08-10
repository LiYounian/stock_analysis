"""策略层包:选股/评分/信号三类可扩充策略的统一注册表(层间接口)。

见 docs/信息流转与层职责.md §2.3(B)。对外只暴露注册/发现/调用接口;
导入本包即完成内置策略(复用 screener 预设 + 示例评分/信号)的注册。
"""
from tools.strategy.registry import (
    STRATEGY_KINDS,
    StrategyMeta,
    get,
    list_strategies,
    register,
    run,
    strategy,
)
from tools.strategy import momentum as _momentum  # noqa: F401  导入即注册

__all__ = ["STRATEGY_KINDS", "StrategyMeta", "strategy", "register",
           "get", "list_strategies", "run"]
