"""公告采集:公司内部行为(增减持/回购/业绩预告/诉讼/重大合同)。

情绪三层中的「公司内部行为」层,结构化程度高,优先做。
主数据源:akshare 巨潮 `stock_zh_a_disclosure_report_cninfo`、
业绩预告 `stock_yjyg_em`、回购 `stock_repurchase_em`(名以官方文档为准)。
落盘:data/raw/announcement/{code}.json
"""


def fetch_announcements(codes: list[str], days: int = 7) -> dict[str, list[dict]]:
    """拉取近 days 天公告并落盘。

    输出:{code: [{date, title, type, url, summary}, ...]}。
    type 例:增持/减持/回购/业绩预告/业绩快报/诉讼/关联交易。
    """
    raise NotImplementedError("P2 阶段实现")


def load_announcements(code: str) -> list[dict]:
    """从本地缓存读单票公告。"""
    raise NotImplementedError("P2 阶段实现")
