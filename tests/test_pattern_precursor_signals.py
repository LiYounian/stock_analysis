"""形态选股·金叉前兆 触发件/趋势门 单测。

锁语义（防未来 + 交叉判定正确性），防未来 prompt/代码重写时无意破坏：
  1. 截断不变性（防未来硬红线）：第 t 行信号，用全历史 vs 只用 ≤t 的 K 线算必须一致。
  2. _cross_up 精确落在交叉那一根，不早不晚。
  3. 触发件/趋势门与手算一致。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import pattern_precursor_signals as sig


def _synth_kline(n: int = 400, seed: int = 7) -> pd.DataFrame:
    """造一段有涨有跌的合成日线（含均线金叉/死叉切换）。"""
    rng = np.random.default_rng(seed)
    # 叠加正弦趋势 + 噪声，制造多次均线交叉
    t = np.arange(n)
    trend = 20 + 5 * np.sin(t / 25.0) + 0.01 * t
    close = trend + rng.normal(0, 0.4, n)
    close = np.maximum(close, 1.0)
    high = close + np.abs(rng.normal(0, 0.3, n))
    low = close - np.abs(rng.normal(0, 0.3, n))
    open_ = close + rng.normal(0, 0.2, n)
    vol = rng.integers(1e5, 5e5, n).astype(float)
    dates = pd.date_range("2022-01-01", periods=n, freq="B")
    return pd.DataFrame({"date": dates, "open": open_, "high": high,
                         "low": low, "close": close, "volume": vol})


def test_cross_up_exact_bar():
    a = pd.Series([1, 1, 1, 2, 3, 3])   # 上穿发生在 idx3
    b = pd.Series([2, 2, 2, 1, 1, 1])
    x = sig._cross_up(a, b)
    assert list(x.fillna(False)) == [False, False, False, True, False, False]


def test_no_lookahead_truncation_invariance():
    """第 t 行的所有触发件/趋势门布尔值，用全历史算 == 只用 kline[:t+1] 算。
    这是防未来的硬测试：任何回看未来的实现都会在此失败。"""
    kl = _synth_kline()
    full = sig.compute_signal_frame(kl)
    cols = list(sig.TRIGGERS[:3]) + ["trig_reso", "gate_A", "gate_B", "gate_C"]
    # 取几个远超预热期的 t
    for t in (120, 200, 275, 333, 399):
        trunc = sig.compute_signal_frame(kl.iloc[: t + 1].reset_index(drop=True))
        for col in cols:
            assert bool(full.iloc[t][col]) == bool(trunc.iloc[t][col]), (
                f"截断不变性破坏 col={col} t={t}: full={full.iloc[t][col]} trunc={trunc.iloc[t][col]}")


def test_kdj_trigger_matches_manual():
    """KDJ 金叉触发件 = K 上穿 D 且 J 上行，与直接调 technical.kdj 手算一致。"""
    from tools.analysis import technical
    kl = _synth_kline(seed=11)
    frame = sig.compute_signal_frame(kl)
    kd = technical.kdj(kl)
    K, D, J = kd["k"], kd["d"], kd["j"]
    manual = (K > D) & (K.shift(1) <= D.shift(1)) & (J > J.shift(1))
    # 预热期后逐行一致
    m = manual.iloc[60:].fillna(False).reset_index(drop=True)
    f = frame["trig_kdj"].iloc[60:].fillna(False).reset_index(drop=True)
    assert (m == f).all()


def test_gate_C_bull_stack():
    """档C 多头排列门 = MA5≥MA10≥MA20≥MA60，与手算一致。"""
    from tools.analysis import technical
    kl = _synth_kline(seed=3)
    frame = sig.compute_signal_frame(kl)
    c = kl["close"]
    ma5, ma10, ma20, ma60 = (technical.ma(c, w) for w in (5, 10, 20, 60))
    manual = (ma5 >= ma10) & (ma10 >= ma20) & (ma20 >= ma60)
    m = manual.iloc[60:].fillna(False).reset_index(drop=True)
    f = frame["gate_C"].iloc[60:].fillna(False).reset_index(drop=True)
    assert (m == f).all()


def test_passes_gate_N_is_trigger_only():
    row = pd.Series({"trig_kdj": True, "gate_A": False, "gate_B": False, "gate_C": False})
    assert sig.passes(row, "trig_kdj", "gate_N") is True
    assert sig.passes(row, "trig_kdj", "gate_A") is False
    row2 = pd.Series({"trig_macd": False})
    assert sig.passes(row2, "trig_macd", "gate_N") is False


def test_resonance_needs_two_triggers():
    """共振：近2日内≥2个不同触发件 且 当日至少1个新触发。"""
    kl = _synth_kline(seed=5)
    frame = sig.compute_signal_frame(kl)
    trig3 = frame[["trig_kdj", "trig_ma5x10", "trig_macd"]].astype(int)
    for t in range(60, len(kl)):
        if frame.iloc[t]["trig_reso"]:
            # 当日必有新触发
            assert trig3.iloc[t].sum() >= 1
            # 近2日并集≥2
            union = (trig3.iloc[t - 1:t + 1].max()).sum()
            assert union >= sig.RESONANCE_MIN
