"""_retry.py 单测:瞬时网络错误退避重试语义。"""
import pytest

from tools.collectors._retry import is_transient, retry_call


def test_is_transient_matches_network_errors():
    assert is_transient(ConnectionError("Failed to perform, curl: (56) Connection closed abruptly"))
    assert is_transient(Exception("('Connection aborted.', RemoteDisconnected(...))"))
    assert is_transient(TimeoutError("curl: (28) Operation timed out"))
    # 非网络错误不重试
    assert not is_transient(ValueError("资金流为空"))
    assert not is_transient(KeyError("字段缺失"))


def test_retry_succeeds_after_transient():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("curl: (56) Connection closed abruptly")
        return "OK"

    assert retry_call(flaky, base_delay=0.01) == "OK"
    assert calls["n"] == 3


def test_retry_skips_non_transient():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("空数据")

    with pytest.raises(ValueError):
        retry_call(bad)
    assert calls["n"] == 1                    # 非瞬时 → 不重试


def test_retry_exhausts_and_raises_last():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise ConnectionError("curl: (56) closed")

    with pytest.raises(ConnectionError):
        retry_call(always, attempts=2, base_delay=0.01)
    assert calls["n"] == 2                     # 用尽 attempts 次
