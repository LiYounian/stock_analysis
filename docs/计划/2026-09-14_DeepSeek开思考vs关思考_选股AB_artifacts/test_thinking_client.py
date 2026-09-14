"""锁住 ThinkingClient 语义(为什么这么写):
- 必须发 extra_body enable_thinking=True(端点激活思考的开关)。
- max_tokens 必须被调大(默认 2048 会被 reasoning 挤断最终 JSON)。
- 只取 message.content(思考链在 reasoning_content,不得污染答案)。
- content 内联 <think> 时必须剥离(网关兜底护栏)。
- 每次调用统计 reasoning/completion token + 时延 + 污染/截空标记。
无网络:替换底层 openai client 为桩。用法:PYTHONPATH=<worktree> python -m pytest test_thinking_client.py
"""
from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from thinking_client import ThinkingClient


class _FakeCompletions:
    def __init__(self, content, reasoning):
        self.content, self.reasoning = content, reasoning
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        msg = SimpleNamespace(content=self.content, reasoning_content=self.reasoning)
        usage = SimpleNamespace(
            completion_tokens=1866, prompt_tokens=2620,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=1527))
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def _client(content="{\"stance\":\"观望\"}", reasoning="想了很多"):
    c = ThinkingClient("http://x", "k", model="deepseek-v4-pro")
    fake = _FakeCompletions(content, reasoning)
    c._cli = SimpleNamespace(chat=SimpleNamespace(completions=fake))
    return c, fake


def test_sends_enable_thinking_and_bumped_max_tokens():
    c, fake = _client()
    c.chat([{"role": "user", "content": "hi"}])
    kw = fake.calls[0]
    assert kw["extra_body"] == {"enable_thinking": True}
    assert kw["max_tokens"] == 4096            # 默认调大,防思考挤断 JSON
    assert kw["model"] == "deepseek-v4-pro"


def test_returns_only_content_not_reasoning():
    c, _ = _client(content="{\"a\":1}", reasoning="CHAIN-OF-THOUGHT")
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "{\"a\":1}"
    assert "CHAIN-OF-THOUGHT" not in out


def test_strips_inlined_think_tag():
    c, _ = _client(content="<think>abc</think>{\"a\":1}", reasoning="")
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "{\"a\":1}"
    assert c.stats[-1]["content_polluted"] is True


def test_stats_recorded():
    c, _ = _client()
    c.chat([{"role": "user", "content": "hi"}])
    s = c.stats[-1]
    assert s["reasoning_tok"] == 1527 and s["completion_tok"] == 1866
    assert s["prompt_tok"] == 2620 and s["content_empty"] is False
    assert s["latency_s"] >= 0


def test_disable_thinking_off():
    c, _ = _client()
    assert c._disable_thinking is False        # 开思考臂:disable=False


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python", "-m", "pytest", "-q", __file__]))
