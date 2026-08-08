"""闭环 K线采集编排单测(master_sync):主档 vs spot 分支选择 + fallback 路径。

锁语义:
  · 主档缺失/覆盖不足/太旧 → 走 backfill_master(baostock 全量);
  · 主档就绪且新鲜 → 走 fetch_spot_all + update_master_from_spot(当日增量);
  · backfill / spot 任一失败 → 回退逐只 akshare fetch_kline(闭环不崩);
  · 空票池 → noop。
不触网:market.* / store.* 全部 monkeypatch。
"""
import pandas as pd

from tools.collectors import market, master_sync
from tools.store import repo as store

_TODAY = "2026-08-07"    # 周五,交易日


def _patch_master(monkeypatch, codes_in_master, last_date):
    monkeypatch.setattr(store, "list_master_codes", lambda: list(codes_in_master))
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": last_date} if c in codes_in_master else None)


# ———————————— 分支选择 ————————————
def test_empty_codes_noop():
    assert master_sync.sync_master([])["mode"] == "noop"


def test_backfill_when_master_empty(monkeypatch):
    _patch_master(monkeypatch, [], None)
    called = {}

    def fake_backfill(codes, *a, **k):
        called["backfill"] = list(codes)
        return {"ok": len(codes), "failed": 0}

    monkeypatch.setattr(market, "backfill_master", fake_backfill)
    r = master_sync.sync_master(["000001", "000002"], as_of=_TODAY)
    assert r["mode"] == "backfill" and r["ok"] == 2
    assert called["backfill"] == ["000001", "000002"]


def test_backfill_when_coverage_low(monkeypatch):
    # 主档只有 1 只,请求 10 只 → 覆盖 10% < 90% → backfill
    _patch_master(monkeypatch, ["000001"], _TODAY)
    monkeypatch.setattr(market, "backfill_master", lambda codes, *a, **k: {"ok": len(codes), "failed": 0})
    codes = [f"{i:06d}" for i in range(1, 11)]
    assert master_sync.sync_master(codes, as_of=_TODAY)["mode"] == "backfill"


def test_backfill_when_stale(monkeypatch):
    # 覆盖足够但主档最新日期距今 > 7 天 → backfill 重算
    codes = ["000001", "000002"]
    _patch_master(monkeypatch, codes, "2026-07-01")     # 距 2026-08-07 超 7 天
    monkeypatch.setattr(market, "backfill_master", lambda c, *a, **k: {"ok": len(c), "failed": 0})
    assert master_sync.sync_master(codes, as_of=_TODAY)["mode"] == "backfill"


def test_spot_when_master_fresh(monkeypatch):
    codes = ["000001", "000002"]
    _patch_master(monkeypatch, codes, _TODAY)           # 新鲜
    seen = {}

    def fake_spot():
        return pd.DataFrame({"code": codes})

    def fake_update(codes=None, date=None, spot=None):
        seen["date"] = date
        seen["codes"] = codes
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(market, "fetch_spot_all", fake_spot)
    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    # backfill 不应被调用
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走 backfill")))
    r = master_sync.sync_master(codes, as_of=_TODAY)
    assert r["mode"] == "spot" and r["ok"] == 2
    assert seen["date"] == _TODAY and seen["codes"] == codes


# ———————————— fallback 路径 ————————————
def test_fallback_on_backfill_failure(monkeypatch):
    _patch_master(monkeypatch, [], None)                # → 本应 backfill
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("baostock down")))
    monkeypatch.setattr(market, "fetch_kline",
                        lambda codes, **k: {c: pd.DataFrame() for c in codes})
    r = master_sync.sync_master(["000001", "000002"], as_of=_TODAY)
    assert r["mode"] == "fallback" and r["ok"] == 2 and r["failed"] == 0


def test_fallback_on_backfill_all_zero(monkeypatch):
    # backfill 返回 ok=0(全失败)也应触发 fallback
    _patch_master(monkeypatch, [], None)
    monkeypatch.setattr(market, "backfill_master", lambda *a, **k: {"ok": 0, "failed": 3})
    monkeypatch.setattr(market, "fetch_kline", lambda codes, **k: {codes[0]: pd.DataFrame()})
    r = master_sync.sync_master(["000001", "000002", "000003"], as_of=_TODAY)
    assert r["mode"] == "fallback"


def test_fallback_on_spot_failure(monkeypatch):
    codes = ["000001", "000002"]
    _patch_master(monkeypatch, codes, _TODAY)           # → 本应 spot
    monkeypatch.setattr(market, "fetch_spot_all",
                        lambda: (_ for _ in ()).throw(ConnectionError("spot down")))
    monkeypatch.setattr(market, "fetch_kline",
                        lambda c, **k: {x: pd.DataFrame() for x in c})
    r = master_sync.sync_master(codes, as_of=_TODAY)
    assert r["mode"] == "fallback" and r["ok"] == 2


def test_no_fallback_reraises(monkeypatch):
    _patch_master(monkeypatch, [], None)
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        master_sync.sync_master(["000001"], as_of=_TODAY, fallback=False)
        assert False, "fallback=False 应重新抛出"
    except RuntimeError:
        pass
