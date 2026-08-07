"""多因子截面打分(F6)—— 自包含策略包(仿 pattern_screener 结构)。

低换手多因子:质量/价值/低波/成长/股息 + 资金流(北向 D8);**规避纯动量**(A股动量弱/
反转强,见 docs/参考/选股与收益支点策略_网络调研.md §类别1)。截面标准化(I3 分位)→ 等权
(I2)合成 → 排序分层 → 每票落 code_view "factor",供合议体系「多因子」专家(experts.py)读。

子模块:
  factor —— 单票原始子指标提取(读记录/K线,无截面)
  score  —— 截面标准化 + 合成 + 分层单调性 + 落库(需全票池)
需求见 docs/计划/多策略合议_实现需求.md F6;因子/数据源见调研 §三对照表。
"""
from __future__ import annotations

from tools.analysis.factor import factor, score
from tools.analysis.factor.factor import raw_factors
from tools.analysis.factor.score import cross_section, monotonicity, precompute

__all__ = ["factor", "score", "raw_factors", "cross_section", "monotonicity", "precompute"]
