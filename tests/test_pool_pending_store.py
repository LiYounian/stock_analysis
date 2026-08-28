"""远端提案表 pool_pending 的 CRUD 单测(tools.sync.pool_pending_store)。

锁住:enqueue→list→mark_consumed 往返;list 按 requested_at 升序;mark_consumed
幂等;count 分状态;轻校验(op 非法 / code 空)拒绝。全程只在 temp sqlite
(monkeypatch DB_URL + reset_engine),绝不碰真实库。
"""
import pytest

from tools.config import settings
from tools.sync import pool_pending_store as store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 'p.db'}")
    store.reset_engine()
    yield
    store.reset_engine()


def test_enqueue_returns_id_and_lists_pending(db):
    rid = store.enqueue(code="600000", name="浦发银行", sector="银行", op="add")
    assert isinstance(rid, int) and rid > 0
    rows = store.list_pending()
    assert len(rows) == 1
    r = rows[0]
    assert r["code"] == "600000" and r["op"] == "add" and r["status"] == "pending"
    assert r["market"] == "A" and r["source"] == "remote"      # 默认值
    assert r["requested_at"]                                   # 自动填时间戳


def test_market_normalized_upper(db):
    store.enqueue(code="00700", market="hk", op="add")
    assert store.list_pending()[0]["market"] == "HK"           # 归一大写


def test_list_pending_ordered_by_requested_at_asc(db):
    store.enqueue(code="000002", op="add", requested_at="2026-08-27T10:00:00+08:00")
    store.enqueue(code="000001", op="add", requested_at="2026-08-27T09:00:00+08:00")
    store.enqueue(code="000003", op="add", requested_at="2026-08-27T11:00:00+08:00")
    codes = [r["code"] for r in store.list_pending()]
    assert codes == ["000001", "000002", "000003"]             # 升序(删除裁决前置约定)


def test_mark_consumed_moves_out_of_pending(db):
    a = store.enqueue(code="600000", op="add")
    b = store.enqueue(code="600001", op="add")
    n = store.mark_consumed([a])
    assert n == 1
    pend = [r["code"] for r in store.list_pending()]
    assert pend == ["600001"]                                  # a 已消化,只剩 b
    cons = store.list_pending(status="consumed")
    assert len(cons) == 1 and cons[0]["code"] == "600000"
    assert cons[0]["consumed_at"]                              # 写了回执时刻


def test_mark_consumed_idempotent(db):
    a = store.enqueue(code="600000", op="add")
    assert store.mark_consumed([a]) == 1
    # 再标一次:行仍在(update 命中),但不改变 pending 集合(幂等,无副作用)
    store.mark_consumed([a])
    assert store.count(status="pending") == 0
    assert store.count(status="consumed") == 1


def test_mark_consumed_empty_is_noop(db):
    store.enqueue(code="600000", op="add")
    assert store.mark_consumed([]) == 0
    assert store.count(status="pending") == 1


def test_count_by_status_and_total(db):
    a = store.enqueue(code="600000", op="add")
    store.enqueue(code="600001", op="remove")
    store.mark_consumed([a])
    assert store.count() == 2                                  # 全部
    assert store.count(status="pending") == 1
    assert store.count(status="consumed") == 1


def test_enqueue_rejects_bad_op(db):
    with pytest.raises(ValueError):
        store.enqueue(code="600000", op="foo")


def test_enqueue_rejects_empty_code(db):
    with pytest.raises(ValueError):
        store.enqueue(code="   ", op="add")


def test_op_normalized_lower(db):
    store.enqueue(code="600000", op="ADD")
    assert store.list_pending()[0]["op"] == "add"
