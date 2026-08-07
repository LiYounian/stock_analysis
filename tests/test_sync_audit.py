"""ingest 审计/防重放表运维单测(tools.sync.audit):nonce 过期清理 + 审计查询。"""
from datetime import datetime, timedelta

import pytest

from tools.config import settings
from tools.sync import audit


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 'a.db'}")
    audit.reset_engine()
    yield
    audit.reset_engine()


def test_purge_expired_removes_only_old(db, monkeypatch):
    monkeypatch.setattr(settings, "SYNC_NONCE_KEEP_S", 3600)     # 保留 1 小时内
    audit.remember_nonce("fresh")                               # 刚记的,应保留
    # 手动塞一条很久以前的 nonce
    from sqlalchemy import insert
    eng = audit._get_engine()
    old_at = (datetime.now().astimezone() - timedelta(days=2)).isoformat(timespec="seconds")
    with eng.begin() as c:
        c.execute(insert(audit.seen_nonce_t).values(nonce="old", at=old_at))

    removed = audit.purge_expired()
    assert removed == 1                                          # 只删过期那条
    assert audit.nonce_seen("fresh") is True
    assert audit.nonce_seen("old") is False


def test_recent_audits_order_and_limit(db):
    for i in range(5):
        audit.record_audit(source="s", key_id="k1", date="2026-08-08", rows=i,
                           verify_ok=True, result="ok", msg=str(i))
    rows = audit.recent_audits(limit=3)
    assert len(rows) == 3                                        # 限制条数
    assert rows[0]["msg"] == "4" and rows[-1]["msg"] == "2"      # 时间倒序(最新在前)


def test_cleanup_main_runs(db, capsys):
    audit.remember_nonce("n1")
    rc = audit.main([])                                          # CLI 入口不报错
    assert rc == 0
    assert "清理过期 nonce" in capsys.readouterr().out