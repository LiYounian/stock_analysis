"""交易日历守卫单测(collectors.calendar):交易日/非交易日/日历取不到回退。

锁语义:
  · 日历可用 → 精确按日历集合判定(节假日=非交易日);
  · 日历取不到(空集)→ 回退"周一~周五"近似;
  · _norm 接受 YYYYMMDD / YYYY-MM-DD / None(今天)。
不触网:_fetch_from_akshare / _load_cache 全部 monkeypatch。
"""
from tools.collectors import calendar as cal


def test_norm_formats():
    assert cal._norm("20260807") == "2026-08-07"
    assert cal._norm("2026-08-07") == "2026-08-07"
    assert len(cal._norm(None)) == 10


def test_trading_day_true_when_in_calendar(monkeypatch):
    monkeypatch.setattr(cal, "trading_dates", lambda **k: {"2026-08-07", "2026-08-10"})
    assert cal.is_trading_day("2026-08-07") is True


def test_holiday_false_when_not_in_calendar(monkeypatch):
    # 日历可用但该日不在集合(节假日/周末)→ False,即使是工作日
    monkeypatch.setattr(cal, "trading_dates", lambda **k: {"2026-08-07"})
    assert cal.is_trading_day("2026-10-01") is False       # 国庆:日历里没有


def test_weekend_false_via_calendar(monkeypatch):
    monkeypatch.setattr(cal, "trading_dates", lambda **k: {"2026-08-07"})
    assert cal.is_trading_day("2026-08-08") is False       # 周六


def test_fallback_weekday_approx_when_no_calendar(monkeypatch):
    # 日历取不到(空集)→ 回退周内近似
    monkeypatch.setattr(cal, "trading_dates", lambda **k: set())
    assert cal.is_trading_day("2026-08-07") is True        # 周五
    assert cal.is_trading_day("2026-08-08") is False       # 周六
    assert cal.is_trading_day("2026-08-09") is False       # 周日


def test_trading_dates_uses_fresh_cache(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(cal, "_load_cache",
                        lambda: {"fetched_at": datetime.now().isoformat(),
                                 "dates": ["2026-08-07"]})
    # 新鲜缓存不应触发采集
    monkeypatch.setattr(cal, "_fetch_from_akshare",
                        lambda: (_ for _ in ()).throw(AssertionError("不应采集")))
    assert cal.trading_dates() == {"2026-08-07"}


def test_trading_dates_fetch_when_stale(monkeypatch):
    monkeypatch.setattr(cal, "_load_cache",
                        lambda: {"fetched_at": "2000-01-01T00:00:00", "dates": ["old"]})
    monkeypatch.setattr(cal, "_fetch_from_akshare", lambda: ["2026-08-07"])
    monkeypatch.setattr(cal, "_save_cache", lambda dates: None)
    assert cal.trading_dates() == {"2026-08-07"}


def test_trading_dates_uses_old_cache_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(cal, "_load_cache",
                        lambda: {"fetched_at": "2000-01-01T00:00:00", "dates": ["2026-08-07"]})
    monkeypatch.setattr(cal, "_fetch_from_akshare",
                        lambda: (_ for _ in ()).throw(ConnectionError("down")))
    assert cal.trading_dates() == {"2026-08-07"}           # 采集失败退回旧缓存
