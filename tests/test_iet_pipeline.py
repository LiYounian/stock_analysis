"""IET 向量化面板构建 单测(monkeypatch readers,锁因果与聚合语义,守则6)。

锁定:
  - _asof_matrix: direction=backward, 只取≤date(防未来), 早于最早值→NaN。
  - build_panel_snapshot: 行业中位/均值, PE剔≤0, n_members<下限弃权。
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PL


def test_asof_matrix_causal(monkeypatch):
    ser = {
        "A": pd.DataFrame({"date": ["2024-01-02", "2024-01-10"], "PE_TTM": [10.0, 20.0]}),
        "B": pd.DataFrame({"date": ["2024-01-05"], "PE_TTM": [30.0]}),
    }
    monkeypatch.setattr(D, "_valuation_series", lambda c: ser.get(c))
    dates = ["2024-01-01", "2024-01-06", "2024-01-12"]
    m = PL._asof_matrix(["A", "B"], dates, D._valuation_series, "PE_TTM")
    # A: 01-01 早于最早 → NaN; 01-06 取 01-02 的 10; 01-12 取 01-10 的 20(不取未来)
    assert np.isnan(m.loc["2024-01-01", "A"])
    assert m.loc["2024-01-06", "A"] == 10.0
    assert m.loc["2024-01-12", "A"] == 20.0
    # B: 01-06 取 01-05 的 30
    assert m.loc["2024-01-06", "B"] == 30.0
    assert np.isnan(m.loc["2024-01-01", "B"])


def test_build_panel_snapshot(monkeypatch):
    # 电子3只(A,B,C) 银行1只(D) → 银行<5弃权
    val = {
        "A": pd.DataFrame({"date": ["2024-01-01"], "PE_TTM": [10.0], "PB": [1.0]}),
        "B": pd.DataFrame({"date": ["2024-01-01"], "PE_TTM": [20.0], "PB": [2.0]}),
        "C": pd.DataFrame({"date": ["2024-01-01"], "PE_TTM": [-5.0], "PB": [3.0]}),  # PE≤0剔
        "D": pd.DataFrame({"date": ["2024-01-01"], "PE_TTM": [8.0], "PB": [1.0]}),
    }
    kl = {c: pd.DataFrame({"date": ["2024-01-01", "2024-01-02"], "turnover": [1.0, 2.0]})
          for c in ["A", "B", "C", "D"]}
    monkeypatch.setattr(D, "_valuation_series", lambda c: val.get(c))
    monkeypatch.setattr(D, "_kline", lambda c: kl.get(c))
    membership = {"A": "电子", "B": "电子", "C": "电子", "D": "银行"}
    df = PL.build_panel_snapshot(membership, ["2024-01-01", "2024-01-02"], min_members=3)
    assert set(df["industry"]) == {"电子"}          # 银行1只<3弃权
    r1 = df[df["date"] == "2024-01-01"].iloc[0]
    assert r1["n_members"] == 3
    assert r1["pe_median"] == 15.0                   # median(10,20), C的-5被剔
    assert r1["pb_median"] == 2.0                    # median(1,2,3)
    assert r1["turnover_mean"] == pytest.approx(1.0)
    r2 = df[df["date"] == "2024-01-02"].iloc[0]
    assert r2["turnover_mean"] == pytest.approx(2.0)  # as-of 01-02
