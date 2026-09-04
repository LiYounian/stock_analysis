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


def test_missing_turnover_is_loud_not_silent():
    """近端整段 turnover 缺失时:①summarize 结果带 `降级=True`/`换手缺失日`>0(不静默当 0);
    ②仍能出值(覆盖率>50%),但被标记降级 —— 锁死 #21(缺失静默当 tr=0 无告警)。"""
    df = _synth(n=120, up=True)
    df.loc[df.index[-16:], "turnover"] = np.nan       # 末 16 日换手缺失(仿 fallback 近端)
    s = chip.summarize(df)
    assert s["降级"] is True
    assert s["换手缺失日"] == 16
    assert s["集中度90"] is not None                  # 覆盖率仍 >50% → 有值但降级


def test_full_turnover_not_degraded():
    """换手完整 → 降级=False、换手缺失日=0(标记默认阴性,不误报)。"""
    s = chip.summarize(_synth(up=True))
    assert s["降级"] is False and s["换手缺失日"] == 0


def test_degrade_flag_present_even_when_none():
    """完全无 turnover 列 → 全 None 的降级路径也带 `降级`/`换手缺失日` 键(schema 一致)。"""
    s = chip.summarize(_synth().drop(columns=["turnover"]))
    assert s["获利比例"] is None
    assert s["降级"] is True and s["换手缺失日"] > 0
