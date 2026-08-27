"""pool_service.enqueue_pending 单测(方案2 远端入队,不采集不重建)。

锁住:①enqueue_pending 写 pool_pending 表(status=pending);②**绝不触发**采集/重建
(monkeypatch collect_one/rebuild_artifacts/add_stock/remove_stock,断言均未被调);
③深校验不在此做(留给本地消化的 add_stock);④source 走 settings.POOL_PENDING_SOURCE。
temp sqlite 隔离(monkeypatch DB_URL + reset_engine)。
"""
import pytest

from tools.config import settings
from tools import pool_service
from tools.sync import pool_pending_store as store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DB_URL", f"sqlite:///{tmp_path / 'p.db'}")
    store.reset_engine()
    # 侦测:任何采集/重建/名单真源写入被调 → 立刻失败(远端入队不该碰这些)
    def _boom(*a, **k):
        raise AssertionError("enqueue 路径不应触发采集/重建/名单写入")
    monkeypatch.setattr(pool_service, "collect_one", _boom)
    monkeypatch.setattr(pool_service, "rebuild_artifacts", _boom)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "add_stock", _boom)
    monkeypatch.setattr(stock_pool, "remove_stock", _boom)
    yield
    store.reset_engine()


def test_enqueue_add_writes_pending_no_collect(db):
    res = pool_service.enqueue_pending("add", "600000", name="浦发", sector="银行")
    assert res["ok"] is True
    assert res["queued"]["op"] == "add" and res["queued"]["code"] == "600000"
    assert res["note"]                                     # 有非实时提示
    rows = store.list_pending()
    assert len(rows) == 1 and rows[0]["op"] == "add" and rows[0]["status"] == "pending"


def test_enqueue_remove_writes_pending_no_rebuild(db):
    res = pool_service.enqueue_pending("remove", "600000")
    assert res["queued"]["op"] == "remove"
    rows = store.list_pending()
    assert len(rows) == 1 and rows[0]["op"] == "remove"


def test_enqueue_source_from_settings(db, monkeypatch):
    monkeypatch.setattr(settings, "POOL_PENDING_SOURCE", "remote-web")
    pool_service.enqueue_pending("add", "600000", sector="银行")
    assert store.list_pending()[0]["source"] == "remote-web"


def test_enqueue_no_deep_validation(db):
    """深校验不在入队做:格式怪异的 code 也能入队(留给本地消化 add_stock 裁决)。"""
    res = pool_service.enqueue_pending("add", "ABC123", sector="测试")
    assert res["ok"] is True                               # 未因格式报错
    assert store.list_pending()[0]["code"] == "ABC123"


def test_enqueue_still_rejects_empty_code(db):
    """底层轻校验仍在:空 code 拒绝。"""
    with pytest.raises(ValueError):
        pool_service.enqueue_pending("add", "   ", sector="银行")
