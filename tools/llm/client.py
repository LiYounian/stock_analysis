"""统一 LLM 客户端(OpenAI 兼容,走用户环境变量配置)。

业务层只依赖 get_client();底层用 openai SDK 调 内部网关 DeepSeek(deepseek-v4-pro)。
url/key 从环境变量读(settings.LLM_*),**不硬编、不入库**。
设计见 docs/大模型调用设计.md。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol

from tools.config import settings

logger = logging.getLogger("llm.client")


class LLMClient(Protocol):
    def chat(self, messages: list[dict], *, temperature: float = 0.0,
             max_tokens: int = 2048) -> str: ...

    def extract(self, text: str, schema: dict, *, instruction: str,
                temperature: float = 0.0) -> dict: ...

    def batch_extract(self, texts: list[str], schema: dict, *,
                      instruction: str) -> list[dict]: ...


def _extract_json(content: str) -> dict:
    """从模型回复里抽出 JSON。支持 ```json ``` 围栏 / 裸 JSON。失败抛 ValueError。"""
    if not content:
        raise ValueError("空回复")
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", content, re.S)
    raw = m.group(1) if m else content
    if not m:  # 无围栏,截取首个 { 到末个 }
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            raw = raw[s:e + 1]
    return json.loads(raw)


class OpenAICompatClient:
    """OpenAI 兼容客户端(deepseek-v4-pro @ 内部网关)。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        if not base_url or not api_key:
            raise RuntimeError(
                "LLM 未配置:请在环境变量设置 LLM_BASE_URL + LLM_API_KEY"
                "(或显式 LLM_BASE_URL / LLM_API_KEY)。")
        from openai import OpenAI
        self._cli = OpenAI(api_key=api_key, base_url=base_url, timeout=settings.LLM_TIMEOUT)
        self.model = model

    def chat(self, messages, *, temperature=0.0, max_tokens=2048) -> str:
        r = self._cli.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens)
        return r.choices[0].message.content or ""

    def extract(self, text, schema, *, instruction, temperature=0.0) -> dict:
        """结构化抽取:强制 JSON + 解析失败重试;超次数抛错(不静默返空,约法第5条)。"""
        sys = (f"{instruction}\n"
               f"只输出一个 JSON,不要任何多余文字/解释。JSON 字段与含义:"
               f"{json.dumps(schema, ensure_ascii=False)}")
        last_err = None
        for attempt in range(settings.LLM_MAX_RETRY):
            content = self.chat(
                [{"role": "system", "content": sys}, {"role": "user", "content": text}],
                temperature=temperature)
            try:
                return _extract_json(content)
            except (ValueError, json.JSONDecodeError) as e:
                last_err = e
                logger.warning("JSON 解析失败(第%d次): %s", attempt + 1, str(e)[:60])
        raise ValueError(f"抽取重试 {settings.LLM_MAX_RETRY} 次仍失败: {last_err}")

    def batch_extract(self, texts, schema, *, instruction) -> list[dict]:
        """逐条抽取(失败的条目标 error,不中断整批)。"""
        out = []
        for t in texts:
            try:
                out.append(self.extract(t, schema, instruction=instruction))
            except Exception as e:
                out.append({"error": str(e)[:80]})
        return out


def get_client(purpose: str = "extract") -> LLMClient:
    """工厂:返回配置好的 LLM 客户端。purpose 预留(未来可路由不同 provider)。"""
    return OpenAICompatClient(settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL)


def is_configured() -> bool:
    """LLM 是否已配置(env 就绪)。供采集/分析层判断是否降级。"""
    return bool(settings.LLM_BASE_URL and settings.LLM_API_KEY)
