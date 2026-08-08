"""采集健壮性 + 超时收敛单测(P1/P2)。

P1:单个数据源失败/空 → 降级跳过,collect_* 绝不抛;政策拉空不再 raise。
P2:采集期设进程级 socket 超时并在结束还原;curl_cffi 采集器超时 ≤10s。
"""
import socket

import pandas as pd
import pytest

from tools import run


# —— P1:_safe 吞异常 ——
def test_safe_returns_value_or_none():
    assert run._safe("ok", lambda: 42) == 42

    def boom():
        raise RuntimeError("blocked")
    assert run._safe("bad", boom) is None


# —— P1:单源抛错时 collect_* 不中止 ——
def test_collect_values_degrades_on_source_error(monkeypatch):
    monkeypatch.setattr(run.master_sync, "sync_master", lambda c, **k: {"mode": "spot", "ok": 1})
    def boom(c):
        raise RuntimeError("东财被墙")
    monkeypatch.setattr(run.fd, "fetch_fundamental", boom)      # 基本面炸
    monkeypatch.setattr(run.an, "fetch_announcements", lambda c: {})
    monkeypatch.setattr(run.ff, "fetch_fundflow", lambda c: {})
    run.collect_values(["000001"])                              # 不应抛


def test_collect_message_degrades_on_policy_error(monkeypatch):
    monkeypatch.setattr(run.news, "fetch_news", lambda c: {})
    monkeypatch.setattr(run.ugc, "fetch_ugc", lambda c: {})
    def boom(*a, **k):
        raise RuntimeError("政策接口异常")
    monkeypatch.setattr(run.policy, "fetch_policy", boom)       # 政策炸
    run.collect_message(["000001"])                            # 不应抛


# —— P1:政策两源皆空 → 降级为空,不 raise ——
def test_fetch_policy_empty_degrades_no_raise(monkeypatch, tmp_path):
    from tools.collectors import policy
    from tools.store import repo
    monkeypatch.setattr(repo, "_RAW_DIR", tmp_path / "raw")
    repo.set_active_date("2026-08-08")
    monkeypatch.setattr(policy, "_fetch_em", lambda kw: pd.DataFrame())   # 主源空
    monkeypatch.setattr(policy, "_collect_cctv", lambda days: [])          # 备源空
    out = policy.fetch_policy(keywords=["半导体 政策"], days=3)
    assert out == []                                           # 降级为空,而非抛
    repo.set_active_date(None)


# —— P2:采集期设 socket 超时,结束还原 ——
# 注:K线走 master_sync(主档/spot,不套此短超时——见 collect_values 注释),故超时语义
#     现由基本面/公告/资金流承载;这里在 fetch_fundamental 内捕获当时的 socket 超时。
def test_collect_values_sets_and_restores_socket_timeout(monkeypatch):
    captured = {}
    def cap(c):
        captured["t"] = socket.getdefaulttimeout()
        return {}
    monkeypatch.setattr(run.master_sync, "sync_master", lambda c, **k: {"mode": "spot", "ok": 1})
    monkeypatch.setattr(run.fd, "fetch_fundamental", cap)
    monkeypatch.setattr(run.an, "fetch_announcements", lambda c: {})
    monkeypatch.setattr(run.ff, "fetch_fundflow", lambda c: {})
    socket.setdefaulttimeout(None)
    run.collect_values(["x"])
    assert captured["t"] == run.FETCH_TIMEOUT                  # 采集期已设超时
    assert socket.getdefaulttimeout() is None                 # 采集后还原


def test_collect_values_restores_timeout_even_on_error(monkeypatch):
    def boom(c):
        raise RuntimeError("x")
    monkeypatch.setattr(run.master_sync, "sync_master", lambda c, **k: {"mode": "spot", "ok": 1})
    monkeypatch.setattr(run.fd, "fetch_fundamental", boom)    # 基本面炸(在超时块内)
    monkeypatch.setattr(run.an, "fetch_announcements", lambda c: {})
    monkeypatch.setattr(run.ff, "fetch_fundflow", lambda c: {})
    socket.setdefaulttimeout(None)
    run.collect_values(["x"])
    assert socket.getdefaulttimeout() is None                 # finally 保证还原


# —— P2:curl_cffi 采集器超时 ≤10s ——
def test_curl_timeouts_bounded():
    from tools.collectors import fundflow, ugc
    assert isinstance(fundflow._TIMEOUT, float) and fundflow._TIMEOUT <= 10
    assert isinstance(ugc._TIMEOUT, float) and ugc._TIMEOUT <= 10
    assert run.FETCH_TIMEOUT <= 10
