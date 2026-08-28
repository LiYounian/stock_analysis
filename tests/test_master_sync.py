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
from tools.config import settings
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

    def fake_update(codes=None, date=None, spot=None, source=None):
        seen["date"] = date
        seen["codes"] = codes
        seen["source"] = source
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(settings, "TUSHARE_ENABLED", False)   # 未配 Tushare → 免费源
    monkeypatch.setattr(market, "fetch_spot_all", fake_spot)
    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    # backfill 不应被调用
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应走 backfill")))
    r = master_sync.sync_master(codes, as_of=_TODAY)
    assert r["mode"] == "spot" and r["ok"] == 2
    assert seen["date"] == _TODAY and seen["codes"] == codes
    assert seen["source"] == "akshare_spot"   # 未配 Tushare → 免费源标记


# ———————————— Tushare 可选源口子 + 回退(免费优先/读得通才用/失败静默回退)————————————
def test_spot_prefers_tushare_when_enabled(monkeypatch):
    """配了 Tushare 且读得通 → 增量走 Tushare 全市场 daily,source=tushare_daily,
    且**不调用**免费源 fetch_spot_all。"""
    codes = ["000001", "000002"]
    _patch_master(monkeypatch, codes, _TODAY)             # 新鲜 → spot 增量分支
    seen = {}
    from tools.collectors import tushare_daily

    monkeypatch.setattr(settings, "TUSHARE_ENABLED", True)
    monkeypatch.setattr(tushare_daily, "fetch_daily_all",
                        lambda day: pd.DataFrame({"code": codes}))
    monkeypatch.setattr(market, "fetch_spot_all",
                        lambda: (_ for _ in ()).throw(AssertionError("配了 Tushare 不应调免费源 spot")))

    def fake_update(codes=None, date=None, spot=None, source=None):
        seen["source"] = source
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    r = master_sync.sync_master(codes, as_of=_TODAY)
    assert r["mode"] == "tushare_spot" and r["source"] == "tushare_daily"
    assert seen["source"] == "tushare_daily"


def test_spot_falls_back_to_free_when_tushare_fails(monkeypatch):
    """配了 Tushare 但**读不通**(抛异常)→ 静默回退免费源 akshare spot,不报错、不进逐只 fallback,
    source 回落 akshare_spot。"""
    codes = ["000001", "000002"]
    _patch_master(monkeypatch, codes, _TODAY)
    seen = {}
    from tools.collectors import tushare_daily

    monkeypatch.setattr(settings, "TUSHARE_ENABLED", True)
    monkeypatch.setattr(tushare_daily, "fetch_daily_all",
                        lambda day: (_ for _ in ()).throw(ConnectionError("tushare 未收盘/网络断")))
    called = {"free_spot": False}

    def fake_free_spot():
        called["free_spot"] = True
        return pd.DataFrame({"code": codes})

    def fake_update(codes=None, date=None, spot=None, source=None):
        seen["source"] = source
        return {"ok": len(codes), "skipped": 0}

    monkeypatch.setattr(market, "fetch_spot_all", fake_free_spot)
    monkeypatch.setattr(market, "update_master_from_spot", fake_update)
    r = master_sync.sync_master(codes, as_of=_TODAY)
    assert called["free_spot"] is True             # 确实回退到免费源
    assert r["mode"] == "spot" and seen["source"] == "akshare_spot"


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


# ———————————— 回退推进主档(核心红线:修 fetch_kline 只写 raw 不推主档)————————————
def _real_store_to_tmp(monkeypatch, tmp_path):
    """把滚动主档目录指向 tmp_path,用真实 store 读写(不触网、不落用户 data/)。"""
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")


def _bar(date, close, *, o=None, h=None, l=None, vol=1000):
    """造一根规整主档 bar(schema 与主档一致)。"""
    o = close if o is None else o
    h = close if h is None else h
    l = close if l is None else l
    return {"date": pd.Timestamp(date), "open": o, "high": h, "low": l,
            "close": close, "volume": vol, "amount": vol * close,
            "turnover": 1.0, "pct_chg": 0.0}


def _seed_master(code, rows):
    store.put_master_kline(code, pd.DataFrame(rows), meta={"source": "seed"})


def _spot_fails(monkeypatch):
    """令决策走 spot 分支,且 spot 抓取失败 → 触发回退。"""
    monkeypatch.setattr(market, "fetch_spot_all",
                        lambda: (_ for _ in ()).throw(ConnectionError("spot down")))


def test_fallback_advances_master_last_date(monkeypatch, tmp_path):
    """核心红线:spot 失败回退,fetch_kline 抓到含当日新 bar 的数据后,
    load_kline 返回的主档 last_date 必须 == 当日新 bar 日期(证明回退推进了主档,
    而非停在旧主档)。"""
    _real_store_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    # 旧主档:...→ 2026-08-06
    _seed_master(code, [_bar("2026-08-05", 10.0), _bar("2026-08-06", 10.5)])
    # 决策:主档就绪且新鲜(last_date=2026-08-06,as_of=2026-08-07,gap=1)→ 走 spot
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-08-06"} if c == code else None)
    _spot_fails(monkeypatch)
    # 回退 fetch_kline 抓到 raw:含旧 bar(值不同)+ 当日新 bar
    raw = pd.DataFrame([_bar("2026-08-06", 99.9), _bar("2026-08-07", 11.0)])
    monkeypatch.setattr(market, "fetch_kline", lambda codes, **k: {code: raw})

    r = master_sync.sync_master([code], as_of=_TODAY)
    assert r["mode"] == "fallback" and r["advanced"] == 1
    m = market.load_kline(code)
    assert pd.to_datetime(m["date"]).max().normalize() == pd.Timestamp("2026-08-07")


def test_fallback_overwrites_last_day_but_not_earlier_history(monkeypatch, tmp_path):
    """覆盖口径(>= last):**最新日**可被 raw 覆盖(盘中→收盘更新),但**更早历史日**不动。"""
    _real_store_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    _seed_master(code, [_bar("2026-08-05", 10.0), _bar("2026-08-06", 10.5)])
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-08-06"} if c == code else None)
    _spot_fails(monkeypatch)
    # raw 同时给:更早历史 08-05(异值 88.8)、最新日 08-06(收盘价 99.9)、新日 08-07
    raw = pd.DataFrame([_bar("2026-08-05", 88.8), _bar("2026-08-06", 99.9), _bar("2026-08-07", 11.0)])
    monkeypatch.setattr(market, "fetch_kline", lambda codes, **k: {code: raw})

    master_sync.sync_master([code], as_of=_TODAY)
    m = market.load_kline(code).set_index(pd.to_datetime(market.load_kline(code)["date"]))
    # 更早历史 08-05 不被覆盖(< last,受保护)
    assert float(m.loc[m.index == pd.Timestamp("2026-08-05")]["close"].iloc[0]) == 10.0
    # 最新日 08-06 被 raw 覆盖成 99.9(== last,放行同日更新)
    assert float(m.loc[m.index == pd.Timestamp("2026-08-06")]["close"].iloc[0]) == 99.9
    # 新日 08-07 追加
    assert float(m.loc[m.index == pd.Timestamp("2026-08-07")]["close"].iloc[0]) == 11.0


def test_fallback_advance_idempotent(monkeypatch, tmp_path):
    """幂等:连跑两次,主档行数不因重复 append 变化、last_date 不变。"""
    _real_store_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    _seed_master(code, [_bar("2026-08-05", 10.0), _bar("2026-08-06", 10.5)])
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-08-06"} if c == code else None)
    _spot_fails(monkeypatch)
    raw = pd.DataFrame([_bar("2026-08-07", 11.0)])
    monkeypatch.setattr(market, "fetch_kline", lambda codes, **k: {code: raw})

    master_sync.sync_master([code], as_of=_TODAY)
    n1 = len(market.load_kline(code))
    last1 = pd.to_datetime(market.load_kline(code)["date"]).max()
    master_sync.sync_master([code], as_of=_TODAY)
    n2 = len(market.load_kline(code))
    last2 = pd.to_datetime(market.load_kline(code)["date"]).max()
    assert n2 == n1 == 3 and last2 == last1 == pd.Timestamp("2026-08-07")


def test_fallback_failed_codes_not_advanced(monkeypatch, tmp_path):
    """失败票不推进:fetch_kline 未覆盖(或返回空)的票,主档 last_date 不变。"""
    _real_store_to_tmp(monkeypatch, tmp_path)
    ok_code, bad_code = "000001", "000002"
    _seed_master(ok_code, [_bar("2026-08-06", 10.5)])
    _seed_master(bad_code, [_bar("2026-08-06", 20.0)])
    monkeypatch.setattr(store, "get_master_kline_meta",
                        lambda c: {"last_date": "2026-08-06"})
    _spot_fails(monkeypatch)
    # 只有 ok_code 抓到(含新 bar);bad_code 缺席(失败集)
    monkeypatch.setattr(market, "fetch_kline",
                        lambda codes, **k: {ok_code: pd.DataFrame([_bar("2026-08-07", 11.0)])})

    r = master_sync.sync_master([ok_code, bad_code], as_of=_TODAY)
    assert r["advanced"] == 1 and r["failed"] == 1
    assert pd.to_datetime(market.load_kline(ok_code)["date"]).max() == pd.Timestamp("2026-08-07")
    # 失败票主档停在旧 last_date
    assert pd.to_datetime(market.load_kline(bad_code)["date"]).max() == pd.Timestamp("2026-08-06")


def test_fallback_first_landing_when_no_master(monkeypatch, tmp_path):
    """主档不存在的票(新股首次):回退时全量落地为主档。
    此处覆盖低(主档空)→ 本走 backfill,但令 backfill 失败 → 回退,回退 fetch_kline
    抓到该票全量 → 应首次落地为主档。"""
    _real_store_to_tmp(monkeypatch, tmp_path)
    code = "000001"
    monkeypatch.setattr(store, "get_master_kline_meta", lambda c: None)
    monkeypatch.setattr(market, "backfill_master",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("baostock down")))
    full = pd.DataFrame([_bar("2026-08-05", 10.0), _bar("2026-08-06", 10.5),
                         _bar("2026-08-07", 11.0)])
    monkeypatch.setattr(market, "fetch_kline", lambda codes, **k: {code: full})

    r = master_sync.sync_master([code], as_of=_TODAY)
    assert r["mode"] == "fallback" and r["advanced"] == 1
    m = market.load_kline(code)
    assert len(m) == 3 and pd.to_datetime(m["date"]).max() == pd.Timestamp("2026-08-07")


def test_advance_master_intraday_to_close_overwrite(monkeypatch, tmp_path):
    """同日盘中→收盘覆盖:主档已有当天 bar(盘中价),回退再采到同日收盘价时,
    _advance_master_from_raw 必须放行同日 bar(>= last)→ append 按 date 覆盖成收盘价。
    (此前 > last 会把同日收盘价挡掉,主档停在午休价——本测试锁死修复。)"""
    import pandas as pd
    from tools.collectors import master_sync as ms
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    code = "300209"
    # 主档已有 08-11 + 08-12(盘中 35.6)
    mid = pd.DataFrame({"date": pd.to_datetime(["2026-08-11", "2026-08-12"]),
                        "open": [34.0, 35.0], "high": [35.0, 36.0], "low": [33.0, 34.0],
                        "close": [34.92, 35.6], "volume": [1e6, 1e6],
                        "amount": [1e7, 1e7], "turnover": [0.01, 0.01], "pct_chg": [0.0, 2.0]})
    store.append_master_kline(code, mid)
    assert float(store.get_master_kline(code)["close"].iloc[-1]) == 35.6
    # 收盘再采:同日 08-12 收盘价 36.5
    close = mid.copy(); close.loc[close["date"] == "2026-08-12", "close"] = 36.5
    ms._advance_master_from_raw({code: close})
    # 断言:同日 bar 被覆盖成收盘价,且不重复行
    m = store.get_master_kline(code)
    assert float(m[m["date"] == "2026-08-12"]["close"].iloc[0]) == 36.5, "同日收盘价未覆盖午休价"
    assert len(m[m["date"] == "2026-08-12"]) == 1, "同日出现重复行"
