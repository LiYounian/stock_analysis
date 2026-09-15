"""锁 backtest_strong 的等价性与防未来函数语义(约法第6条:断言锁住"为什么改")。

核心断言:
  · 向量化 C1/C2/C3 + 近期大涨次数 与生产 screen_strong.signal_at 逐点一致(等价性,防重写漂移)。
  · C4 判据 c4_of 与 signal_at 的 ④ 分支同真值(winner_rate>阈值 或 high≥cost_95pct)。
  · 前向收益/α 只用 t 及 t+N 的价,t+N 越界 → NaN(防未来函数,未到期不编造)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import backtest_strong as bs
from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_strong

_CFG = THRESHOLDS["最强选股"]
_PERIODS = [int(p) for p in _CFG["均线多头周期"]]


def _synth_kline(n=400, seed=7) -> pd.DataFrame:
    """造一段带趋势 + 偶发大涨的合成 K线(足够长以触发 C1/C3 的 250 根窗口)。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.002, 0.02, n)
    ret[::37] += 0.06  # 周期性单日大涨,触发 C2
    close = 10.0 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    dates = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame({"date": dates, "open": close, "high": high, "low": low,
                         "close": close, "volume": 1e6, "amount": 1e7,
                         "turnover": 1.0, "pct_chg": ret * 100})


def test_cond123_equivalent_to_signal_at():
    """向量化 C1/C2/C3 与近期大涨次数,对每根 t 都与 signal_at 逐点相等。"""
    df = _synth_kline()
    feat = bs.precompute_features(df, _PERIODS, (1, 5))
    c1, c2, c3, big = bs.cond123(feat, _PERIODS)
    kdf = df.reset_index(drop=True)
    need = int(_CFG["最少历史根数"])
    checked = 0
    for t in range(need - 1, len(df)):
        r = screen_strong.signal_at(kdf, t, chip=None)
        if "C1_六均线多头" not in r:  # 历史不足等早退分支
            continue
        assert bool(c1[t]) == r["C1_六均线多头"], f"C1@{t}"
        assert bool(c2[t]) == r["C2_近期连涨"], f"C2@{t}"
        assert bool(c3[t]) == r["C3_高位区间"], f"C3@{t}"
        assert int(big[t]) == r["明细"]["近期大涨次数"], f"big@{t}"
        checked += 1
    assert checked > 50, f"有效对比点太少({checked})"


def test_c4_matches_signal_at():
    """c4_of 与 signal_at 的 ④ 分支同真值(多组 winner_rate/cost95/high 组合)。"""
    df = _synth_kline()
    kdf = df.reset_index(drop=True)
    t = len(df) - 1
    high_t = float(df["high"].iloc[t])
    cases = [
        {"winner_rate": 99.0, "cost_95pct": high_t * 2},   # 仅 wr 命中
        {"winner_rate": 50.0, "cost_95pct": high_t * 0.5}, # 仅 high≥cost95 命中
        {"winner_rate": 50.0, "cost_95pct": high_t * 2},   # 都不命中
        {"winner_rate": 96.0, "cost_95pct": high_t * 0.9}, # 都命中
    ]
    for c in cases:
        r = screen_strong.signal_at(kdf, t, chip=c)
        expect = r["C4_筹码获利"]
        got = bs.c4_of(c["winner_rate"], c["cost_95pct"], high_t)
        assert got == expect, f"C4 mismatch for {c}: got {got} expect {expect}"


def test_forward_return_no_lookahead():
    """前向 r_N:t+N 越界 → NaN;在界内 = close[t+N]/close[t]-1(不回看、不编造)。"""
    df = _synth_kline(n=100)
    feat = bs.precompute_features(df, _PERIODS, (1, 5))
    close = df["close"].to_numpy(float)
    r5 = feat["fwd"][5]
    assert np.isnan(r5[-1]) and np.isnan(r5[-5])  # 尾部 5 根无 t+5
    t = 40
    assert abs(r5[t] - (close[t + 5] / close[t] - 1.0)) < 1e-9


def test_oc_next_day_intraday_no_lookahead():
    """次日 oc 口径:oc[t]=close[t+1]/open[t+1]-1(次日开盘买、次日收盘卖);末根越界→NaN。"""
    df = _synth_kline(n=100)
    feat = bs.precompute_features(df, _PERIODS, (1, 5))
    close = df["close"].to_numpy(float)
    open_ = df["open"].to_numpy(float)
    oc = feat["oc"]
    assert np.isnan(oc[-1])                              # 末根无 t+1
    t = 40
    assert abs(oc[t] - (close[t + 1] / open_[t + 1] - 1.0)) < 1e-9
    assert feat["exec_date"][t] == feat["dates"][t + 1]  # 交易发生在次日
