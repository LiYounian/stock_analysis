"""情感打分:对新闻/研报/UGC 文本判断情感倾向。

高耗 token 的批量任务,默认外包 qwen,不烧主额度。
统一走 tools.llm.client.get_client("sentiment"),prompt 见 tools.llm.prompts。
见 settings.USE_QWEN_SENTIMENT / QWEN_BATCH_SIZE / LLM_ROUTE。
"""


def score(texts: list[str]) -> list[dict]:
    """批量情感打分。

    输入:texts 文本列表。
    输出:[{score: float(-1~1), label: 利空/中性/利好, reason: str}, ...]。
    实现:分批调 qwen-delegate,prompt 需堵死反问陷阱、给定严格输出 schema。
    """
    raise NotImplementedError("P4 阶段实现")


def aggregate_score(items: list[dict]) -> dict:
    """把一票的多条情感打分聚合成单票情绪分。

    输出:{net_sentiment: float, bull_ratio, bear_ratio, sample_n}。
    """
    raise NotImplementedError("P4 阶段实现")
