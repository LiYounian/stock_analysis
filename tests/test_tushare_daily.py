"""Tushare 可选源采集单测(tushare_daily)。

锁语义(全部 mock,不需真 token / 不触网):
  · is_configured 只看 settings.TUSHARE_TOKEN;
  · fetch_daily_all 归一:ts_code→6位 code、vol 手→×100 股、amount 千元→×1000 元、
    换手率从 daily_basic.turnover_rate 合并(口径=百分比);空数据抛 ConnectionError;
  · fetch_chip 取筹码 winner_rate / cost_95pct;
  · 未装 tushare 时 _pro() 抛 RuntimeError(供上层回退,不崩)。
"""
import sys
import types

import pandas as pd
import pytest

from tools.collectors import tushare_daily
from tools.config import settings


def _install_fake_tushare(monkeypatch, *, daily=None, daily_basic=None, cyq=None,
                          expect_token="test-token"):
    """把一个假的 `tushare` 模块塞进 sys.modules,pro_api 返回可控数据。"""
    class _FakePro:
        def daily(self, **kw):
            return daily
        def daily_basic(self, **kw):
            return daily_basic
        def cyq_perf(self, **kw):
            return cyq

    fake = types.ModuleType("tushare")
    def _pro_api(token):
        assert token == expect_token, "token 必须来自 settings.TUSHARE_TOKEN"
        return _FakePro()
    fake.pro_api = _pro_api
    monkeypatch.setitem(sys.modules, "tushare", fake)
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", expect_token)


def test_is_configured(monkeypatch):
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", "")
    assert tushare_daily.is_configured() is False
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", "x")
    assert tushare_daily.is_configured() is True


def test_fetch_daily_all_normalizes_units_and_merges_turnover(monkeypatch):
    daily = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": ["20260818", "20260818"],
        "open": [10.0, 8.0], "high": [11.0, 8.5], "low": [9.5, 7.9], "close": [10.5, 8.2],
        "vol": [12.0, 30.0],       # 手 → ×100 股
        "amount": [34.0, 50.0],    # 千元 → ×1000 元
        "pct_chg": [1.2, -0.5],
    })
    basic = pd.DataFrame({
        "ts_code": ["000001.SZ", "600000.SH"],
        "trade_date": ["20260818", "20260818"],
        "turnover_rate": [2.5, 1.1],
    })
    _install_fake_tushare(monkeypatch, daily=daily, daily_basic=basic)
    out = tushare_daily.fetch_daily_all("2026-08-18")
    row = out.set_index("code")
    assert list(out["code"]) == ["000001", "600000"]         # ts_code→6位裸码
    assert float(row.loc["000001", "volume"]) == 1200.0       # 12 手 ×100
    assert float(row.loc["000001", "amount"]) == 34000.0      # 34 千元 ×1000
    assert float(row.loc["000001", "turnover"]) == 2.5        # 换手率从 daily_basic 合并
    assert float(row.loc["600000", "turnover"]) == 1.1


def test_fetch_daily_all_turnover_na_when_basic_missing(monkeypatch):
    """daily_basic 取失败(返回空)不致命:换手率列为 NA,OHLC 仍正常返回。"""
    daily = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20260818"],
        "open": [10.0], "high": [11.0], "low": [9.5], "close": [10.5],
        "vol": [12.0], "amount": [34.0], "pct_chg": [1.2],
    })
    _install_fake_tushare(monkeypatch, daily=daily, daily_basic=pd.DataFrame())
    out = tushare_daily.fetch_daily_all("2026-08-18")
    assert len(out) == 1 and pd.isna(out.iloc[0]["turnover"])


def test_fetch_daily_all_empty_raises(monkeypatch):
    _install_fake_tushare(monkeypatch, daily=pd.DataFrame())
    with pytest.raises(ConnectionError):
        tushare_daily.fetch_daily_all("2026-08-16")   # 非交易日/未收盘


def test_fetch_chip(monkeypatch):
    cyq = pd.DataFrame({
        "ts_code": ["000001.SZ"], "trade_date": ["20260818"],
        "winner_rate": [96.3], "cost_95pct": [10.9],
    })
    _install_fake_tushare(monkeypatch, cyq=cyq)
    out = tushare_daily.fetch_chip("2026-08-18")
    assert list(out["code"]) == ["000001"]
    assert float(out.iloc[0]["winner_rate"]) == 96.3
    assert float(out.iloc[0]["cost_95pct"]) == 10.9


def test_pro_requires_token(monkeypatch):
    monkeypatch.setattr(settings, "TUSHARE_TOKEN", "")
    with pytest.raises(RuntimeError):
        tushare_daily.fetch_daily_all("2026-08-18")
