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
    # 风险收益比恒定(= 止盈倍数/止损倍数)
    assert st["1日"]["风险收益比"] == st["10日"]["风险收益比"]


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


def test_predict_insufficient():
    assert pr.predict(_kline([10.0] * 10), {}).get("error") == "数据不足"


def test_predict_full_shape():
    kl = _kline(list(np.linspace(10, 20, 120)))
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    out = pr.predict(kl, tech)
    assert set(["现价", "近三次放量", "支撑位", "压力位", "持有期建议",
                "情景预测", "买卖倾向", "免责"]).issubset(out.keys())
    assert "1日" in out["持有期建议"] and "10日" in out["情景预测"]
