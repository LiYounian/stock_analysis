"""开思考臂客户端(A/B 用,只注入不改生产)。

关键发现(统筹已验):同一 deepseek-v4-pro,思考是否激活由**端点**决定——
  - LLM_BASE_URL(=.../qwen/v1):enable_thinking 不生效(秒回、无 reasoning_content)。
  - QWEN_BASE_URL(=.../qwen/compatible-mode/v1):同模型真思考(~23s、有 reasoning_content)。
两 base 用同一把 key(LLM_API_KEY == QWEN_API_KEY)。

本类 = 指向 compatible-mode 端点 + deepseek-v4-pro + 开思考 的 OpenAICompatClient 子类:
  - chat() 覆盖:显式发 extra_body={"enable_thinking": True},并调大 max_tokens
    (思考 token 计入输出,默认 2048 易把最终 JSON 挤断)。
  - 记录每次调用的 reasoning/answer token、时延,供成本/时延维度统计。
  - extract() 沿用父类(父类取 message.content;思考链在 reasoning_content,不污染 content)。
    父类 _extract_json 已足够健壮;若个别票 content 被 <think> 内联污染,记进 stats 再兜底。

不改 registry/settings/生产;model 显式写 deepseek-v4-pro(不用 qwen_max 的 qwen3.8-max)。
"""
from __future__ import annotations

import re
import time

from tools.config import settings
from tools.llm.client import OpenAICompatClient

_THINK_TAG = re.compile(r"<think>.*?</think>", re.S)


class ThinkingClient(OpenAICompatClient):
    def __init__(self, base_url, api_key, model="deepseek-v4-pro",
                 *, timeout=180, max_tokens=4096):
        super().__init__(base_url, api_key, model, timeout=timeout,
                         disable_thinking=False)
        self._max_tokens = max_tokens
        self.stats: list[dict] = []   # 每次调用:latency_s / reasoning_tok / completion_tok / content_polluted

    def chat(self, messages, *, temperature=0.0, max_tokens=None) -> str:
        mt = max_tokens or self._max_tokens
        t0 = time.time()
        r = self._cli.chat.completions.create(
            model=self.model, messages=messages,
            temperature=temperature, max_tokens=mt,
            extra_body={"enable_thinking": True})
        dt = time.time() - t0
        msg = r.choices[0].message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning_content", None) or ""
        # 若网关把思考链内联进 content(<think>...</think>),先剥离再回传(护栏)。
        polluted = bool(_THINK_TAG.search(content))
        if polluted:
            content = _THINK_TAG.sub("", content).strip()
        usage = getattr(r, "usage", None)
        self.stats.append({
            "latency_s": round(dt, 2),
            "reasoning_chars": len(reasoning),
            "reasoning_tok": getattr(usage, "completion_tokens_details", None)
            and getattr(usage.completion_tokens_details, "reasoning_tokens", None),
            "completion_tok": getattr(usage, "completion_tokens", None),
            "prompt_tok": getattr(usage, "prompt_tokens", None),
            "content_polluted": polluted,
            "content_empty": not content,
        })
        return content
