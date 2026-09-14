"""锁语义:capitulation 回测的防未来函数 / 口径 / 一般性(约法6)。

这些断言锁住"为什么这么做"的语义,防未来 prompt/代码重写时无意破坏:
① trailing 分位因果(日 t 的判定不随 >t 数据变化);
② 前向收益与 event_study 口径一致 + 越界不结算;
③ α = 子集 − 全样本等权(pp);
④ 超卖共振镜像 technical._overbought_oversold;
⑤ capitulation 定义**不含任何硬编码例日/例票分支**(一般性)。
"""
from __future__ import annotations

import pathlib

import numpy as np
import pandas as pd
import pytest

from tools.backtest.capitulation import breadth_features as bf
from tools.backtest.capitulation import forward as fwd
from tools.backtest.capitulation.indicators import _oversold_flags


# ————————————————————————— ① 因果性 —————————————————————————
def _synth_breadth(n=800, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    mp = rng.normal(0, 1.2, n)
    osr = np.clip(rng.normal(0.5, 0.15, n), 0.01, 0.99)
    return pd.DataFrame({"mean_pct": mp, "below_ma20_ratio": osr,
                         "net_adv": rng.normal(0, 0.3, n)}, index=idx)


def test_trailing_quantile_is_causal():
    """日 t 的 capitulation 判定只用 ≤t 的数据:改动 >t 的行不改变 ≤t 的标记。"""
    b = _synth_breadth()
    full = bf.build_capitulation_flags(b)
    cut = 700
    tcut = b.index[cut]
    # 截断到 ≤t 重算
    trunc = bf.build_capitulation_flags(b.loc[:tcut])
    key = bf._grid_key(0.90, 0.10)
    a = full.loc[:tcut, key].reset_index(drop=True)
    c = trunc[key].reset_index(drop=True)
    assert a.equals(c), "trailing 分位泄露未来:截断重算与全序列在 ≤t 不一致"


def test_warmup_no_events():
    b = _synth_breadth()
    f = bf.build_capitulation_flags(b, trailing=500)
    # 前 500 行(warmup)不得有任何 capitulation 事件
    warm = f.index[f["warmup"]]
    for _, _ in bf.GRID and []:
        pass
    key = bf._grid_key(0.90, 0.10)
    assert not f.loc[warm, key].any(), "warmup 期不应产生 capitulation 事件"


# ————————————————————————— ② 前向收益 —————————————————————————
def test_forward_matches_event_study():
    """forward_returns_panel(lag=1) 与 event_study.forward_returns 数值一致(pp vs 分数)。"""
    from tools.backtest.event_study import forward_returns as es_fwd
    idx = pd.bdate_range("2020-01-01", periods=20)
    close = pd.Series(np.linspace(10, 30, 20), index=idx)
    panel = pd.DataFrame({"AAA": close})
    signal = idx[5]
    got = fwd.forward_returns_panel(panel, signal, horizons=(1, 5), lag=1)
    # event_study 进场=事件日;要对齐 lag=1,事件日传 signal 的次一交易日
    entry_day = idx[6]
    es = es_fwd([entry_day], panel.reset_index().rename(columns={"index": "date"})
                .rename(columns={"AAA": "close"})[["date", "close"]], windows=(1, 5))
    for N in (1, 5):
        mine = got[N]["AAA"] / 100.0            # pp → 分数
        # event_study 内部 round 到 6dp → 用 abs 容差对齐
        assert es[0]["前瞻"][N] == pytest.approx(float(mine), abs=1e-5)


def test_forward_overflow_not_settled():
    idx = pd.bdate_range("2020-01-01", periods=8)
    panel = pd.DataFrame({"AAA": pd.Series(np.arange(1, 9, dtype=float), index=idx)})
    got = fwd.forward_returns_panel(panel, idx[6], horizons=(1, 5), lag=1)
    assert len(got[5]) == 0, "越界窗口必须不结算(空),不得编造"


# ————————————————————————— ③ α 口径 —————————————————————————
def test_alpha_is_subset_minus_universe_pp():
    idx = pd.bdate_range("2020-01-01", periods=5)
    # 3 票,进场=idx[1](lag=1,信号 idx[0]),N=1 看 idx[2]/idx[1]
    panel = pd.DataFrame({
        "A": [10, 10, 11, 0, 0],   # r=+10%
        "B": [10, 10, 12, 0, 0],   # r=+20%
        "C": [10, 10, 9, 0, 0],    # r=-10%
    }, index=idx).astype(float)
    res = fwd.alpha_for_subset(panel, idx[0], subset_codes=["A", "B"], horizons=(1,))
    r = res[1]
    assert r["benchmark"] == pytest.approx((10 + 20 - 10) / 3)   # 全样本等权 =6.667pp
    assert r["subset_mean"] == pytest.approx(15.0)               # (10+20)/2
    assert r["alpha"] == pytest.approx(15.0 - (10 + 20 - 10) / 3)


# ————————————————————————— ④ 超卖镜像 —————————————————————————
def test_oversold_mirrors_resonance():
    """镜像 _overbought_oversold:≥2 票超卖且 os≥ob 才判超卖。"""
    # k<20 且 rsi<30 且 bias<-10 → 3 票超卖 → True
    df = _oversold_flags(bias20=pd.Series([-15.0]), rsi12=pd.Series([25.0]),
                         k=pd.Series([10.0]), j=pd.Series([5.0]))
    assert bool(df["oversold"].iloc[0])
    # 仅 bias 超卖(1 票)→ 不足共振 → False
    df2 = _oversold_flags(bias20=pd.Series([-15.0]), rsi12=pd.Series([50.0]),
                          k=pd.Series([50.0]), j=pd.Series([50.0]))
    assert not bool(df2["oversold"].iloc[0])
    # NaN 视为不成立
    df3 = _oversold_flags(bias20=pd.Series([np.nan]), rsi12=pd.Series([25.0]),
                          k=pd.Series([10.0]), j=pd.Series([np.nan]))
    # 仅 rsi 超卖(k<20 记一票,j NaN)→ kdj(k<20)=1 + rsi=1 = 2 票 → True
    assert bool(df3["oversold"].iloc[0])


# ————————————————————————— ⑤ 一般性(无例日/例票硬编码) —————————————————————————
def test_no_hardcoded_examples():
    """capitulation 包源码不得出现动机例日/例票——防对着已知样本调参。"""
    pkg = pathlib.Path(__file__).resolve().parents[1] / "tools" / "backtest" / "capitulation"
    banned = ["2026-09-14", "2026-09-02", "20260914", "20260902",
              "002913", "002064", "688262", "002811", "603270"]
    for py in pkg.glob("*.py"):
        txt = py.read_text(encoding="utf-8")
        for b in banned:
            assert b not in txt, f"{py.name} 含硬编码例日/例票 {b}(违反一般性红线)"
