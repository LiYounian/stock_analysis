"""方案2 端到端本地自证(§四):单进程 loopback 串起半环,不依赖远端真机。

enqueue(模拟远端入队)→ 签名 GET /pull?kind=pool_pending → pull 客户端落缓冲 →
plan_digestion 裁决 → 执行(mock add/remove)→ upload_date(pool_ack_ids) 搭 __pool_ack__ →
POST /ingest 标 consumed → list_pending 变空。

重点断言(统筹提醒):
  · 并集去重 + remove 时间戳裁决(通过裁决结果)
  · **重复拉 + 重复 ack 幂等无副作用**(二次 pull 拿空、二次 ack 不改变已 consumed)
  · **失败项保留 pending 下轮重试**(执行抛错的行不回执,仍 pending)
全程 temp sqlite + 临时目录,绝不碰真实库/真实 data。
"""
import json
from datetime import datetime

import pytest

from tools.config import settings
from tools.store import backend_db, repo
from tools.sync import audit, pull, sign, upload
from tools.sync import pool_merge
from tools.sync import pool_pending_store as store

TOKEN = "test-token"
KEY = "test-key"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


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


def _get_fn(client):
    def get_fn(url, params, headers):
        r = client.get(url, params=params, headers=headers)
        return r.status_code, r.json()
    return get_fn


def _post_fn(client):
    def post_fn(url, token, env):
        r = client.post("/ingest", json=env, headers={"Authorization": f"Bearer {token}"})
        return r.status_code, r.json()
    return post_fn


def _seed_analysis(root, date):
    d = root / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "000021.json").write_text(json.dumps({"meta": {"code": "000021"}}), encoding="utf-8")
    (d / "panel.json").write_text(json.dumps({"rows": []}), encoding="utf-8")


def _pull_rows(client, tmp_path):
    res = pull.pull("pool_pending", url="/pull", token=TOKEN, key=KEY, key_id="k1",
                    get_fn=_get_fn(client), retries=0, sleep_fn=lambda *_: None)
    assert res["ok"] is True
    buf = json.loads((tmp_path / "receipts" / "pool_pending.json").read_text(encoding="utf-8"))
    return buf["rows"]


def _send_ack(client, tmp_path, consumed_ids):
    _seed_analysis(tmp_path / "analysis", _today())
    upload.upload_date(_today(), url="/ingest", token=TOKEN, source="local",
                       key_id="k1", key=KEY, analysis_dir=tmp_path / "analysis",
                       post_fn=_post_fn(client), retries=0, base_delay=0,
                       sleep_fn=lambda *_: None, only_shards=set(),   # 只发回执,不发数据分片
                       pool_ack_ids=consumed_ids)


def test_e2e_half_loop_and_idempotency(client, tmp_path):
    # ① 模拟远端入队:add A + add B + remove C(C 已在本地池)
    store.enqueue(code="000001", name="平安", sector="银行", op="add")
    store.enqueue(code="600000", name="浦发", sector="银行", op="add")
    store.enqueue(code="300308", op="remove")              # 本地有 300308(早于删票时间)

    # ② 签名 /pull → 落缓冲
    rows = _pull_rows(client, tmp_path)
    assert len(rows) == 3

    # ③ 裁决:本地池含 300308(很早加)→ A/B 入 to_add,C 入 to_remove
    local_index = {("300308", "A"): "2026-08-20T00:00:00+08:00"}
    plan = pool_merge.plan_digestion(rows, local_index)
    assert {r["code"] for r in plan.to_add} == {"000001", "600000"}
    assert {r["code"] for r in plan.to_remove} == {"300308"}

    # ④ 执行(mock 成功)→ 全部 id 可 consume
    consumed = list(plan.noop_consumed_ids) + [r["id"] for r in plan.to_add] + [r["id"] for r in plan.to_remove]

    # ⑤ upload 搭 __pool_ack__ → POST /ingest 标 consumed
    _send_ack(client, tmp_path, consumed)
    assert store.count(status="pending") == 0              # 半环闭合:全 consumed
    assert store.count(status="consumed") == 3

    # ⑥ 幂等:再拉一次 → 拿空(已无 pending)
    assert _pull_rows(client, tmp_path) == []
    # 重复 ack(同 id 再标)→ 无副作用,仍全 consumed
    _send_ack(client, tmp_path, consumed)
    assert store.count(status="pending") == 0
    assert store.count(status="consumed") == 3


def test_e2e_failed_row_stays_pending(client, tmp_path):
    """失败项保留 pending 下轮重试:只回执成功的 id,失败的行仍 pending 且下轮能重新拉到。"""
    ida = store.enqueue(code="000001", name="平安", sector="银行", op="add")
    idb = store.enqueue(code="600000", name="浦发", sector="银行", op="add")

    rows = _pull_rows(client, tmp_path)
    assert len(rows) == 2

    # 模拟 B 采集失败:只把 A 计入 consumed(B 保留 pending)
    _send_ack(client, tmp_path, [ida])
    assert store.count(status="pending") == 1              # B 仍 pending
    assert store.count(status="consumed") == 1

    # 下轮重拉:只剩 B(失败项可重试)
    rows2 = _pull_rows(client, tmp_path)
    assert [r["id"] for r in rows2] == [idb]
