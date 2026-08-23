"""指标条件化预测单测(计划文档1 F3/F4)。

锁死:①向量化池标签与逐日 state_vector 严格一致 ②无未来函数(od_N ≤ as_of 才入池)
③匹配阶梯 精确→放宽1→放宽2→退回 + min样本 ④r_N 未实现丢弃 ⑤方向映射。
"""
import numpy as np
import pandas as pd

from tools.analysis import conditional_predict as cp
from tools.analysis import indicator_state as ist
from tools.analysis import technical as ta


def _kline(closes):
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1000.0] * n, "amount": [c * 1000 for c in closes],
        "turnover": [0.05] * n,
        "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })


def _rand_closes(n, seed):
    rng = np.random.RandomState(seed)
    return list(10 * np.cumprod(1 + rng.normal(0, 0.02, n)))


# ---------- ① 向量化池标签 == 逐日 state_vector ----------
def test_pool_labels_match_state_vector():
    """建池的向量化 3 主维度,必须与逐日 technical.compute+state_vector 的 primary_key 逐字段一致。"""
    df = _kline(_rand_closes(300, seed=7))
    trend, mom, boll = cp._pool_labels(df)
    for t in (80, 150, 250):
        sl = df.iloc[:t + 1].reset_index(drop=True)
        pk = ist.primary_key(ist.state_vector(sl, ta.compute(sl)))
        assert (trend.iloc[t], mom.iloc[t], boll.iloc[t]) == pk, f"t={t}"


# ---------- 手工池:精确控制匹配 ----------
def _pool_rows(trend, mom, boll, n, r5=1.0, od5=None):
    od5 = od5 or pd.Timestamp("2020-06-01")
    return pd.DataFrame({
        "code": ["x"] * n, "date": [pd.Timestamp("2020-01-10")] * n,
        "trend": [trend] * n, "mom": [mom] * n, "boll": [boll] * n,
        "r1": [r5] * n, "r5": [r5] * n, "r10": [r5] * n,
        "od1": [od5] * n, "od5": [od5] * n, "od10": [od5] * n,
    })


def _fixed_sv(monkeypatch, trend, mom, boll):
    monkeypatch.setattr(cp.ist, "state_vector", lambda kl, tech: {
        "主维度": {"趋势方向": trend, "动量状态": mom, "BOLL位置": boll}, "二级维度": {}})


AS_OF = pd.Timestamp("2020-12-31")


def test_ladder_exact(monkeypatch):
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    pool = _pool_rows("多头", "强", "中性", 10)
    kl = _kline(_rand_closes(60, 1))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=3)
    assert out["5日"]["放宽层级"] == "精确" and out["5日"]["是否退回"] is False
    assert out["5日"]["相似样本数"] == 10


def test_ladder_relax1_boll(monkeypatch):
    """无精确匹配,但 趋势+动量 匹配(BOLL 不同)≥min → 放宽1。"""
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    pool = _pool_rows("多头", "强", "触上轨", 10)     # BOLL 不同
    kl = _kline(_rand_closes(60, 2))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=3)
    assert out["5日"]["放宽层级"] == "放宽1"


def test_ladder_relax2_trend(monkeypatch):
    """只有 趋势 匹配 → 放宽2。"""
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    pool = _pool_rows("多头", "弱", "触下轨", 10)     # 仅趋势同
    kl = _kline(_rand_closes(60, 3))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=3)
    assert out["5日"]["放宽层级"] == "放宽2"


def test_ladder_fallback_when_no_trend_match(monkeypatch):
    """趋势都不匹配 → 退回无条件,是否退回=True。"""
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    pool = _pool_rows("空头", "弱", "触下轨", 100)
    kl = _kline(_rand_closes(60, 4))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=3)
    assert out["5日"]["放宽层级"] == "退回" and out["5日"]["是否退回"] is True


def test_fallback_when_pool_none(monkeypatch):
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    kl = _kline(_rand_closes(60, 5))
    out = cp.conditional_scenarios(kl, {}, None, AS_OF, horizons=(5,), min_samples=3)
    assert out["5日"]["是否退回"] is True


# ---------- ② 无未来函数:od_N ≤ as_of 才入池 ----------
def test_no_future_filter(monkeypatch):
    """结局日在 as_of 之后的样本必须被排除。"""
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    past = _pool_rows("多头", "强", "中性", 4, od5=pd.Timestamp("2020-06-01"))     # 已实现
    future = _pool_rows("多头", "强", "中性", 6, od5=pd.Timestamp("2021-06-01"))   # 结局在未来
    pool = pd.concat([past, future], ignore_index=True)
    kl = _kline(_rand_closes(60, 6))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=1)
    assert out["5日"]["相似样本数"] == 4      # 只数已实现的 4 条,未来 6 条剔除


# ---------- ④ r_N 未实现丢弃 ----------
def test_unrealized_rn_dropped(monkeypatch):
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    good = _pool_rows("多头", "强", "中性", 3)
    bad = _pool_rows("多头", "强", "中性", 5)
    bad["r5"] = np.nan                        # r5 未实现
    pool = pd.concat([good, bad], ignore_index=True)
    kl = _kline(_rand_closes(60, 7))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=1)
    assert out["5日"]["相似样本数"] == 3      # NaN 的 5 条丢弃,不填 0


# ---------- ⑤ 方向映射 F4 ----------
def test_direction_view_mapping():
    cond = {
        "1日": {"上涨概率%": 65.0, "放宽层级": "精确", "是否退回": False},
        "5日": {"上涨概率%": 35.0, "放宽层级": "放宽1", "是否退回": False},
        "10日": {"上涨概率%": 50.0, "放宽层级": "放宽2", "是否退回": False},
    }
    dv = cp.direction_view(cond)
    assert dv["1日"]["方向"] == "看涨" and dv["1日"]["置信度"] == "高"
    assert dv["5日"]["方向"] == "看跌" and dv["5日"]["置信度"] == "中"
    assert dv["10日"]["方向"] == "中性" and dv["10日"]["置信度"] == "低"


def test_direction_view_fallback_low_conf():
    cond = {"5日": {"上涨概率%": 70.0, "放宽层级": "退回", "是否退回": True}}
    dv = cp.direction_view(cond)
    assert dv["5日"]["置信度"] == "低"       # 退回→低置信,即便概率高


def test_percentile_quantiles_present(monkeypatch):
    """精确匹配时输出含 q7/q50/q93 区间 + 期望。"""
    _fixed_sv(monkeypatch, "多头", "强", "中性")
    pool = _pool_rows("多头", "强", "中性", 50)
    pool["r5"] = list(np.linspace(-5, 5, 50))
    kl = _kline(_rand_closes(60, 8))
    out = cp.conditional_scenarios(kl, {}, pool, AS_OF, horizons=(5,), min_samples=3)["5日"]
    assert "悲观%(q7)" in out and "中位%(q50)" in out and "乐观%(q93)" in out and "期望%" in out
    assert out["悲观%(q7)"] < out["中位%(q50)"] < out["乐观%(q93)"]
