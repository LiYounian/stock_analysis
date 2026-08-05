"""报告层:渲染两级 Markdown 报告。

- 组合层报告:行业分布、主线强弱、情绪温度、重点票筛选(方案2 第一层)
- 个股深挖报告:重点票的技术面 + 情绪三层(方案2 第二层)
产出到 docs/报告/。
"""


def build_portfolio_report(agg: dict) -> str:
    """渲染组合层报告并落盘。

    输入:portfolio.aggregate 的输出。
    输出:报告文件路径 docs/报告/组合_{date}.md。
    """
    raise NotImplementedError("P3 阶段实现")


def build_stock_report(code: str, result: dict) -> str:
    """渲染单只重点票深挖报告并落盘。

    输入:code + 该票 {technical, sentiment, events, fundamental}。
    输出:报告文件路径 docs/报告/个股_{code}_{date}.md。
    """
    raise NotImplementedError("P4 阶段实现")
