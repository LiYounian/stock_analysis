"""BOLL(布林带)单测:构造已知序列,锁口径语义(计划文档1 F1)。

锁死:20周期 / 2σ / 总体标准差 ddof=0(通达信口径);带宽=(上−下)/中轨;
%B 越轨边界(>1 破上轨 / <0 破下轨);挤压=带宽处于近 N 日低分位;compute 输出 boll 块。
"""
import numpy as np
import pandas as pd

from tools.analysis import technical as ta
from tools.config.strategy import THRESHOLDS


def _kline(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * n, "amount": [c * 1000 for c in closes],
        "turnover": [0.05] * n,
        "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })


def test_boll_formula_20_2sigma_ddof0():
    """中轨=20日SMA;上/下轨=中轨±2×总体标准差(ddof=0),不是样本std(ddof=1)。"""
    close = pd.Series(np.arange(1, 41, dtype=float))       # 1..40
    bl = ta.boll(close, 20, 2.0)
    last20 = close.iloc[-20:].to_numpy()
    mid = last20.mean()
    std_pop = np.std(last20, ddof=0)                        # 总体标准差
    std_sample = np.std(last20, ddof=1)                     # 样本标准差(用于反证)
    assert abs(bl["mid"].iloc[-1] - mid) < 1e-9
    assert abs(bl["upper"].iloc[-1] - (mid + 2 * std_pop)) < 1e-9
    assert abs(bl["lower"].iloc[-1] - (mid - 2 * std_pop)) < 1e-9
    # 反证:若误用样本std(ddof=1),上轨会明显不同 → 锁死 ddof=0
    assert abs(bl["upper"].iloc[-1] - (mid + 2 * std_sample)) > 1e-6


def test_bandwidth_formula():
    """带宽 = (上轨 − 下轨) / 中轨。"""
    close = pd.Series(np.arange(1, 41, dtype=float))
    bl = ta.boll(close, 20, 2.0)
    up, lo, mid = bl["upper"].iloc[-1], bl["lower"].iloc[-1], bl["mid"].iloc[-1]
    assert abs(bl["bandwidth"].iloc[-1] - (up - lo) / mid) < 1e-9


def test_percent_b_break_upper():
    """价冲破上轨 → %B > 1 → 位置='破上轨'。"""
    closes = [10.0] * 19 + [20.0]
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    assert bl["percent_b"].iloc[-1] > 1.0
    st = ta._boll_state(bl)
    assert st["位置"] == "破上轨"


def test_percent_b_break_lower():
    """价跌破下轨 → %B < 0 → 位置='破下轨'。"""
    closes = [10.0] * 19 + [2.0]
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    assert bl["percent_b"].iloc[-1] < 0.0
    st = ta._boll_state(bl)
    assert st["位置"] == "破下轨"


def test_percent_b_mid_neutral():
    """末值恰等于 20 日均值 → %B=0.5 → 位置='中性'。

    构造:末值 = 前 19 根均值时,含末值的 20 根窗口均值恰等于末值(代数恒等),%B=0.5。
    """
    first19 = list(np.linspace(9.0, 11.0, 19))          # 均值=10
    closes = first19 + [float(np.mean(first19))]         # 末值=前19根均值 → 窗口均值=末值
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    st = ta._boll_state(bl)
    assert abs(bl["percent_b"].iloc[-1] - 0.5) < 1e-9
    assert st["位置"] == "中性"


def test_squeeze_true_when_recent_calm():
    """近端转平静(带宽收窄到近 N 日低分位)→ 挤压=True。"""
    rng = np.sin(np.linspace(0, 12 * np.pi, 120)) * 3 + 20   # 前120根大幅震荡
    calm = np.full(60, 20.0)                                  # 后60根几乎恒定
    closes = np.concatenate([rng, calm])
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    st = ta._boll_state(bl)
    assert st["挤压"] is True


def test_squeeze_false_when_recent_volatile():
    """近端波动放大(带宽处于近 N 日高位)→ 挤压=False。"""
    amp = np.linspace(0.5, 5.0, 180)                          # 振幅随时间放大
    closes = 20 + amp * np.sin(np.linspace(0, 18 * np.pi, 180))
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    st = ta._boll_state(bl)
    assert st["挤压"] is False


def test_compute_boll_block_present():
    """technical.compute 输出含 boll 块 + 关键字段。"""
    closes = list(np.linspace(10.0, 15.0, 60))
    out = ta.compute(_kline(closes))
    assert "boll" in out
    for key in ("上轨", "中轨", "下轨", "带宽", "percent_b", "位置", "挤压"):
        assert key in out["boll"]
    # 上轨 ≥ 中轨 ≥ 下轨
    assert out["boll"]["上轨"] >= out["boll"]["中轨"] >= out["boll"]["下轨"]


def test_boll_insufficient_data_no_crash():
    """不足 20 根 → 位置='数据不足',不报错。"""
    closes = [10.0, 11.0, 12.0]
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    st = ta._boll_state(bl)
    assert st["位置"] == "数据不足"
    assert st["挤压"] is False


def test_config_boll_params():
    """BOLL 参数真源在 THRESHOLDS['BOLL'],口径=20/2.0。"""
    t = THRESHOLDS["BOLL"]
    assert t["周期"] == 20 and t["倍数"] == 2.0
    assert t["触轨上_percentB"] == 0.8 and t["触轨下_percentB"] == 0.2
