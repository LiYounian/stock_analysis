"""统一 LLM 客户端(OpenAI 兼容,走用户环境变量配置)。

业务层只依赖 get_client();底层用 openai SDK 调 内部网关 DeepSeek(deepseek-v4-pro)。
url/key 从环境变量读(settings.LLM_*),**不硬编、不入库**。
设计见 docs/大模型调用设计.md。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Protocol

from tools.config import settings

logger = logging.getLogger("llm.client")

# 瞬时错误关键词兜底:网关有时抛裸 Exception(非 openai 异常类型),按消息识别可重试类。
_TRANSIENT_KEYWORDS = (
    "connection error", "connection aborted", "connection reset",
    "timeout", "timed out", "rate limit", "too many requests",
    "temporarily", "try again", "429", "500", "502", "503", "504",
    "service unavailable", "bad gateway", "gateway timeout",
)


def _is_transient_error(exc: Exception) -> bool:
    """判断异常是否瞬时可重试(连接/超时/限流/5xx)。

    优先按 openai SDK 异常类型判(APIConnectionError/APITimeoutError/RateLimitError/
    InternalServerError + APIStatusError 的 429/5xx);openai 未装或裸 Exception 时按消息
    关键词兜底(09-03 大面积 `Connection error.` 即 openai.APIConnectionError 的 str)。
    """
    try:
        import openai
        typed = tuple(
            t for t in (
                getattr(openai, "APIConnectionError", None),
                getattr(openai, "APITimeoutError", None),
                getattr(openai, "RateLimitError", None),
                getattr(openai, "InternalServerError", None),
            ) if isinstance(t, type)
        )
        if typed and isinstance(exc, typed):
            return True
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and (status == 429 or status >= 500):
            return True
    except Exception:               # openai 未装/导入异常:退回关键词兜底
        pass
    msg = str(exc).lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


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
                "(只在本机 shell,不入库;model 写死 deepseek-v4-pro)。")
        from openai import OpenAI
        self._cli = OpenAI(api_key=api_key, base_url=base_url, timeout=settings.LLM_TIMEOUT)
        self.model = model

    def chat(self, messages, *, temperature=0.0, max_tokens=2048) -> str:
        # 关思考模式:实测对当前模型中性(该网关本就不花时间思考),
        # 为将来换带思考模型自动生效预留;网关接受该参数、不报错。
        extra = {"extra_body": {"enable_thinking": False}} if settings.LLM_DISABLE_THINKING else {}
        r = self._cli.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=max_tokens, **extra)
        return r.choices[0].message.content or ""

    def _chat_with_retry(self, messages, *, temperature) -> str:
        """chat + 瞬时错误(连接/超时/429/5xx)指数退避重试。

        非瞬时错误(如鉴权 401、请求体错误)立即抛出不重试;瞬时错误重试
        settings.LLM_RETRY_MAX 次(第 k 次退避 base*2^k 秒),仍失败则抛出最后一个异常——
        由上层 event._one/ugc_sentiment 捕获转成 C1 的显式失败标记,绝不冒充成功。
        """
        last_err = None
        for attempt in range(settings.LLM_RETRY_MAX + 1):
            try:
                return self.chat(messages, temperature=temperature)
            except Exception as e:                    # noqa: BLE001 需按类型/消息二次判定
                if not _is_transient_error(e) or attempt >= settings.LLM_RETRY_MAX:
                    raise
                last_err = e
                delay = settings.LLM_RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning("LLM 瞬时错误(第%d/%d次重试,退避%.2fs):%s",
                               attempt + 1, settings.LLM_RETRY_MAX, delay, str(e)[:60])
                time.sleep(delay)
        raise last_err                                # 理论到不了(循环内已 return/raise)

    def extract(self, text, schema, *, instruction, temperature=0.0) -> dict:
        """结构化抽取:强制 JSON + 解析失败重试;超次数抛错(不静默返空,约法第5条)。

        底层 chat 调用带瞬时错误(连接/超时/限流/5xx)指数退避重试(_chat_with_retry),
        与此处的 JSON 解析重试分层:瞬时故障先被重试压低失败率,仍失败才上抛。
        """
        sys = (f"{instruction}\n"
               f"只输出一个 JSON,不要任何多余文字/解释。JSON 字段与含义:"
               f"{json.dumps(schema, ensure_ascii=False)}")
        last_err = None
        for attempt in range(settings.LLM_MAX_RETRY):
            content = self._chat_with_retry(
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
