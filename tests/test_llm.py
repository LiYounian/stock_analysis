"""LLM 客户端单测。JSON 解析为纯逻辑(不联网);真实调用用例在未配置 env 时自动跳过。"""
import pytest

from tools.llm import client as lc


def test_extract_json_fenced():
    assert lc._extract_json('```json\n{"a": 1, "b": "x"}\n```') == {"a": 1, "b": "x"}


def test_extract_json_bare_with_text():
    # 模型偶尔在 JSON 前后带闲话 → 仍能截出
    assert lc._extract_json('好的,结果如下:{"score": -1, "label": "利空"} 完毕') == \
        {"score": -1, "label": "利空"}


def test_extract_json_array():
    assert lc._extract_json('```\n[{"x": 1}]\n```') == [{"x": 1}]


def test_extract_json_fail():
    with pytest.raises(ValueError):
        lc._extract_json("这里没有任何 JSON")


def test_get_client_requires_config(monkeypatch):
    """未配置 env → 构造抛清晰错误(不静默)。"""
    monkeypatch.setattr(lc.settings, "LLM_BASE_URL", "")
    monkeypatch.setattr(lc.settings, "LLM_API_KEY", "")
    assert lc.is_configured() is False
    with pytest.raises(RuntimeError):
        lc.get_client()


@pytest.mark.skipif(not lc.is_configured(), reason="LLM env 未配置(需 source ~/.zshrc)")
def test_live_extract():
    cli = lc.get_client()
    r = cli.extract("坐席说:公司今日回购股份 2 亿元。",
                    {"事件类型": "str", "金额": "str", "影响方向": "利好/利空/中性"},
                    instruction="从文本抽取结构化事件。只抄原文,不推断。")
    assert isinstance(r, dict)
    assert r.get("影响方向") in ("利好", "利空", "中性")
