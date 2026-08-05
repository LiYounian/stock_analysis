"""财报分析模块(深度挖掘)。留口子:分析框架与产出待用户单独讨论(需求 N2)。

定位:对选定个股做财报深度分析,作为个股评估的"基本面深挖"维度。
待讨论:看哪些财务维度、产出什么结论、哪些字段用 LLM 从财报原文抽取。
可复用 collectors/fundamental.py(已有:营收/净利/ROE/毛利/负债/估值)+
未来的三大报表原文(akshare stock_financial_report_sina)。
契约见 docs/需求与目标.md N2 + docs/架构设计.md。
"""
from __future__ import annotations


def analyze(code: str) -> dict:
    """对单票做财报深度分析。

    输出结构待定(N2),预期方向:
      {盈利质量, 成长性, 现金流质量, 偿债能力, 杜邦分解, 风险点, 结论}
    其中定性/原文抽取部分可能走 LLM(见 tools/llm)。
    """
    raise NotImplementedError("财报分析框架待用户讨论后确定(需求 N2)")


def need_llm_fields() -> list[str]:
    """列出计划用 LLM 从财报原文抽取的字段(待 N2 + Q7)。"""
    raise NotImplementedError("待 N2 讨论")
