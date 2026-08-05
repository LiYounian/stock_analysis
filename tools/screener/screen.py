"""选股筛选器(阶段一)。留口子:具体筛选规则待用户定(需求 N1)。

定位:在「个股评估」之前,从票池/全市场筛出候选股。
设计成 pluggable —— 每个筛选条件是一个可组合的 filter,规则确定后填。
契约见 docs/需求与目标.md 第 3 节 + docs/架构设计.md。
"""
from __future__ import annotations

from typing import Callable

# 一个筛选条件:输入单票的综合画像 dict,返回是否通过
Filter = Callable[[dict], bool]


def screen(profiles: dict[str, dict], filters: list[Filter]) -> list[str]:
    """对票池综合画像逐一过滤,返回通过所有条件的代码列表。

    输入:{code: 综合画像}(来自 analysis/portfolio.aggregate);filters 条件列表。
    输出:通过的 code 列表。
    这层已可用;具体 filters 由下面工厂产出(规则待定)。
    """
    return [code for code, prof in profiles.items()
            if all(f(prof) for f in filters)]


# ---------- 内置筛选条件(留口子,待用户明确规则 N1)----------
def by_reversal(min_score: int = 50) -> Filter:
    """拐点评分达标(超跌反弹启动)。示例条件,阈值待定。"""
    raise NotImplementedError("待用户明确筛选规则 N1")


def by_fundflow(min_days: int = 3) -> Filter:
    """主力资金连续净流入。待 P3.1 资金流 + 规则 N1。"""
    raise NotImplementedError("待 P3.1 资金流 + 规则 N1")


def by_technical_rating(min_score: int = 30) -> Filter:
    """技术评级达标。阈值待定。"""
    raise NotImplementedError("待用户明确筛选规则 N1")


def default_filters() -> list[Filter]:
    """默认筛选组合。规则待用户拍板后填(N1)。"""
    raise NotImplementedError("待用户明确筛选规则 N1")
