import pandas as pd

from tools.pipeline import screen_max_range as mr


def _df(n=250, code="000001"):
    close = [100.0] * (n - 32) + [101.0] * 30 + [108.0, 108.5]
    return pd.DataFrame({"date": pd.date_range("2025-01-01", periods=n),
                         "close": close, "low": [99.0] * n}), code


def test_formula_selects_matching_stock():
    df, code = _df()
    result = mr.signal_latest(df, code)
    assert result["SELECT"] is True
    assert result["明细"]["32日涨超6%次数"] >= 1


def test_finance3_two_equivalent_keeps_002_and_excludes_bj_prefix():
    df, _ = _df()
    result = mr.signal_latest(df, "002001")
    assert result["SELECT"] is True
    assert result["明细"]["checks"]["非北交所(FINANCE(3)!=2)"] is True
    result = mr.signal_latest(df, "830001")
    assert result["SELECT"] is False
    assert result["明细"]["checks"]["非北交所(FINANCE(3)!=2)"] is False


def test_history_short_is_not_selected():
    df, code = _df(249)
    assert mr.signal_latest(df, code)["SELECT"] is False
