"""ops.consume_pool_pending 本地闭环集成测(mock pull + pool_service)。

锁住:pull→裁决→执行→consumed_ids 全链;**失败项保留 pending 不进 consumed_ids**(下轮重试);
noop 立即 consumed;dry-run 只裁决不执行;回执文件写 consumed_ids;pull 失败则不执行。
不触网、不真采集、不真写库(缓冲/回执路径 monkeypatch 到临时目录)。
"""
import json

import pytest

from ops import consume_pool_pending as cpp
from tools import pool_service
from tools.config import stock_pool


class _S:
    def __init__(self, code, market="A", added_at=""):
        self.code, self.market, self.added_at = code, market, added_at


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(cpp, "_receipt_dir", lambda: tmp_path)
    # 本地池现状:已有 600000(2026-08-20 加)
    monkeypatch.setattr(stock_pool, "reload", lambda: None)
    monkeypatch.setattr(stock_pool, "get_pool",
                        lambda: [_S("600000", "A", "2026-08-20T00:00:00+08:00")])
    return tmp_path


def _write_buffer(tmp_path, rows):
    (tmp_path / "pool_pending.json").write_text(
        json.dumps({"pulled_at": "t", "rows": rows}, ensure_ascii=False), encoding="utf-8")


def _ok_pull(*a, **k):
    return {"ok": True, "rows": 0}


ROWS = [
    {"id": 1, "code": "000001", "name": "平安银行", "industry": "银行", "sector": "银行",
     "market": "A", "op": "add", "requested_at": "2026-08-27T09:00:00+08:00"},
    {"id": 2, "code": "600000", "op": "add", "market": "A",
     "requested_at": "2026-08-27T09:01:00+08:00"},                       # 已存在 → noop
    {"id": 3, "code": "600000", "op": "remove", "market": "A",
     "requested_at": "2026-08-27T10:00:00+08:00"},                       # 晚于本地 add → 删
]


def test_full_flow_executes_and_consumes_all(env, monkeypatch):
    _write_buffer(env, ROWS)
    calls = {"add": [], "remove": []}
    monkeypatch.setattr(pool_service, "add_and_collect",
                        lambda code, *a, **k: calls["add"].append(code) or {"ok": True})
    monkeypatch.setattr(pool_service, "remove_and_cleanup",
                        lambda code: calls["remove"].append(code) or {"ok": True})

    res = cpp.consume(url="/pull", token="t", key="K", key_id="k1", pull_fn=_ok_pull)
    assert res["ok"] is True and res["pulled"] == 3
    assert calls["add"] == ["000001"]                    # 只采新票
    assert calls["remove"] == ["600000"]                 # 删更晚的
    assert set(res["consumed_ids"]) == {1, 2, 3}         # 全部裁决完毕 → consumed
    assert res["failed"] == []
    # 回执文件写了 consumed_ids
    assert set(cpp.read_ack(env / "pool_ack.json")) == {1, 2, 3}


def test_failed_add_kept_pending(env, monkeypatch):
    """采集失败的行不进 consumed_ids(保留 pending 下轮重试),进 failed。"""
    _write_buffer(env, ROWS)

    def boom_add(code, *a, **k):
        raise RuntimeError("采集超时")
    monkeypatch.setattr(pool_service, "add_and_collect", boom_add)
    monkeypatch.setattr(pool_service, "remove_and_cleanup", lambda code: {"ok": True})

    res = cpp.consume(url="/pull", token="t", key="K", key_id="k1", pull_fn=_ok_pull)
    assert 1 not in res["consumed_ids"]                  # 失败的 add(id=1)不 consumed
    assert {2, 3} <= set(res["consumed_ids"])            # noop(2)+ 成功 remove(3)照常 consumed
    assert len(res["failed"]) == 1 and res["failed"][0]["id"] == 1
    assert res["failed"][0]["op"] == "add"


def test_dry_run_no_execution(env, monkeypatch):
    _write_buffer(env, ROWS)
    def boom(*a, **k):
        raise AssertionError("dry-run 不应执行采集/删除")
    monkeypatch.setattr(pool_service, "add_and_collect", boom)
    monkeypatch.setattr(pool_service, "remove_and_cleanup", boom)

    res = cpp.consume(url="/pull", token="t", key="K", key_id="k1", dry_run=True, pull_fn=_ok_pull)
    assert res["dry_run"] is True
    assert res["to_add"] == ["000001"] and res["to_remove"] == ["600000"]
    assert res["noop_consumed_ids"] == [2]
    assert not (env / "pool_ack.json").exists()          # dry-run 不写回执


def test_pull_failure_skips_execution(env, monkeypatch):
    _write_buffer(env, ROWS)
    def boom(*a, **k):
        raise AssertionError("pull 失败不应执行")
    monkeypatch.setattr(pool_service, "add_and_collect", boom)
    monkeypatch.setattr(pool_service, "remove_and_cleanup", boom)

    res = cpp.consume(url="/pull", token="t", key="K", key_id="k1",
                      pull_fn=lambda *a, **k: {"ok": False, "error": "远端不可达"})
    assert res["ok"] is False and res["consumed_ids"] == []


def test_empty_buffer_is_noop(env, monkeypatch):
    """无缓冲文件(或空)→ 拉 0、不执行、回执空。"""
    monkeypatch.setattr(pool_service, "add_and_collect", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(pool_service, "remove_and_cleanup", lambda code: {"ok": True})
    res = cpp.consume(url="/pull", token="t", key="K", key_id="k1", pull_fn=_ok_pull)
    assert res["ok"] is True and res["pulled"] == 0
    assert res["consumed_ids"] == []
