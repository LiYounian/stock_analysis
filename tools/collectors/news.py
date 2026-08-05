"""新闻/研报采集。

主数据源:akshare 个股新闻 `stock_news_em`、研报 `stock_research_report_em`。
落盘:data/raw/news/{code}.json
"""


def fetch_news(codes: list[str], days: int = 7) -> dict[str, list[dict]]:
    """拉取近 days 天个股新闻并落盘。

    输出:{code: [{date, title, source, url, content}, ...]}。
    """
    raise NotImplementedError("P2 阶段实现")


def fetch_research_reports(codes: list[str]) -> dict[str, list[dict]]:
    """拉取券商研报(含评级)。

    输出:{code: [{date, org, rating, target_price, title}, ...]}。
    rating 用于情绪层「研报评级」信号。
    """
    raise NotImplementedError("P2 阶段实现")


def load_news(code: str) -> list[dict]:
    """从本地缓存读单票新闻。"""
    raise NotImplementedError("P2 阶段实现")
