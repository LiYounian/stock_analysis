"""pull 客户端 kind=pool_pending 落缓冲单测(loopback:TestClient 当 get_fn)。

锁住:pull("pool_pending") 把远端 pending 行整份写本地缓冲(pool_pending.json),
返回 {ok, rows, buffer};kline 路径不回归(仍写主档 + 推水位)。
缓冲路径 monkeypatch 到临时目录,绝不碰真实 data/sync_receipts。
"""
import json
from datetime import datetime

import pandas as pd
import pytest

from tools.config import settings
from tools.store import backend_db, repo
from tools.sync import audit, pull
from tools.sync import pool_pending_store as store

TOKEN = "test-token"
KEY = "test-key"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "_MASTER_DIR", tmp_path / "master")
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setattr(settings, "SYNC_INGEST_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "SYNC_SIGNING_KEY", KEY)
    monkeypatch.setattr(settings, "SYNC_KEY_ID", "k1")
    monkeypatch.setattr(settings, "SYNC_SIGNING_KEY_OLD", "")
    monkeypatch.setattr(settings, "SYNC_KEY_ID_OLD", "k0")
    monkeypatch.setattr(settings, "SYNC_REPLAY_WINDOW_S", 300)
    monkeypatch.setattr(settings, "SYNC_RATE_MAX", 500)
    monkeypatch.setattr(settings, "SYNC_RATE_WINDOW_S", 60)
    # 缓冲/水位路径 → 临时目录(绝不碰真实 data/sync_receipts)
    monkeypatch.setattr(pull, "_receipt_dir", lambda: tmp_path / "receipts")
    backend_db.reset_engine()
    audit.reset_engine()
    store.reset_engine()
    from tools.sync.ingest import _rate
    _rate.reset()
    from fastapi.testclient import TestClient
    from tools.sync.ingest import app
    with TestClient(app) as c:
        yield c
    _rate.reset()
    backend_db.reset_engine()
    audit.reset_engine()
    store.reset_engine()


def _loopback_get_fn(client):
    def get_fn(url, params, headers):
        r = client.get(url, params=params, headers=headers)
        return r.status_code, r.json()
    return get_fn


def test_pull_pool_pending_writes_buffer(client, tmp_path):
    store.enqueue(code="600000", name="浦发", sector="银行", op="add")
    store.enqueue(code="000001", op="remove")
    res = pull.pull("pool_pending", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    get_fn=_loopback_get_fn(client), retries=0, sleep_fn=lambda *_: None)
    assert res["ok"] is True and res["rows"] == 2
    buf = tmp_path / "receipts" / "pool_pending.json"
    assert buf.exists()
    obj = json.loads(buf.read_text(encoding="utf-8"))
    assert {r["code"] for r in obj["rows"]} == {"600000", "000001"}
    assert obj["pulled_at"]


def test_pull_pool_pending_empty_ok(client, tmp_path):
    """无 pending 时也成功落空缓冲(rows=0)。"""
    res = pull.pull("pool_pending", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    get_fn=_loopback_get_fn(client), retries=0, sleep_fn=lambda *_: None)
    assert res["ok"] is True and res["rows"] == 0
    obj = json.loads((tmp_path / "receipts" / "pool_pending.json").read_text(encoding="utf-8"))
    assert obj["rows"] == []


def test_pull_pool_pending_overwrites_not_appends(client, tmp_path):
    """整份覆盖:第二次拉取(pending 已变少)缓冲被覆盖,不残留旧行。"""
    a = store.enqueue(code="600000", op="add")
    store.enqueue(code="000001", op="add")
    gf = _loopback_get_fn(client)
    pull.pull("pool_pending", url="/pull", token=TOKEN, key=KEY, key_id="k1",
              get_fn=gf, retries=0, sleep_fn=lambda *_: None)
    store.mark_consumed([a])                                  # a 消化,只剩 1 条 pending
    pull.pull("pool_pending", url="/pull", token=TOKEN, key=KEY, key_id="k1",
              get_fn=gf, retries=0, sleep_fn=lambda *_: None)
    obj = json.loads((tmp_path / "receipts" / "pool_pending.json").read_text(encoding="utf-8"))
    assert {r["code"] for r in obj["rows"]} == {"000001"}     # 覆盖,不含已消化的 600000


def test_pull_kline_still_works(client, tmp_path):
    """回归:kline 路径不受影响(写主档 + 推水位)。"""
    repo.put_master_kline("000001", pd.DataFrame({
        "date": pd.to_datetime(["2026-08-06", "2026-08-07"]),
        "open": [10.0, 11.0], "high": [10.0, 11.0], "low": [10.0, 11.0],
        "close": [10.0, 11.0], "volume": [100, 100]}))
    wm = tmp_path / "pull_kline.json"
    res = pull.pull("kline", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    since="", get_fn=_loopback_get_fn(client), watermark_path=wm,
                    retries=0, sleep_fn=lambda *_: None)
    assert res["ok"] is True and res["codes_written"] == 1
    assert res["max_date"] == "2026-08-07"
