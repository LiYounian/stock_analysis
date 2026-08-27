"""ingest /pull?kind=pool_pending 门禁 + __pool_ack__ 回执标 consumed 单测(loopback TestClient)。

/pull pool_pending 锁住:无 token→401 / 未签名→403 / 错密钥→403 / 合法→200 且返 pending 行。
__pool_ack__ 锁住:搭 /ingest 完整签名门禁上送 consumed_ids → 对应 pending 变 consumed;
  ack 分片不作为普通 view 落库;幂等(重复 ack 无副作用)。
全程 temp sqlite(monkeypatch DB_URL + 各 reset_engine),绝不碰真实库。
"""
from datetime import datetime

import pytest

from tools.config import settings
from tools.store import backend_db, repo
from tools.sync import audit, sign
from tools.sync import pool_pending_store as store

TOKEN = "test-token"
KEY = "test-key"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


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


def _pull_headers(kind, since, codes, *, key=KEY, key_id="k1", ts=None):
    ts = ts or _now_iso()
    env = sign.pull_envelope(kind, since or "", codes or "", ts, "n-pull-1", key_id)
    sig = sign.sign_envelope(env, key)
    return {"X-Sync-Ts": ts, "X-Sync-Nonce": "n-pull-1", "X-Sync-Key-Id": key_id, "X-Sync-Sig": sig}


def _get(client, *, token=TOKEN, headers_extra=None):
    h = dict(headers_extra or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return client.get("/pull", params={"kind": "pool_pending", "since": "", "codes": ""}, headers=h)


# ———— /pull?kind=pool_pending 门禁 ————
def test_pull_pending_no_token_401(client):
    assert _get(client, token=None, headers_extra=_pull_headers("pool_pending", "", "")).status_code == 401


def test_pull_pending_unsigned_403(client):
    assert _get(client).status_code == 403


def test_pull_pending_bad_key_403(client):
    assert _get(client, headers_extra=_pull_headers("pool_pending", "", "", key="wrong")).status_code == 403


def test_pull_pending_valid_returns_rows(client):
    store.enqueue(code="600000", name="浦发", sector="银行", op="add")
    store.enqueue(code="000001", op="remove")
    r = _get(client, headers_extra=_pull_headers("pool_pending", "", ""))
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "pool_pending" and body["count"] == 2
    codes = {row["code"] for row in body["data"]}
    assert codes == {"600000", "000001"}
    # 安全红线:返回列不含任何密钥/配置字段
    assert set(body["data"][0].keys()) <= {
        "id", "code", "name", "industry", "sector", "market", "op",
        "source", "requested_at", "status", "consumed_at"}


def test_pull_pending_only_pending_status(client):
    a = store.enqueue(code="600000", op="add")
    store.enqueue(code="000001", op="add")
    store.mark_consumed([a])                                 # a 已消化
    r = _get(client, headers_extra=_pull_headers("pool_pending", "", ""))
    assert {row["code"] for row in r.json()["data"]} == {"000001"}   # 只返 pending


# ———— __pool_ack__ 回执经 /ingest 标 consumed ————
def _ingest_ack_envelope(consumed_ids, *, nonce="n-ack-1"):
    date = datetime.now().strftime("%Y-%m-%d")
    meta = {"date": date, "source": "local", "key_id": "k1",
            "generated_at": _now_iso(), "sig_alg": sign.SIG_ALG,
            "ts": _now_iso(), "nonce": nonce}
    env = {"meta": meta, "records": {},
           "views": {"__pool_ack__": {"consumed_ids": consumed_ids}}, "code_views": {}}
    env["meta"]["sig"] = sign.sign_envelope(env, KEY)
    return env


def _post_ingest(client, env):
    return client.post("/ingest", json=env, headers={"Authorization": f"Bearer {TOKEN}"})


def test_ingest_ack_marks_consumed(client):
    a = store.enqueue(code="600000", op="add")
    b = store.enqueue(code="000001", op="add")
    r = _post_ingest(client, _ingest_ack_envelope([a, b]))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.count(status="pending") == 0                # 两条都 consumed
    assert store.count(status="consumed") == 2


def test_ingest_ack_not_persisted_as_view(client):
    """__pool_ack__ 不落库为普通 view(被摘除)。"""
    a = store.enqueue(code="600000", op="add")
    _post_ingest(client, _ingest_ack_envelope([a]))
    date = datetime.now().strftime("%Y-%m-%d")
    with pytest.raises(FileNotFoundError):                     # 未作为 view 落库(被摘除)
        backend_db.get_view("__pool_ack__", date)


def test_ingest_ack_idempotent(client):
    a = store.enqueue(code="600000", op="add")
    _post_ingest(client, _ingest_ack_envelope([a], nonce="n-ack-A"))
    # 再上送一次(换 nonce 过防重放),幂等无副作用
    _post_ingest(client, _ingest_ack_envelope([a], nonce="n-ack-B"))
    assert store.count(status="consumed") == 1
    assert store.count(status="pending") == 0


def test_ingest_ack_requires_auth(client):
    """未鉴权的 ack 不能标 consumed(复用 /ingest 门禁,不裸开写口)。"""
    a = store.enqueue(code="600000", op="add")
    r = client.post("/ingest", json=_ingest_ack_envelope([a]))   # 无 token
    assert r.status_code == 401
    assert store.count(status="pending") == 1                    # 未被标记
