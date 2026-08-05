"""基本面采集:财报、估值、ROE、行业对比。

主数据源:akshare `stock_financial_abstract`(财报摘要)、
`stock_a_indicator_lg`(历史 PE/PB/股息率)、`stock_financial_report_sina`(三大报表)。
落盘:data/raw/fundamental/{code}.json
"""


def fetch_fundamental(codes: list[str]) -> dict[str, dict]:
    """拉取基本面并落盘。

    输入:codes 代码列表。
    输出:{code: {revenue, net_profit, roe, pe, pb, gross_margin, debt_ratio, ...}}。
    """
    raise NotImplementedError("P2 阶段实现")


def load_fundamental(code: str) -> dict:
    """从本地缓存读单票基本面。"""
    raise NotImplementedError("P2 阶段实现")
