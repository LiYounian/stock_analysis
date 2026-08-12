"""预测两条线回测 harness 的语义锁(约法6)。

锁三件,防未来重写误删:
  1. 先摸判定的同日 tie 规则 = 保守归 SL(A股跌得快);时间上先到者胜。
  2. **无未来函数**:改 t 之后的行,不改 t 时刻预测侧字段(entry/止损止盈/上涨概率/分位),
     只改前瞻结局字段(r_N / touch)。这是 walk-forward 的命门。
  3. 区间覆盖率口径合法(0~100,且 覆盖+跌破+冲破≈100)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.backtest import backtest_predict as bp


def test_first_touch_tie_is_sl():
    # 同日两条都触及 → 保守判 SL
    assert bp._first_touch(np.array([10.0]), np.array([1.0]), sl=2.0, tp=9.0) == "SL"
    # 只触止盈
    assert bp._first_touch(np.array([10.0]), np.array([5.0]), sl=2.0, tp=9.0) == "TP"
    # 只触止损
    assert bp._first_touch(np.array([8.0]), np.array([1.0]), sl=2.0, tp=9.0) == "SL"
    # 都不触
    assert bp._first_touch(np.array([8.0, 8.5]), np.array([5.0, 5.5]), sl=2.0, tp=9.0) == "NEITHER"
    # 时间先到者胜:第2天先摸止盈(前一天啥都没碰)
    assert bp._first_touch(np.array([8.0, 9.5]), np.array([5.0, 5.0]), sl=2.0, tp=9.0) == "TP"
    # sl 为 None → 只看 tp
    assert bp._first_touch(np.array([10.0]), np.array([1.0]), sl=None, tp=9.0) == "TP"


def _synth_kline(n=90, seed=7):
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.001, 0.02, n)
    close = 20.0 * np.cumprod(1 + ret)
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    vol = rng.integers(1e6, 5e6, n).astype(float)
    dates = pd.bdate_range("2024-01-01", periods=n)
    close_s = pd.Series(close)
    return pd.DataFrame({"date": dates, "open": close, "high": high,
                         "low": low, "close": close, "volume": vol,
                         "pct_chg": (close_s.pct_change() * 100).fillna(0.0)})


def test_no_lookahead(monkeypatch):
    """改 t 之后的未来行,t 时刻的预测侧字段必须纹丝不动(前瞻结局可变)。"""
    df = _synth_kline()
    monkeypatch.setattr(bp.market, "load_kline", lambda code: df.copy())
    base = bp.build_panel(["X"], horizons=(5,), warmup=40)
    assert not base.empty

    # 篡改一个较晚交易日的未来价格(制造极端未来)
    tamper_t = 60
    df2 = df.copy()
    df2.loc[tamper_t:, ["high", "close"]] = df2.loc[tamper_t:, ["high", "close"]] * 3.0
    df2.loc[tamper_t:, "low"] = df2.loc[tamper_t:, "low"] * 3.0
    monkeypatch.setattr(bp.market, "load_kline", lambda code: df2.copy())
    tampered = bp.build_panel(["X"], horizons=(5,), warmup=40)

    pred_cols = ["entry", "br_loss", "br_gain", "up_p", "q_lo", "q_mid", "q_hi"]
    # 只比较预测**完全在篡改点之前**的行(t+N <= tamper_t):这些行的前瞻窗口都没碰到未来篡改
    a = base[base["t"] + base["N"] <= tamper_t].set_index("t")[pred_cols]
    b = tampered[tampered["t"] + tampered["N"] <= tamper_t].set_index("t")[pred_cols]
    common = a.index.intersection(b.index)
    assert len(common) > 5
    pd.testing.assert_frame_equal(a.loc[common], b.loc[common], check_like=True)


def test_coverage_ratio_wellformed():
    df = _synth_kline(n=120)
    import types
    monkey = types.SimpleNamespace()
    orig = bp.market.load_kline
    bp.market.load_kline = lambda code: df.copy()          # noqa: E731
    try:
        panel = bp.build_panel(["X"], horizons=(5,), warmup=40)
        cov = bp._coverage(panel[panel["N"] == 5])
    finally:
        bp.market.load_kline = orig
    assert cov["n"] > 0
    assert 0 <= cov["覆盖率%"] <= 100
    # 覆盖 + 跌破下沿 + 冲破上沿 ≈ 100
    assert abs(cov["覆盖率%"] + cov["跌破下沿%"] + cov["冲破上沿%"] - 100) < 1e-6


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
