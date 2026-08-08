"""远端数据仓库 Phase 1 单测:ingest GET /pull 门禁 + 本地 pull 客户端 round-trip。

/pull 锁住:无 token→401 / 未签名→403 / 错密钥→403 / 过期签名→409 / since 非法→422 /
不支持 kind→422 / 合法→200 且只回 date>since 的增量;NaN→null。
pull 客户端锁住(loopback,用 TestClient 当 get_fn):写本地主档幂等 append + 水位推进 +
幂等重拉不产生重复行 + 远端不可达时保留本地旧数据。

主档指向临时目录(monkeypatch repo._MASTER_DIR),绝不碰真实 data/master。
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from tools.config import settings
from tools.store import repo
from tools.sync import audit, pull, sign
from tools.store import backend_db

TOKEN = "test-token"
KEY = "test-key"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _mk_df(dates, closes):
    return pd.DataFrame({"date": pd.to_datetime(list(dates)),
                         "open": closes, "high": closes, "low": closes,
                         "close": closes, "volume": [100] * len(dates)})


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 主档 → 临时目录(repo 在 import 时把 settings.DATA_MASTER 抓成模块级 _MASTER_DIR)
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
    from tools.sync.ingest import _rate
    _rate.reset()
    from fastapi.testclient import TestClient
    from tools.sync.ingest import app
    with TestClient(app) as c:
        yield c
    _rate.reset()
    backend_db.reset_engine()
    audit.reset_engine()


def _sign_headers(kind, since, codes, *, key=KEY, key_id="k1", ts=None):
    ts = ts or _now_iso()
    env = sign.pull_envelope(kind, since or "", codes or "", ts, "n-1", key_id)
    sig = sign.sign_envelope(env, key)
    return {"X-Sync-Ts": ts, "X-Sync-Nonce": "n-1", "X-Sync-Key-Id": key_id, "X-Sync-Sig": sig}


def _get(client, kind="kline", since="", codes="", token=TOKEN, headers_extra=None):
    h = dict(headers_extra or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return client.get("/pull", params={"kind": kind, "since": since, "codes": codes}, headers=h)


# ————————————————————————————————————————————————
# /pull 门禁
# ————————————————————————————————————————————————
def test_pull_no_token_401(client):
    h = _sign_headers("kline", "", "")
    assert _get(client, token=None, headers_extra=h).status_code == 401


def test_pull_unsigned_403(client):
    # 有 token 但没签名头 → 403(安全红线:未签名请求拒绝)
    assert _get(client).status_code == 403


def test_pull_bad_key_403(client):
    h = _sign_headers("kline", "", "", key="wrong-key")
    assert _get(client, headers_extra=h).status_code == 403


def test_pull_expired_sig_409(client):
    old = (datetime.now().astimezone() - timedelta(seconds=1000)).isoformat(timespec="seconds")
    h = _sign_headers("kline", "", "", ts=old)
    assert _get(client, headers_extra=h).status_code == 409


def test_pull_bad_since_422(client):
    h = _sign_headers("kline", "not-a-date", "")
    assert _get(client, since="not-a-date", headers_extra=h).status_code == 422


def test_pull_unsupported_kind_422(client):
    h = _sign_headers("fundamental", "", "")
    assert _get(client, kind="fundamental", headers_extra=h).status_code == 422


def test_pull_valid_returns_increment_since(client):
    repo.put_master_kline("000001", _mk_df(
        ["2026-08-05", "2026-08-06", "2026-08-07"], [10.0, 11.0, 12.0]))
    h = _sign_headers("kline", "2026-08-05", "")
    r = _get(client, since="2026-08-05", headers_extra=h)
    assert r.status_code == 200
    body = r.json()
    dates = [b["date"] for b in body["data"]["000001"]]
    assert dates == ["2026-08-06", "2026-08-07"]      # 严格 date>since,不含 08-05
    assert body["count"] == 2


def test_pull_nan_to_null(client):
    df = _mk_df(["2026-08-06", "2026-08-07"], [10.0, 11.0])
    df.loc[0, "close"] = float("nan")                 # 造一个 NaN
    repo.put_master_kline("000002", df)
    h = _sign_headers("kline", "", "")
    r = _get(client, headers_extra=h)
    assert r.status_code == 200
    assert r.json()["data"]["000002"][0]["close"] is None   # NaN→null


def test_pull_codes_filter(client):
    repo.put_master_kline("000001", _mk_df(["2026-08-07"], [12.0]))
    repo.put_master_kline("600000", _mk_df(["2026-08-07"], [8.0]))
    h = _sign_headers("kline", "", "600000")
    r = _get(client, codes="600000", headers_extra=h)
    assert set(r.json()["data"].keys()) == {"600000"}


# ————————————————————————————————————————————————
# pull 客户端 round-trip(loopback:TestClient 当 get_fn)
# ————————————————————————————————————————————————
def _loopback_get_fn(client):
    def get_fn(url, params, headers):
        r = client.get(url, params=params, headers=headers)
        return r.status_code, r.json()
    return get_fn


def test_pull_client_roundtrip_and_watermark(client, tmp_path):
    # 单进程 loopback:server 读主档 → client 写回同一主档(fixture 已把 _MASTER_DIR 指临时目录)
    repo.put_master_kline("000001", _mk_df(
        ["2026-08-05", "2026-08-06", "2026-08-07"], [10.0, 11.0, 12.0]))
    wm = tmp_path / "pull_kline.json"
    get_fn = _loopback_get_fn(client)

    res = pull.pull("kline", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    since="2026-08-05", get_fn=get_fn, watermark_path=wm,
                    retries=0, sleep_fn=lambda *_: None)
    assert res["ok"] is True
    assert res["codes_written"] == 1
    assert res["max_date"] == "2026-08-07"
    assert pull.read_watermark("kline", wm) == "2026-08-07"   # 水位推进到最新 date

    # 幂等:再拉一次(全量),append 按 date 去重 → 主档不产生重复行
    before = len(repo.get_master_kline("000001"))
    pull.pull("kline", url="/pull", token=TOKEN, key=KEY, key_id="k1",
              since="", get_fn=get_fn, watermark_path=wm, retries=0, sleep_fn=lambda *_: None)
    assert len(repo.get_master_kline("000001")) == before


def test_pull_client_unreachable_keeps_local(client, tmp_path, monkeypatch):
    monkeypatch.setattr(repo, "_MASTER_DIR", tmp_path / "master")
    repo.put_master_kline("000001", _mk_df(["2026-08-07"], [12.0]))
    before = len(repo.get_master_kline("000001"))
    wm = tmp_path / "pull_kline.json"

    def boom(url, params, headers):
        raise ConnectionError("remote down")

    res = pull.pull("kline", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    since="", get_fn=boom, watermark_path=wm, retries=1,
                    base_delay=0, sleep_fn=lambda *_: None)
    assert res["ok"] is False
    assert not wm.exists()                                   # 失败不推进水位
    assert len(repo.get_master_kline("000001")) == before    # 本地旧数据保留不动
