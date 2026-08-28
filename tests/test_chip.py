"""chip.py 单测(纯本地推演,不触网)。

锁语义:上涨行情下获利比例高、平均成本低于现价;换手率缺失→降级 None;
成本区间单调、集中度 ∈ (0,1)。
"""
import numpy as np
import pandas as pd

from tools.collectors import chip


def _synth(n=120, up=True):
    """合成一段带换手率的日 K线(默认单边上涨)。"""
    rng = np.random.RandomState(1)
    base = np.linspace(10, 15, n) if up else np.linspace(15, 10, n)
    base = base + rng.randn(n) * 0.15
    rows = []
    for i in range(n):
        c = float(base[i]); o = c * (1 + rng.randn() * 0.01)
        h = max(o, c) * 1.01; l = min(o, c) * 0.99
        vol = 1e6 * (1 + rng.rand())
        rows.append(dict(date=pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                         open=o, high=h, low=l, close=c, volume=vol,
                         amount=vol * c, turnover=2.0 + rng.rand(), pct_chg=0.0))
    return pd.DataFrame(rows)


def test_summarize_uptrend():
    s = chip.summarize(_synth(up=True))
    assert 0.0 <= s["获利比例"] <= 1.0
    assert s["获利比例"] > 0.6                      # 上涨→多数筹码浮盈
    assert s["平均成本"] < s["现价"]                 # 成本重心低于现价
    assert s["成本区间下沿"] < s["成本区间上沿"]
    assert 0.0 < s["集中度90"] < 1.0


def test_missing_turnover_degrades():
    df = _synth().drop(columns=["turnover"])
    s = chip.summarize(df)
    assert s["获利比例"] is None and s["平均成本"] is None


def test_short_series_degrades():
    assert chip.compute_distribution(_synth(n=10)) is None      # 不足 20 根


def test_distribution_normalized():
    prices, chips = chip.compute_distribution(_synth())
    assert abs(float(chips.sum()) - 1.0) < 1e-6                  # 归一
    assert (chips >= 0).all()
