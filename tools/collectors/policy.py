"""政策采集:宏观/行业政策(国内 + 国外)。

情绪三层中的「政策」层。无一站式结构化源,用 WebSearch 定向检索 +
按行业关键词命中(如 半导体/存储/机器人/算力 × 央行/证监会/出口管制/关税)。
落盘:data/raw/policy/policy_{date}.json
"""


def fetch_policy(keywords: list[str], days: int = 7) -> list[dict]:
    """按关键词定向检索近 days 天政策/宏观新闻并落盘。

    输入:keywords 行业+政策关键词列表。
    输出:[{date, title, source, url, region(国内/国外), summary}, ...]。
    检索动作由上层(Claude 主控 WebSearch)执行,本函数负责归并+落盘。
    """
    raise NotImplementedError("P4 阶段实现")


def default_keywords() -> list[str]:
    """基于股票池行业生成默认政策检索关键词。"""
    raise NotImplementedError("P4 阶段实现")
