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
    """OpenAI 兼容客户端(DeepSeek / 千问网关等,均走此实现,由注册表 model/凭证区分)。

    timeout / disable_thinking 支持按 provider 覆盖(缺省 None → 沿用 settings 全局默认,
    保证注册表未指定时行为与改动前完全一致)。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 *, timeout: int | None = None, disable_thinking: bool | None = None):
        if not base_url or not api_key:
            raise RuntimeError(
                "LLM 未配置:请在环境变量设置对应 provider 的 base_url + api_key "
                "(只在本机 shell,不入库;变量名见 tools/config/model_registry.yaml)。")
        from openai import OpenAI
        self._timeout = timeout if timeout is not None else settings.LLM_TIMEOUT
        self._disable_thinking = (
            disable_thinking if disable_thinking is not None else settings.LLM_DISABLE_THINKING)
        self._cli = OpenAI(api_key=api_key, base_url=base_url, timeout=self._timeout)
        self.model = model

    def chat(self, messages, *, temperature=0.0, max_tokens=2048) -> str:
        # 关思考模式:实测对当前模型中性(该网关本就不花时间思考),
        # 为将来换带思考模型自动生效预留;网关接受该参数、不报错。
        extra = {"extra_body": {"enable_thinking": False}} if self._disable_thinking else {}
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


def _build_client(spec, *, enable_thinking: bool | None = None) -> LLMClient:
    """按 ProviderSpec.kind 构造对应 client。

    P1 只实现 openai_compat(DeepSeek / 千问网关皆走此);其余 kind(如 anthropic)
    显式抛错,不静默降级——避免"路由到未实现 provider 却假装成功"。
    timeout / enable_thinking 从注册表 params 取,缺省则沿用 settings 全局默认。

    enable_thinking(可选覆盖):双跑框架的 think A/B 用——传 True/False 覆盖注册表 params,
    None 表示不覆盖(沿用注册表 params → settings 默认)。
    """
    if spec.kind == "openai_compat":
        params = spec.params or {}
        timeout = params.get("timeout")
        et = enable_thinking if enable_thinking is not None else params.get("enable_thinking")
        disable_thinking = (not et) if et is not None else None
        return OpenAICompatClient(
            spec.resolve_base_url(), spec.resolve_api_key(), spec.model,
            timeout=timeout, disable_thinking=disable_thinking)
    raise NotImplementedError(
        f"provider {spec.id!r} kind={spec.kind!r} 尚未实现(P1 只支持 openai_compat;"
        f"anthropic 等留待后续波次)。")


def get_client(purpose: str = "extract") -> LLMClient:
    """工厂:按注册表路由表 purpose → 主 provider,返回对应 client。

    P1(行为零变化):注册表所有 purpose 的 primary 都指向现 DeepSeek,故任意 purpose
    的返回与改动前一致;千问已注册但暂不默认路由。fallback 链在注册表里是设计意向数据,
    P1 **不执行跨 provider 自动降级**(避免静默换模型掩盖故障)。

    健壮性:注册表加载/解析异常时,回退到旧的固定 DeepSeek 构造(降级不崩,约法第5条),
    保证 P1 严格不劣于改动前。
    """
    try:
        from tools.config import model_registry as mr
        spec = mr.primary_spec_for(purpose)
        return _build_client(spec)
    except Exception as e:                        # noqa: BLE001 注册表异常一律回退旧路径
        logger.warning("模型注册表路由失败(purpose=%s),回退固定 DeepSeek 构造:%s",
                       purpose, str(e)[:120])
        return OpenAICompatClient(settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL)


def get_client_for(provider_id: str, *, enable_thinking: bool | None = None) -> LLMClient:
    """按 provider **id** 直接构造 client(绕过 purpose 路由)。

    双跑对比框架(§3)用:需显式指定"跑哪个 provider"作为对比两臂(如 DeepSeek vs 千问),
    以及 think A/B(同 provider,enable_thinking 开/关)。与 get_client 的区别 = 不看 routes、
    直接按 registry 里声明的 provider 取 spec。**additive,不改 get_client 现有行为**。

    provider_id 不在注册表 → 抛 KeyError(fail loud,不静默降级);注册表加载失败原样上抛
    (双跑是研发/验证工具,故障要显式暴露,不像日更主链路那样兜底回退)。
    """
    from tools.config import model_registry as mr
    spec = mr.load_registry().spec_for(provider_id)
    return _build_client(spec, enable_thinking=enable_thinking)


def is_configured() -> bool:
    """LLM 是否已配置(env 就绪)。供采集/分析层判断是否降级。"""
    return bool(settings.LLM_BASE_URL and settings.LLM_API_KEY)
