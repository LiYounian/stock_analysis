"""事件三层分类:把公告/新闻/政策归入情绪三层并打重要度。

三层:政策(policy) / 公司内部行为(company) / 舆情(public_opinion)。
这是情绪影响分析的骨架:所有文本类信号先归层、打标、判方向。
"""

LAYERS = ("policy", "company", "public_opinion")


def classify(items: list[dict]) -> list[dict]:
    """对混合的事件条目归层 + 打标 + 判影响方向。

    输入:items 混合来源条目(公告/新闻/政策/UGC),每条含 {source, title, text, ...}。
    输出:[{
        layer: policy|company|public_opinion,
        tag:   细分标签(如 增持/回购/出口管制/评级上调/大V看多),
        importance: 1~5,
        impact_direction: 利好/利空/中性,
        affected_codes: [...],   # 命中的票
    }, ...]。
    实现:规则打标为主(结构化的公告/研报),定性部分可借 qwen。
    """
    raise NotImplementedError("P2 阶段实现")


def to_layer(source: str, tag: str) -> str:
    """把 (来源, 标签) 映射到三层之一。"""
    raise NotImplementedError("P2 阶段实现")
