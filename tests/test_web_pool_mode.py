"""web/app.py 票池增删双路由按 POOL_WRITE_MODE 分流单测。

锁住:direct(本地默认)→ 走 add_and_collect/remove_and_cleanup(现状逐字节不变);
enqueue(远端)→ 走 enqueue_pending 只入队(不采集不重建)。两路由(add + delete)都覆盖。
mock pool_service,不真采集/不真写库。
"""
import pytest
from fastapi.testclient import TestClient

from tools.config import settings
from tools import pool_service
from web.app import app

client = TestClient(app)


@pytest.fixture
def spy(monkeypatch):
    calls = {"add_and_collect": 0, "remove_and_cleanup": 0, "enqueue_pending": []}

    def add_and_collect(code, name, industry, sector, market="A"):
        calls["add_and_collect"] += 1
        return {"stock": {"code": code}, "date": "2026-08-27", "collected": {}}

    def remove_and_cleanup(code):
        calls["remove_and_cleanup"] += 1
        return {"stock": {"code": code}, "removed_files": 0}

    def enqueue_pending(op, code, **kw):
        calls["enqueue_pending"].append((op, code))
        return {"ok": True, "queued": {"id": 1, "op": op, "code": code, "market": kw.get("market", "A")},
                "note": "已提交"}

    monkeypatch.setattr(pool_service, "add_and_collect", add_and_collect)
    monkeypatch.setattr(pool_service, "remove_and_cleanup", remove_and_cleanup)
    monkeypatch.setattr(pool_service, "enqueue_pending", enqueue_pending)
    return calls


def _add_body():
    return {"code": "600000", "name": "浦发", "industry": "银行", "sector": "银行", "market": "A"}


def test_add_direct_mode_collects(spy, monkeypatch):
    monkeypatch.setattr(settings, "POOL_WRITE_MODE", "direct")
    r = client.post("/api/pool", json=_add_body())
    assert r.status_code == 200 and r.json()["mode"] == "direct"
    assert spy["add_and_collect"] == 1                     # 走直采
    assert spy["enqueue_pending"] == []                    # 没入队


def test_add_enqueue_mode_queues_no_collect(spy, monkeypatch):
    monkeypatch.setattr(settings, "POOL_WRITE_MODE", "enqueue")
    r = client.post("/api/pool", json=_add_body())
    assert r.status_code == 200 and r.json()["mode"] == "enqueue"
    assert spy["enqueue_pending"] == [("add", "600000")]   # 只入队
    assert spy["add_and_collect"] == 0                     # 不采集


def test_delete_direct_mode_cleans(spy, monkeypatch):
    monkeypatch.setattr(settings, "POOL_WRITE_MODE", "direct")
    r = client.post("/api/pool/600000/delete")
    assert r.status_code == 200 and r.json()["mode"] == "direct"
    assert spy["remove_and_cleanup"] == 1
    assert spy["enqueue_pending"] == []


def test_delete_enqueue_mode_queues_no_cleanup(spy, monkeypatch):
    monkeypatch.setattr(settings, "POOL_WRITE_MODE", "enqueue")
    r = client.post("/api/pool/600000/delete")
    assert r.status_code == 200 and r.json()["mode"] == "enqueue"
    assert spy["enqueue_pending"] == [("remove", "600000")]
    assert spy["remove_and_cleanup"] == 0


def test_add_direct_validation_error_400(spy, monkeypatch):
    """direct 模式校验失败仍返回 400(现状不回归)。"""
    monkeypatch.setattr(settings, "POOL_WRITE_MODE", "direct")

    def boom(*a, **k):
        raise ValueError("代码非法")
    monkeypatch.setattr(pool_service, "add_and_collect", boom)
    r = client.post("/api/pool", json=_add_body())
    assert r.status_code == 400 and r.json()["ok"] is False
