"""Prompt 模板集中管理(可版本化)。

每个 LLM 触点一个模板函数。写法遵循约法第 3 条:描述性一般约束,不塞具体 case,
给定严格输出 schema。外包 qwen 的模板额外堵反问陷阱。
schema 与触点对应见 docs/大模型调用设计.md 第 1 节。
"""

# —— L1:新闻/公告关键信息提取 ——
NEWS_EXTRACT_SCHEMA = {
    "event_type": "str",       # 事件类型:业绩/增减持/回购/合同/诉讼/政策/其他
    "subjects": "list[str]",   # 涉及主体(公司/机构/人)
    "figures": "list[str]",    # 关键数字(金额/比例/数量)
    "time": "str",             # 事件时间
    "impact_direction": "str", # 利好/利空/中性
}


def news_extract_instruction() -> str:
    """L1 抽取指令。只抄原文事实、禁止推断编造;拿不准的字段留空不硬凑。"""
    raise NotImplementedError("P2 阶段填写")


# —— L2:情感打分 ——
SENTIMENT_SCHEMA = {"score": "float(-1~1)", "label": "利好/中性/利空", "reason": "str"}


def sentiment_instruction() -> str:
    """L2 情感指令(批量,走 qwen)。堵反问陷阱 + 严格 JSON schema。"""
    raise NotImplementedError("P4 阶段填写")


# —— L3:政策解读 ——
POLICY_SCHEMA = {
    "affected_industries": "list[str]",
    "direction": "利好/利空/中性",
    "strength": "1~5",
    "basis": "str",
}


def policy_instruction(industries: list[str]) -> str:
    """L3 政策影响解读指令。给定本票池行业列表,只在其中命中。"""
    raise NotImplementedError("P4 阶段填写")


# —— L4:舆情观点归纳 ——
def opinion_summary_instruction() -> str:
    """L4 UGC 观点归纳指令。输出主流观点/多空比/争议点。"""
    raise NotImplementedError("P4 阶段填写")
