"""technical.py 单测(构造已知序列,锁指标语义)。"""
import numpy as np
import pandas as pd

from tools.analysis import technical as ta


def _kline(closes, highs=None, lows=None, vols=None):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1000.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": vols, "amount": [c * v for c, v in zip(closes, vols)],
        "turnover": [0.05] * n, "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })


def test_ma():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ta.ma(s, 3).iloc[-1] == 4.0        # (3+4+5)/3


def test_sma_cn_recursion():
    """SMA(X,N,1):首值作种子,后续递推。"""
    x = pd.Series([10.0, 10.0, 10.0])
    y = ta._sma_cn(x, 3, 1)
    assert y.iloc[0] == 10.0 and y.iloc[-1] == 10.0   # 常数序列恒为该值


def test_rsi_bounds_and_extremes():
    """单调上涨 → RSI 趋近 100;单调下跌 → 趋近 0;区间恒在 [0,100]。"""
    up = ta.rsi(pd.Series(np.arange(1, 40, dtype=float)), 6)
    down = ta.rsi(pd.Series(np.arange(40, 1, -1, dtype=float)), 6)
    assert up.iloc[-1] > 99
    assert down.iloc[-1] < 1
    assert up.dropna().between(0, 100).all()


def test_macd_zero_on_constant():
    """常数价 → DIF=DEA=0,柱=0。"""
    md = ta.macd(pd.Series([10.0] * 50))
    assert abs(md.iloc[-1]["dif"]) < 1e-9
    assert abs(md.iloc[-1]["macd"]) < 1e-9


def test_kdj_range():
    """KDJ 的 K 落在 [0,100];随机波动不越界。"""
    closes = (10 + np.sin(np.linspace(0, 6, 40))).tolist()
    kd = ta.kdj(_kline(closes))
    assert kd["k"].dropna().between(0, 100).all()


def test_compute_uptrend_bullish():
    """稳定上涨序列 → 均线多头 + 综合评级偏多。"""
    closes = np.linspace(10, 20, 70).tolist()
    res = ta.compute(_kline(closes))
    assert res["ma"]["排列"] == "多头排列"
    assert res["signal"]["评级"] == "偏多"
    assert res["signal"]["得分"] > 0


def test_compute_downtrend_bearish():
    """稳定下跌序列 → 均线空头 + 综合评级偏空。"""
    closes = np.linspace(20, 10, 70).tolist()
    res = ta.compute(_kline(closes))
    assert res["ma"]["排列"] == "空头排列"
    assert res["signal"]["评级"] == "偏空"
    assert res["signal"]["得分"] < 0


def test_compute_insufficient_data():
    """K线过短 → 标数据不足,不报错。"""
    res = ta.compute(_kline([10.0]))
    assert res.get("error") == "数据不足"


def test_reversal_oversold_bounce():
    """深跌后末日放量反包 → 拐点标签=反弹启动,且趋势仍偏空(两维度并列不互掩)。"""
    closes = list(np.linspace(20, 10, 40))          # 长期下跌
    closes[-1] = closes[-2] * 1.09                  # 末日大涨反包
    highs = [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = [1000.0] * 40
    vols[-1] = 3000.0                               # 末日放量
    res = ta.compute(_kline(closes, highs, lows, vols))
    rev = res["reversal"]
    assert rev["放量反包"] is True
    assert rev["拐点标签"] == "反弹启动"
    assert rev["拐点评分"] >= 50
    # 拐点在非多头趋势下照样触发 → 证明两维度独立,拐点没搭多头顺风车
    assert res["signal"]["评级"] != "偏多"


def test_reversal_none_on_uptrend():
    """稳定上涨(不超跌、不放量反包)→ 拐点标签=无,不误报。"""
    closes = list(np.linspace(10, 20, 70))
    res = ta.compute(_kline(closes))
    assert res["reversal"]["拐点标签"] == "无"
    assert res["reversal"]["超跌"] is False
