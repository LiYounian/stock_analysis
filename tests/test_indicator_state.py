"""指标状态向量单测(计划文档1 F2/F2b)。

锁死:主维度3元组(二级维度不入主相似度量)、动量分档、KDJ钝化=连续N日、KDJ交叉(低位/高位/交叉点抬高降低)、
RSI/MACD顶底背离、BOLL形态(开口/缩口/沿轨)、无未来函数(未来行改动不影响 ≤t 状态)。
"""
import numpy as np
import pandas as pd

from tools.analysis import indicator_state as st
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


# ---------- 主维度 / 结构 ----------
def test_state_vector_structure_and_primary_key():
    closes = list(np.linspace(10.0, 15.0, 60))
    kl = _kline(closes)
    sv = st.state_vector(kl, ta.compute(kl))
    assert set(sv["主维度"]) == {"趋势方向", "动量状态", "BOLL位置"}
    assert set(sv["二级维度"]) == {"KDJ", "RSI背离", "MACD背离", "BOLL形态"}
    pk = st.primary_key(sv)
    assert pk == (sv["主维度"]["趋势方向"], sv["主维度"]["动量状态"], sv["主维度"]["BOLL位置"])
    assert len(pk) == 3   # 只有3主维度,二级维度不入 A2 相似度量


def test_momentum_strong_mid_weak():
    cfg = THRESHOLDS["指标状态"]
    assert st._momentum({"macd": {"状态": "金叉"}, "rsi": {"rsi12": 60}}, cfg) == "强"
    assert st._momentum({"macd": {"状态": "死叉"}, "rsi": {"rsi12": 40}}, cfg) == "弱"
    assert st._momentum({"macd": {"状态": "多头"}, "rsi": {"rsi12": 50}}, cfg) == "中"


# ---------- F2b:KDJ 钝化 ----------
def test_kdj_dullness_high_low_none():
    n = THRESHOLDS["指标状态"]["KDJ钝化连续天数"]
    assert st._kdj_dullness(pd.Series([85.0] * n), n) == "高位钝化"
    assert st._kdj_dullness(pd.Series([10.0] * n), n) == "低位钝化"
    assert st._kdj_dullness(pd.Series([85.0, 50.0, 85.0]), n) == "无"   # 非连续
    assert st._kdj_dullness(pd.Series([85.0]), n) == "无"               # 数据不足


# ---------- F2b:KDJ 交叉 ----------
def test_kdj_cross_golden_low_position():
    k = pd.Series([10.0, 10.0, 10.0, 30.0])
    d = pd.Series([20.0, 20.0, 20.0, 20.0])
    r = st._kdj_cross(k, d, 3)
    assert r["最近交叉"] == "金叉" and r["位置"] == "低位" and r["新近"] is True


def test_kdj_cross_point_rising():
    """相邻两次金叉,后一次 K 更高 → 交叉点趋势=抬高(动能增强)。"""
    k = pd.Series([10.0, 30.0, 10.0, 40.0])
    d = pd.Series([20.0, 20.0, 20.0, 20.0])
    r = st._kdj_cross(k, d, 3)
    assert r["最近交叉"] == "金叉" and r["交叉点趋势"] == "抬高"


def test_kdj_cross_dead_high_position():
    k = pd.Series([80.0, 80.0, 80.0, 60.0])
    d = pd.Series([70.0, 70.0, 70.0, 70.0])
    r = st._kdj_cross(k, d, 3)
    assert r["最近交叉"] == "死叉" and r["位置"] == "高位"


# ---------- F2b:背离 ----------
def test_divergence_bottom():
    """价创新低但指标抬高 → 底背离。"""
    price = np.full(30, 10.0); price[10] = 5.0; price[25] = 4.0     # 近段低点(4)< 前段低点(5)
    ind = np.full(30, 25.0); ind[10] = 20.0; ind[25] = 30.0        # 指标近段低点(30)> 前段(20)
    assert st._divergence(pd.Series(price), pd.Series(ind), 30, 10) == "底背离"


def test_divergence_top():
    """价创新高但指标走低 → 顶背离。"""
    price = np.full(30, 10.0); price[10] = 15.0; price[25] = 16.0   # 近段高点(16)> 前段(15)
    ind = np.full(30, 25.0); ind[10] = 30.0; ind[25] = 20.0        # 指标近段高点(20)< 前段(30)
    assert st._divergence(pd.Series(price), pd.Series(ind), 30, 10) == "顶背离"


def test_divergence_none():
    price = np.linspace(10, 12, 30)                                 # 单调,无背离
    ind = np.linspace(20, 40, 30)
    assert st._divergence(pd.Series(price), pd.Series(ind), 30, 10) == "无"


# ---------- F2b:BOLL 形态 ----------
def test_boll_form_squeeze_passthrough():
    """%B 收在中部(非沿轨)+ squeeze=True → 缩口。"""
    closes = list(10 + 0.3 * np.sin(np.linspace(0, 4 * np.pi, 40)))  # 末值≈中轨,%B~0.5
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    assert st._boll_form(bl, squeeze=True) == "缩口"


def test_boll_form_ride_upper():
    """陡峭上行 → %B 连续高位 → 沿上轨。"""
    closes = list(np.linspace(10.0, 40.0, 60))
    bl = ta.boll(pd.Series(closes), 20, 2.0)
    assert st._boll_form(bl, squeeze=False) == "沿上轨"


def test_boll_form_expand():
    """近端波动率突然放大(带宽较5根前 ≥1.2倍)→ 开口(且非沿轨、非挤压)。"""
    base = list(10 + 0.02 * np.arange(44))          # 44 根近平(低带宽)
    lvl = base[-1]
    swings = [lvl + 3, lvl - 3, lvl + 3, lvl - 3, lvl + 3, lvl - 3]  # 末端大幅震荡
    bl = ta.boll(pd.Series(base + swings), 20, 2.0)
    assert st._boll_form(bl, squeeze=False) == "开口"


# ---------- F2b:MA 止跌锚(structure_anchor)----------
def test_ma_support_anchor_in_structure():
    """上行趋势中 MA10/20/60 位于现价下方 → 结构位.均线支撑 收录并按就近排序。"""
    from tools.analysis import predict as pred
    closes = list(np.linspace(10.0, 20.0, 60))       # 上行 → 价在各均线上方
    kl = _kline(closes)
    out = pred.predict(kl, ta.compute(kl))
    anchors = out["结构位"]["均线支撑"]
    assert len(anchors) >= 1
    price = out["现价"]
    for a in anchors:
        assert a["价"] < price and a["距今%"] > 0
    assert [a["距今%"] for a in anchors] == sorted(a["距今%"] for a in anchors)  # 就近排序
    assert {a["名称"] for a in anchors} <= {"MA10", "MA20", "MA60"}


# ---------- 无未来函数 ----------
def test_no_future_function():
    """改动 t 之后的未来行,不影响 ≤t 的状态向量(严格无未来函数)。"""
    rng = np.sin(np.linspace(0, 8 * np.pi, 200)) * 2 + 20
    full = _kline(list(rng))
    t = 120
    sv_a = st.state_vector(full.iloc[:t + 1].reset_index(drop=True),
                           ta.compute(full.iloc[:t + 1]))
    tampered = full.copy()
    tampered.loc[t + 1:, "close"] = 999.0        # 篡改未来
    tampered.loc[t + 1:, "high"] = 1010.0
    sv_b = st.state_vector(tampered.iloc[:t + 1].reset_index(drop=True),
                           ta.compute(tampered.iloc[:t + 1]))
    assert sv_a == sv_b
