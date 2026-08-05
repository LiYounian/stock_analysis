"""统一 LLM 客户端(可插拔 provider)。

业务层只依赖 LLMClient 抽象,不关心底层是用户 API 还是 qwen。
设计详见 docs/大模型调用设计.md。
provider 规格待用户提供(见该文档第 6 节),到位后填实现。
"""
from typing import Protocol


class LLMClient(Protocol):
    """LLM 调用抽象。两种模式:chat(自由文本)/ extract(结构化 JSON)。"""

    def chat(self, messages: list[dict], *, temperature: float = 0.0,
             max_tokens: int = 2048) -> str:
        """自由问答,返回纯文本。用于归纳/摘要(L4~L6)。"""
        ...

    def extract(self, text: str, schema: dict, *, instruction: str,
                temperature: float = 0.0) -> dict:
        """结构化抽取:拼 prompt → 强制 JSON → 解析校验 → 失败重试。

        用于 L1(新闻关键信息提取)/L2(情感)/L3(政策解读)。
        解析失败抛错重试(不静默返回空);超重试次数则抛错让上层落 error。
        """
        ...

    def batch_extract(self, texts: list[str], schema: dict, *,
                      instruction: str) -> list[dict]:
        """批量抽取,内部按 settings.QWEN_BATCH_SIZE 分批,省往返。"""
        ...


class OpenAICompatClient:
    """面向 OpenAI /chat/completions 兼容 API(用户将提供的接口,默认形态)。

    base_url / api_key / model 从 settings 读(env 注入,禁止硬编)。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        raise NotImplementedError("待用户提供 API 规格后实现(见设计文档第 6 节)")

    def chat(self, messages, *, temperature=0.0, max_tokens=2048) -> str:
        raise NotImplementedError

    def extract(self, text, schema, *, instruction, temperature=0.0) -> dict:
        raise NotImplementedError

    def batch_extract(self, texts, schema, *, instruction) -> list[dict]:
        raise NotImplementedError


class QwenDelegateClient:
    """封装现有 qwen-delegate CLI,用于大批量(L2 情感),走 内部网关不烧主额度。

    调用形态:subprocess 调 `qwen-delegate <任务名> "<prompt>"`,解析其输出。
    prompt 需堵反问陷阱(见约法第 10 条)。
    """

    def __init__(self):
        raise NotImplementedError("P4 阶段实现")

    def chat(self, messages, *, temperature=0.0, max_tokens=2048) -> str:
        raise NotImplementedError

    def extract(self, text, schema, *, instruction, temperature=0.0) -> dict:
        raise NotImplementedError

    def batch_extract(self, texts, schema, *, instruction) -> list[dict]:
        raise NotImplementedError


def get_client(purpose: str) -> LLMClient:
    """工厂:按 settings.LLM_ROUTE 为不同触点选 provider。

    purpose: "extract" / "sentiment" / "summary"。
    重批量(sentiment)→ QwenDelegateClient;精确抽取/摘要 → OpenAICompatClient。
    """
    raise NotImplementedError("待 provider 实现到位后接线")
