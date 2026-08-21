import pandas as pd

from tools.collectors import tushare_daily


def test_tushare_daily_normalizes_all_market_rows(monkeypatch):
    class Pro:
        def daily(self, **kwargs):
            assert kwargs["trade_date"] == "20260818"
            return pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260818",
                                  "open": 10, "high": 11, "low": 9, "close": 10.5,
                                  "vol": 12, "amount": 34, "pct_chg": 1.2}])
    class TS:
        @staticmethod
        def pro_api(token):
            assert token == "test-token"
            return Pro()
    monkeypatch.setattr(tushare_daily.settings, "TUSHARE_TOKEN", "test-token")
    monkeypatch.setitem(__import__("sys").modules, "tushare", TS)
    out = tushare_daily.fetch_daily_all("2026-08-18")
    assert out.loc[0, "code"] == "000001"
    assert out.loc[0, "volume"] == 1200
    assert out.loc[0, "amount"] == 34000
