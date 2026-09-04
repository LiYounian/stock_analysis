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


# ---------- C2:瞬时错误重试(连接/超时/429/5xx 指数退避)----------
def _bare_client():
    """绕过 __init__(不需 openai/env)构造一个可注入 chat 的客户端实例。"""
    c = lc.OpenAICompatClient.__new__(lc.OpenAICompatClient)
    c.model = "test-model"
    return c


class _FlakyChat:
    """假 chat:前 fail_times 次抛给定异常,之后返回合法 JSON 文本;计调用次数。"""

    def __init__(self, fail_times, exc, ok='{"影响方向": "利好", "影响强度": 4}'):
        self.calls = 0
        self.fail_times = fail_times
        self.exc = exc
        self.ok = ok

    def __call__(self, messages, *, temperature=0.0):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return self.ok


def test_is_transient_error_classification():
    """连接/超时/429/5xx 判瞬时;鉴权/字段类判非瞬时(据类型/status_code/消息)。"""
    assert lc._is_transient_error(ConnectionError("Connection error."))   # 09-03 活体形态
    assert lc._is_transient_error(TimeoutError("Operation timed out"))
    assert lc._is_transient_error(Exception("HTTP 429 Too Many Requests"))
    assert lc._is_transient_error(Exception("502 Bad Gateway"))
    e503 = Exception("boom"); e503.status_code = 503
    assert lc._is_transient_error(e503)                                    # 5xx status_code
    e400 = Exception("boom"); e400.status_code = 400
    assert not lc._is_transient_error(e400)                               # 4xx(非429)不重试
    assert not lc._is_transient_error(ValueError("字段缺失"))
    assert not lc._is_transient_error(RuntimeError("invalid api key"))


def test_extract_retries_transient_then_succeeds(monkeypatch):
    """瞬时错误一次后成功 → 发生了重试(chat 调 2 次),最终返回可用结果;
    经 news_ai._to_ai 该结果落成 scored:true(不冒充,也不误判失败)。"""
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)             # 不真退避
    monkeypatch.setattr(lc.settings, "LLM_RETRY_MAX", 3)
    c = _bare_client()
    chat = _FlakyChat(1, ConnectionError("Connection error."))
    c.chat = chat
    r = c.extract("正文", {"影响方向": "利好/利空/中性"}, instruction="抽取")
    assert r == {"影响方向": "利好", "影响强度": 4}
    assert chat.calls == 2                                            # 1 失败 + 1 成功 = 重试过

    from tools.analysis import news_ai
    assert news_ai._to_ai(r)["scored"] is True                       # 重试成功 → 真成功态


def test_extract_no_retry_on_non_transient(monkeypatch):
    """非瞬时错误(鉴权/逻辑)立即抛,不重试。"""
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    c = _bare_client()
    chat = _FlakyChat(99, RuntimeError("字段缺失"))
    c.chat = chat
    with pytest.raises(RuntimeError):
        c.extract("t", {"x": "y"}, instruction="i")
    assert chat.calls == 1                                            # 未重试


def test_extract_transient_exhausts_and_raises(monkeypatch):
    """瞬时错误一直不好 → 重试耗尽后抛出(由上层转成 C1 显式失败标记,不冒充成功)。"""
    monkeypatch.setattr(lc.time, "sleep", lambda s: None)
    monkeypatch.setattr(lc.settings, "LLM_RETRY_MAX", 2)
    c = _bare_client()
    chat = _FlakyChat(99, ConnectionError("Connection error."))
    c.chat = chat
    with pytest.raises(Exception):
        c.extract("t", {"x": "y"}, instruction="i")
    assert chat.calls == 3                                            # 1 初次 + 2 重试


@pytest.mark.skipif(not lc.is_configured(), reason="LLM env 未配置(需 source ~/.zshrc)")
def test_live_extract():
    cli = lc.get_client()
    r = cli.extract("坐席说:公司今日回购股份 2 亿元。",
                    {"事件类型": "str", "金额": "str", "影响方向": "利好/利空/中性"},
                    instruction="从文本抽取结构化事件。只抄原文,不推断。")
    assert isinstance(r, dict)
    assert r.get("影响方向") in ("利好", "利空", "中性")
