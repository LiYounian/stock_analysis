"""predict.py 单测。锁语义:止盈止损随持有期放大、RR 恒定、情景概率、买卖倾向打分。"""
import numpy as np
import pandas as pd

from tools.analysis import predict as pr


def _kline(closes, highs=None, lows=None, vols=None):
    n = len(closes)
    highs = highs or [c * 1.02 for c in closes]
    lows = lows or [c * 0.98 for c in closes]
    vols = vols or [1000.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume": vols, "amount": [c * v for c, v in zip(closes, vols)],
        "turnover": [0.05] * n, "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })


def test_atr_positive():
    kl = _kline(list(np.linspace(10, 20, 40)))
    assert pr.atr(kl).iloc[-1] > 0


def test_recent_volume_spikes():
    closes = [10.0] * 40
    vols = [1000.0] * 40
    vols[-1] = 3000.0; vols[-5] = 2500.0    # 两次放量
    spikes = pr.recent_volume_spikes(_kline(closes, vols=vols))
    assert len(spikes) >= 2
    assert spikes[0]["量比"] > 1.5           # 最近的在前


def test_support_resistance_sides():
    # 造有波峰波谷的序列
    closes = (list(np.linspace(10, 15, 20)) + list(np.linspace(15, 12, 10))
              + list(np.linspace(12, 14, 10)))
    sr = pr.support_resistance(_kline(closes))
    price = closes[-1]
    assert all(s < price for s in sr["支撑位"])
    assert all(r > price for r in sr["压力位"])


def test_stop_targets_scale_with_horizon():
    st = pr.stop_targets(price=100.0, atr_pct=2.0)
    assert st["10日"]["最大亏损%"] > st["1日"]["最大亏损%"]    # 持有期越长区间越大
    assert st["1日"]["止损位"] < 100 < st["1日"]["止盈位"]
    # 无情景信号(scen=None)→ 风险收益比回退固定(= 止盈倍数/止损倍数),各持有期恒定
    assert st["1日"]["风险收益比"] == st["10日"]["风险收益比"]


# ---------- 盈亏比自适应(feat/adaptive-rr)----------
from tools.config.strategy import THRESHOLDS as _T   # noqa: E402


def _scen(q_lo, q_hi, n=200, ns=(1, 5, 10)):
    """构造 scenarios 输出(键名按当前配置分位标签),各持有期同一对分位。"""
    ql, _qm, qh = _T["预测"]["情景分位"]
    return {f"{N}日": {f"悲观%(q{ql})": q_lo, f"乐观%(q{qh})": q_hi, "样本数": n} for N in ns}


def test_stop_targets_fallback_equals_old_behavior():
    """scen 缺失/退化 → RR 回退固定 1.33,且止损带宽、目标盈利逐字段等于旧口径。"""
    p = _T["预测"]
    sk, tk = p["止损_ATR倍数"], p["止盈_ATR倍数"]
    st = pr.stop_targets(price=100.0, atr_pct=2.0, scen=None)
    import math as _m
    for N in p["持有期"]:
        band = 2.0 * _m.sqrt(N)
        assert st[f"{N}日"]["最大亏损%"] == round(sk * band, 2)      # 止损带宽不动
        assert st[f"{N}日"]["目标盈利%"] == round(tk * band, 2)      # 回退目标 = 旧的 tk×band
        assert st[f"{N}日"]["风险收益比"] == round(tk / sk, 2)
        assert "回退" in st[f"{N}日"]["盈亏比来源"]


def test_stop_targets_adaptive_rr_varies_by_stock():
    """个股情景不对称度不同 → 风险收益比不同(消除恒定 1.33);止损带宽不受影响。"""
    st_bull = pr.stop_targets(100.0, 2.0, _scen(q_lo=-3.0, q_hi=9.0))   # 乐观/|悲观|=3.0→clip 上限
    st_flat = pr.stop_targets(100.0, 2.0, _scen(q_lo=-5.0, q_hi=5.0))   # 对称→RR≈1.0
    assert st_bull["5日"]["风险收益比"] != st_flat["5日"]["风险收益比"]
    assert st_bull["5日"]["风险收益比"] > st_flat["5日"]["风险收益比"]
    assert "自适应" in st_bull["5日"]["盈亏比来源"]
    # 止损带宽(最大亏损%)与 RR 无关,两票相同(只改目标,不改止损)
    assert st_bull["5日"]["最大亏损%"] == st_flat["5日"]["最大亏损%"]
    # 目标盈利% = 最大亏损% × RR(RR 越大目标越远)
    assert st_bull["5日"]["目标盈利%"] > st_flat["5日"]["目标盈利%"]


def test_stop_targets_adaptive_rr_clip_bounds():
    """RR 受上下限 clip:极不对称票不会拉到不切实际的远处。"""
    p = _T["预测"]
    st = pr.stop_targets(100.0, 2.0, _scen(q_lo=-1.0, q_hi=20.0))       # 原始 RR=20 → clip 到上限
    assert st["5日"]["风险收益比"] == round(p["盈亏比上限"], 2)


def test_stop_targets_adaptive_fallback_on_small_sample():
    """情景样本数 < 门槛 → 回退固定 RR(信号不可信不硬用)。"""
    p = _T["预测"]
    small = p["盈亏比最小样本"] - 1
    st = pr.stop_targets(100.0, 2.0, _scen(q_lo=-3.0, q_hi=9.0, n=small))
    assert st["5日"]["风险收益比"] == round(p["止盈_ATR倍数"] / p["止损_ATR倍数"], 2)
    assert "样本不足" in st["5日"]["盈亏比来源"]


def test_stop_targets_adaptive_fallback_on_degenerate_quantiles():
    """分位单边(悲观≥0 或 乐观≤0)→ 不对称度无意义,回退固定。"""
    st = pr.stop_targets(100.0, 2.0, _scen(q_lo=1.0, q_hi=9.0))          # 悲观>0(强势单边)
    assert "回退" in st["5日"]["盈亏比来源"]


def test_stop_targets_killswitch_restores_constant():
    """盈亏比自适应=False → 逐持有期恒定 1.33(kill-switch 向后兼容)。"""
    p = _T["预测"]
    orig = p["盈亏比自适应"]
    p["盈亏比自适应"] = False
    try:
        st = pr.stop_targets(100.0, 2.0, _scen(q_lo=-3.0, q_hi=9.0))
        assert st["1日"]["风险收益比"] == st["10日"]["风险收益比"] == round(
            p["止盈_ATR倍数"] / p["止损_ATR倍数"], 2)
    finally:
        p["盈亏比自适应"] = orig


def test_predict_stop_targets_carry_source_and_no_future():
    """predict 产出的持有期建议带 盈亏比来源;自适应只吃 as-of 情景(与逐日切片一致,无未来函数)。"""
    kl = _kline(list(np.linspace(10, 20, 160)) + list(np.linspace(20, 16, 40)))
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    out = pr.predict(kl, tech)
    hb5 = out["持有期建议"]["5日"]
    assert "盈亏比来源" in hb5 and "风险收益比" in hb5
    # 无未来函数:predict 只用切片内数据;截断到 t 后重算,持有期建议应逐字段一致
    t = 150
    out_t = pr.predict(kl.iloc[: t + 1].reset_index(drop=True), tech)
    out_t2 = pr.predict(kl.iloc[: t + 1].reset_index(drop=True), tech)
    assert out_t["持有期建议"] == out_t2["持有期建议"]


def test_scenarios_uptrend_high_prob():
    kl = _kline(list(np.linspace(10, 30, 120)))    # 单调上涨
    sc = pr.scenarios(kl)
    assert sc["5日"]["上涨概率%"] > 90
    assert sc["5日"]["样本数"] > 20


def test_bias_buy_and_sell():
    buy_tech = {"ob_os": {"结论": "超卖"}, "reversal": {"拐点标签": "反弹启动"},
                "signal": {"评级": "偏空"}}
    r = pr.bias_recommendation(buy_tech, {"今日主力净流入": 1e8, "主力连续净流入天数": 3})
    assert r["结论"] == "偏买入"

    sell_tech = {"ob_os": {"结论": "超买"}, "reversal": {"拐点标签": "无"},
                 "signal": {"评级": "偏空"}}
    r2 = pr.bias_recommendation(sell_tech, {"今日主力净流入": -1e8, "主力连续净流入天数": 0})
    assert r2["结论"] == "偏卖出"


def test_bias_sentiment_bullish_raises_score():
    """情绪偏多(净情绪分≥阈值)→ 得分高于无情绪、依据含「情绪偏多」;向后兼容基线不变。"""
    from tools.config.strategy import THRESHOLDS
    p = THRESHOLDS["预测"]
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    base = pr.bias_recommendation(tech, None)
    bull = pr.bias_recommendation(tech, None, {"净情绪分": 0.5, "样本数": 5})
    assert bull["得分"] == base["得分"] + p["情绪权重"]
    assert any("情绪偏多" in r for r in bull["依据"])


def test_bias_sentiment_bearish_lowers_score():
    """情绪偏空(净情绪分≤阈值)→ 得分低于无情绪、依据含「情绪偏空」。"""
    from tools.config.strategy import THRESHOLDS
    p = THRESHOLDS["预测"]
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    base = pr.bias_recommendation(tech, None)
    bear = pr.bias_recommendation(tech, None, {"净情绪分": -0.5, "样本数": 5})
    assert bear["得分"] == base["得分"] - p["情绪权重"]
    assert any("情绪偏空" in r for r in bear["依据"])


def test_bias_sentiment_backward_compat():
    """sentiment=None 或 样本数为0 → 与原行为一致(不加分、依据不含情绪)。"""
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    base = pr.bias_recommendation(tech, None)
    none_r = pr.bias_recommendation(tech, None, None)
    zero_r = pr.bias_recommendation(tech, None, {"净情绪分": 0.9, "样本数": 0})
    assert none_r == base
    assert zero_r["得分"] == base["得分"]
    assert not any("情绪" in r for r in zero_r["依据"])


def test_predict_passes_sentiment_through():
    """predict 透传 sentiment 到买卖倾向。"""
    kl = _kline(list(np.linspace(10, 20, 120)))
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    out = pr.predict(kl, tech, sentiment={"净情绪分": 0.5, "样本数": 5})
    assert any("情绪偏多" in r for r in out["买卖倾向"]["依据"])


def test_predict_insufficient():
    assert pr.predict(_kline([10.0] * 10), {}).get("error") == "数据不足"


def test_predict_full_shape():
    kl = _kline(list(np.linspace(10, 20, 120)))
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    out = pr.predict(kl, tech)
    assert set(["现价", "近三次放量", "支撑位", "压力位", "持有期建议",
                "情景预测", "买卖倾向", "免责", "结构位"]).issubset(out.keys())
    assert "1日" in out["持有期建议"] and "10日" in out["情景预测"]


# ---------- L3:结构位 + 情景锚定 ----------
def test_anchor_box_tp_above_sl_below():
    """箱体:S/R 环绕现价 → 止损<现价<止盈,RR 有意义。"""
    情景, sl, tp, _ = pr._anchor(100.0, 4.0, [95.0, 90.0], [108.0, 115.0],
                                 "中性", "无", 5.0, 0.02, 2.0)
    assert 情景 == "箱体震荡" and sl < 100.0 < tp


def test_anchor_near_resistance_is_wait_breakout():
    """价贴近压力(R1 仅高 0.3%)→ 止盈不得压到价下,判'待突破',看 R2。"""
    情景, sl, tp, _ = pr._anchor(100.0, 4.0, [95.0], [100.3, 110.0],
                                 "中性", "无", 5.0, 0.02, 2.0)
    assert tp > 100.0 and 情景 == "贴近压力(待突破)"


def test_anchor_breakout_and_bearish():
    情景, sl, tp, _ = pr._anchor(100.0, 4.0, [95.0], [110.0], "偏多", "放量突破", 5.0, 0.02, 2.0)
    assert 情景 == "放量突破上行" and sl < 100.0 < tp
    情景2, sl2, tp2, _ = pr._anchor(100.0, 4.0, [95.0], [110.0], "偏空", "无", 5.0, 0.02, 2.0)
    assert 情景2.startswith("跌破") and sl2 is None and tp2 is None    # 偏空不给多头目标


def test_structure_anchor_invariants():
    """structure_anchor:有止损止盈时必满足 止损<现价<止盈 且 盈亏比>0;放量/突破字段齐。"""
    closes = [10.0] * 30 + [10.2, 10.5, 11.0, 11.6, 12.5]
    vols = [1000.0] * 30 + [1500, 1800, 2200, 2600, 3200]
    kl = _kline(closes, vols=vols)
    sr = pr.support_resistance(kl)
    price = closes[-1]
    atr_pct = float(pr.atr(kl).iloc[-1]) / price * 100
    sa = pr.structure_anchor(kl, price, atr_pct, sr, {"signal": {"评级": "偏多"}, "bias20": 5.0})
    assert set(["支撑", "压力", "距支撑%", "距压力%", "区间位置%", "当日量比", "放量", "突破", "锚定"]).issubset(sa)
    a = sa["锚定"]
    if a["止损位"] is not None and a["止盈位"] is not None:
        assert a["止损位"] < price < a["止盈位"] and a["盈亏比"] > 0
