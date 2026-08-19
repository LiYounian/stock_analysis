"""米勒维尼趋势模板筛选(完整 8 条 A1–A8)。

纯计算层:单票指标(indicators)+ 条件判定(conditions)+ 横截面 RPS(rps)。
只做第一层趋势过滤,不含 VCP / 枢轴点 / 突破放量 / 买点 / 自动交易。
⚠️ 非投资建议。见 docs/计划/趋势模板筛选系统_需求与方案.md。
"""
from tools.analysis.trend_template.conditions import evaluate
from tools.analysis.trend_template.indicators import (
    highest_high,
    lowest_low,
    ma,
    return_n,
    valid_bars,
)
from tools.analysis.trend_template.rps import rps_from_returns

__all__ = ["ma", "lowest_low", "highest_high", "return_n", "valid_bars",
           "evaluate", "rps_from_returns"]
