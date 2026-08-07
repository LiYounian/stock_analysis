"""展示端 ingest 服务单测(FastAPI TestClient)。

锁住五关 + 落库 + 审计:
  无 token→401 / 篡改→403 / 过期 ts→409 / 重放 nonce→409 / 未来日期→422 /
  过老日期→422 / 旧盖新→409 / 契约不合规→422 / 合法→200 且落库 / 幂等 / 每次必审计。
DB 与审计库指向临时 SQLite,前后 reset_engine 隔离,绝不碰真实库。
"""
from datetime import datetime, timedelta

import pytest

from tools.config import settings
from tools.store import backend_db
from tools.sync import audit, sign

TOKEN = "test-token"
KEY = "test-key"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _valid_rec(code: str) -> dict:
    return {"schema_version": "1.0", "meta": {"code": code, "name": f"票{code}"},
            "events": [], "timeseries_refs": {}, "provenance": {}}


def _env(records=None, views=None, code_views=None, *, date=None, gen=None,
         ts=None, nonce="n-1", key=KEY, key_id="k1"):
    records = records if records is not None else {"000021": _valid_rec("000021")}
    e = {"meta": {"date": date or _today(), "generated_at": gen or _now_iso(),
                  "source": "local", "ts": ts or _now_iso(), "nonce": nonce,
                  "key_id": key_id, "sig_alg": "HMAC-SHA256"},
         "records": records, "views": views or {}, "code_views": code_views or {}}
    e["meta"]["sig"] = sign.sign_envelope(e, key)
    return e


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setattr(settings, "SYNC_INGEST_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "SYNC_SIGNING_KEY", KEY)
    monkeypatch.setattr(settings, "SYNC_KEY_ID", "k1")
    monkeypatch.setattr(settings, "SYNC_SIGNING_KEY_OLD", "")
    monkeypatch.setattr(settings, "SYNC_KEY_ID_OLD", "k0")
    monkeypatch.setattr(settings, "SYNC_REPLAY_WINDOW_S", 300)
    monkeypatch.setattr(settings, "SYNC_MAX_AGE_DAYS", 90)
    monkeypatch.setattr(settings, "SYNC_RATE_MAX", 120)          # 硬化:限流上限(单测内够宽)
    monkeypatch.setattr(settings, "SYNC_RATE_WINDOW_S", 60)
    monkeypatch.setattr(settings, "SYNC_MAX_BODY_BYTES", 32 * 1024 * 1024)
    backend_db.reset_engine()
    audit.reset_engine()
    from tools.sync.ingest import _rate
    _rate.reset()                                                # 隔离限流器状态
    from fastapi.testclient import TestClient
    from tools.sync.ingest import app
    with TestClient(app) as c:
        yield c
    _rate.reset()
    backend_db.reset_engine()
    audit.reset_engine()


def _post(client, env, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return client.post("/ingest", json=env, headers=headers)


# —— ① 鉴权 ——
def test_no_token_401(client):
    before = audit.audit_count()
    r = _post(client, _env(), token=None)
    assert r.status_code == 401
    assert audit.audit_count() == before + 1     # 失败也要审计


def test_wrong_token_401(client):
    assert _post(client, _env(), token="nope").status_code == 401


# —— ② 验签 ——
def test_tampered_payload_403(client):
    env = _env()
    env["records"]["000021"]["meta"]["name"] = "篡改"   # 签名后改字节
    assert _post(client, env).status_code == 403


# —— ③ 防重放 ——
def test_expired_ts_rejected(client):
    old = (datetime.now().astimezone() - timedelta(seconds=1000)).isoformat(timespec="seconds")
    assert _post(client, _env(ts=old)).status_code == 409


def test_replay_nonce_rejected(client):
    env = _env(nonce="dup")
    assert _post(client, env).status_code == 200
    env2 = _env(nonce="dup")                      # 同 nonce 再来 → 重放
    assert _post(client, env2).status_code == 409


# —— ④ 时效 ——
def test_future_date_422(client):
    fut = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    assert _post(client, _env(date=fut)).status_code == 422


def test_too_old_date_422(client):
    old = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    assert _post(client, _env(date=old)).status_code == 422


def test_old_generated_at_over_new_409(client):
    new = "2026-08-07T12:00:00+08:00"
    older = "2026-08-07T08:00:00+08:00"
    assert _post(client, _env(gen=new, nonce="a")).status_code == 200
    # 同日期、更旧的 generated_at → 拒绝旧盖新
    assert _post(client, _env(gen=older, nonce="b")).status_code == 409


# —— ⑤ 契约 ——
def test_invalid_record_422(client):
    bad = {"000021": {"meta": {"code": "000021"}}}   # 缺必需顶层块
    assert _post(client, _env(records=bad)).status_code == 422


# —— 合法 + 落库 + 幂等 ——
def test_valid_ingests_and_persists(client):
    env = _env(views={"panel": {"rows": [{"代码": "000021"}]}},
               code_views={"chart": {"000021": {"close": [1.0]}}}, nonce="ok1")
    r = _post(client, env)
    assert r.status_code == 200 and r.json()["ok"] is True
    d = _today()
    assert backend_db.get_record("000021", d)["meta"]["name"] == "票000021"
    assert backend_db.get_view("panel", d)["rows"][0]["代码"] == "000021"
    assert backend_db.get_code_view("chart", "000021", d)["close"] == [1.0]


def test_idempotent_upsert_no_dup(client):
    _post(client, _env(nonce="i1"))
    _post(client, _env(nonce="i2"))              # 同数据同日期、不同 nonce 重传
    d = _today()
    assert len(list(backend_db.iter_records(d))) == 1   # 不重复


def test_every_request_audited(client):
    before = audit.audit_count()
    _post(client, _env(nonce="x1"))              # 成功
    _post(client, _env(), token="bad")           # 失败
    assert audit.audit_count() == before + 2     # 成功+失败各一条


# —— 硬化:限流 429 ——
def test_rate_limit_429(client, monkeypatch):
    monkeypatch.setattr(settings, "SYNC_RATE_MAX", 3)            # 窗口内最多 3 次
    from tools.sync.ingest import _rate
    _rate.reset()
    codes = [_post(client, _env(nonce=f"r{i}")).status_code for i in range(5)]
    assert codes.count(429) >= 1                                 # 超过 3 次后被限流
    assert codes[:3] == [200, 200, 200] or 429 in codes[3:]      # 前几次放行,后面 429


def test_rate_limit_audited(client, monkeypatch):
    monkeypatch.setattr(settings, "SYNC_RATE_MAX", 1)
    from tools.sync.ingest import _rate
    _rate.reset()
    _post(client, _env(nonce="a"))                              # 放行
    before = audit.audit_count()
    r = _post(client, _env(nonce="b"))                         # 被限流
    assert r.status_code == 429
    assert audit.audit_count() == before + 1                    # 限流也审计
    assert audit.last_audit()["result"] == "rate"


# —— 硬化:请求体上限 413 ——
def test_body_too_large_413(client, monkeypatch):
    monkeypatch.setattr(settings, "SYNC_MAX_BODY_BYTES", 500)   # 上限 500 字节
    big = _env(records={"000021": _valid_rec("000021")})
    big["records"]["000021"]["padding"] = "x" * 2000            # 撑大 payload(签名前加,验签不影响 413 先判)
    big["meta"]["sig"] = sign.sign_envelope(big, KEY)
    r = _post(client, big)
    assert r.status_code == 413


# —— 硬化:只读审计查询 ——
def test_audit_endpoint_requires_token(client):
    assert client.get("/audit").status_code == 401              # 无 token
    _post(client, _env(nonce="q1"))                            # 造一条审计
    r = client.get("/audit", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(r.json()["rows"]) >= 1


def test_audit_view_html(client):
    _post(client, _env(nonce="q2"))
    assert client.get("/audit/view").status_code == 401         # 无 token
    r = client.get(f"/audit/view?token={TOKEN}")               # 查询参数带 token
    assert r.status_code == 200 and "<table" in r.text
    assert audit.last_audit()["result"] in ("ok", "auth_fail")
