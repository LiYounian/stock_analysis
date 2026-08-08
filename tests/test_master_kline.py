"""滚动主档 + load_kline 优先主档 + spot 增量 append 单测(不触网)。

锁语义:
  - 主档 put/get 往返;put 时按 date 去重+升序;
  - append 幂等(同日覆盖不产生重复行)、append 到不存在=首次落地;
  - load_kline 优先主档、回退 raw、两缺抛 FileNotFoundError(对外签名不变);
  - spot 增量:更新已有主档、停牌(spot 无该 bar)跳过、重复跑幂等;
  - delete_stock 连带清理主档。
路径隔离:monkeypatch _RAW_DIR / _MASTER_DIR 到 tmp。
"""
import pandas as pd
import pytest

from tools.collectors import market
from tools.store import repo as store


@pytest.fixture
def iso(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    master = tmp_path / "master"
    raw.mkdir()
    master.mkdir()
    monkeypatch.setattr(store, "_RAW_DIR", raw)
    monkeypatch.setattr(store, "_MASTER_DIR", master)
    store.set_active_date("2026-08-07")
    yield store
    store.set_active_date(None)


def _kdf(dates, close):
    n = len(dates)
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": close, "high": [c + 0.5 for c in close], "low": [c - 0.5 for c in close],
        "close": close, "volume": [100.0] * n, "amount": [1e6] * n,
        "turnover": [0.05] * n, "pct_chg": [0.0] * n,
    })


# —— 主档读写 ——
def test_master_roundtrip(iso):
    df = _kdf(["2026-08-05", "2026-08-06"], [10.0, 10.5])
    p = iso.put_master_kline("000021", df)
    assert p.endswith("master/kline/000021.parquet")
    got = iso.get_master_kline("000021")
    assert len(got) == 2
    assert got["date"].is_monotonic_increasing
    assert iso.has_master_kline("000021")


def test_master_put_dedup_and_sort(iso):
    """乱序 + 同日重复 → put 后升序、同日只留最后一条。"""
    df = _kdf(["2026-08-06", "2026-08-05", "2026-08-06"], [10.5, 10.0, 99.0])
    iso.put_master_kline("000021", df)
    got = iso.get_master_kline("000021")
    assert list(got["date"].dt.strftime("%Y-%m-%d")) == ["2026-08-05", "2026-08-06"]
    assert got.iloc[-1]["close"] == 99.0        # 同日保留最后一条


def test_master_missing_raises(iso):
    with pytest.raises(FileNotFoundError):
        iso.get_master_kline("999999")
    assert not iso.has_master_kline("999999")


def test_append_creates_when_absent(iso):
    iso.append_master_kline("000021", _kdf(["2026-08-05"], [10.0]))
    assert iso.has_master_kline("000021")
    assert len(iso.get_master_kline("000021")) == 1


def test_append_idempotent_same_day(iso):
    """同日 append 两次 → 只保留一条(覆盖),不产生重复行。"""
    iso.put_master_kline("000021", _kdf(["2026-08-05", "2026-08-06"], [10.0, 10.5]))
    iso.append_master_kline("000021", _kdf(["2026-08-07"], [11.0]))
    iso.append_master_kline("000021", _kdf(["2026-08-07"], [11.2]))   # 重复跑
    got = iso.get_master_kline("000021")
    assert len(got) == 3
    assert got.iloc[-1]["close"] == 11.2         # 最后一次写入生效
    assert got["date"].is_monotonic_increasing


def test_list_master_codes(iso):
    iso.put_master_kline("000021", _kdf(["2026-08-05"], [10.0]))
    iso.put_master_kline("600519", _kdf(["2026-08-05"], [1000.0]))
    assert iso.list_master_codes() == ["000021", "600519"]


def test_master_meta_dates(iso):
    iso.put_master_kline("000021", _kdf(["2026-08-05", "2026-08-06"], [10.0, 10.5]))
    m = iso.get_master_kline_meta("000021")
    assert m["rows"] == 2 and m["first_date"] == "2026-08-05" and m["last_date"] == "2026-08-06"


def test_delete_stock_removes_master(iso):
    iso.put_master_kline("000021", _kdf(["2026-08-05"], [10.0]))
    removed = iso.delete_stock("000021")
    assert not iso.has_master_kline("000021")
    assert any("master/kline/000021.parquet" in r for r in removed)


# —— load_kline 优先主档 / 回退 raw ——
def test_load_kline_prefers_master(iso):
    """主档与 raw 同时存在 → 读主档(全历史)。"""
    iso.put_raw("kline", "000021", _kdf(["2026-08-06"], [10.5]))            # raw 仅 1 根
    iso.put_master_kline("000021", _kdf(["2026-08-01", "2026-08-06"], [9.0, 10.5]))
    got = market.load_kline("000021")
    assert len(got) == 2                    # 主档 2 根,而非 raw 的 1 根


def test_load_kline_falls_back_to_raw(iso):
    """无主档 → 回退当日 raw。"""
    iso.put_raw("kline", "000021", _kdf(["2026-08-06"], [10.5]))
    got = market.load_kline("000021")
    assert len(got) == 1


def test_load_kline_both_missing_raises(iso):
    with pytest.raises(FileNotFoundError):
        market.load_kline("999999")


# —— spot 增量 append ——
def _spot(rows):
    return pd.DataFrame(rows)


def test_update_master_from_spot_appends(iso):
    iso.put_master_kline("000021", _kdf(["2026-08-06"], [10.5]))
    spot_df = pd.DataFrame([
        {"code": "000021", "open": 10.6, "high": 10.9, "low": 10.4,
         "close": 10.8, "volume": 100.0, "amount": 1e6, "turnover": 0.05, "pct_chg": 2.8},
    ])
    res = market.update_master_from_spot(spot=spot_df, date="2026-08-07")
    assert res["ok"] == 1
    got = iso.get_master_kline("000021")
    assert len(got) == 2
    assert got.iloc[-1]["date"].strftime("%Y-%m-%d") == "2026-08-07"
    assert got.iloc[-1]["close"] == 10.8


def test_update_master_from_spot_idempotent(iso):
    iso.put_master_kline("000021", _kdf(["2026-08-06"], [10.5]))
    spot_df = pd.DataFrame([
        {"code": "000021", "open": 10.6, "high": 10.9, "low": 10.4,
         "close": 10.8, "volume": 100.0, "amount": 1e6, "turnover": 0.05, "pct_chg": 2.8},
    ])
    market.update_master_from_spot(spot=spot_df, date="2026-08-07")
    market.update_master_from_spot(spot=spot_df, date="2026-08-07")   # 重复跑
    got = iso.get_master_kline("000021")
    assert len(got) == 2                    # 幂等:仍 2 根,无重复


def test_update_master_from_spot_skips_halted(iso):
    """spot 无该股当日 bar(停牌)→ 跳过,不动主档。"""
    iso.put_master_kline("000021", _kdf(["2026-08-06"], [10.5]))
    spot_df = pd.DataFrame([
        {"code": "600519", "open": 1, "high": 1, "low": 1, "close": 1,
         "volume": 1.0, "amount": 1.0, "turnover": 0.0, "pct_chg": 0.0},
    ])
    res = market.update_master_from_spot(codes=["000021"], spot=spot_df, date="2026-08-07")
    assert res["ok"] == 0 and res["skipped"] == 1
    assert len(iso.get_master_kline("000021")) == 1


def test_update_master_from_spot_new_code(iso):
    """codes=None 且 spot 有新股 → 首次落主档。"""
    spot_df = pd.DataFrame([
        {"code": "301999", "open": 20.0, "high": 21.0, "low": 19.5, "close": 20.8,
         "volume": 500.0, "amount": 1e7, "turnover": 0.1, "pct_chg": 4.0},
    ])
    res = market.update_master_from_spot(spot=spot_df, date="2026-08-07")
    assert res["ok"] == 1
    assert iso.has_master_kline("301999")
