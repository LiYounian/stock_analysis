"""形态选股·金叉前兆 回测引擎（限价撮合 + 退出 + 涨停约束）单测。

锁次日实盘口径的成交语义，防重写破坏：
  · match_entry：跳空成交更优价 / 回踩成交限价 / 未回踩弃单 / 涨停一字不可买。
  · simulate_exit：止盈/止损/时间止损 + 同日双触保守 + 绝对价 + 扣成本 + 卖方涨停顺延。
"""
from __future__ import annotations

import numpy as np

from tools.backtest import backtest_pattern_precursor as bt


def _rec(rows):
    """rows = [(open,high,low,close), ...] → PriceBook.get 风格元组。"""
    a = np.array(rows, float)
    dmap = {f"2026-01-{i+1:02d}": i for i in range(len(rows))}
    return (a[:, 0], a[:, 1], a[:, 2], a[:, 3], dmap)


# ---------- match_entry ----------
def test_entry_fill_on_retrace():
    # 昨收=10(idx0)；dip0 → P_entry=10。次日 low=9.5≤10、open=10.2>10 → 成交于 P_entry=10。
    rec = _rec([(10, 10, 10, 10), (10.2, 10.5, 9.5, 10.1)])
    px, mark = bt.match_entry(rec, 0, dip_pct=0.0, code="600000")
    assert px == 10.0 and "回踩" in mark


def test_entry_fill_gap_down_better_price():
    # 次日跳空低开 open=9.6 ≤ P_entry=10 → 成交于 open(更优)。
    rec = _rec([(10, 10, 10, 10), (9.6, 9.8, 9.4, 9.7)])
    px, mark = bt.match_entry(rec, 0, dip_pct=0.0, code="600000")
    assert px == 9.6 and "跳空" in mark


def test_entry_no_retrace_abandon():
    # dip1% → P_entry=9.9。次日全天最低 low=10.1 > 9.9 → 未回踩弃单。
    rec = _rec([(10, 10, 10, 10), (10.2, 10.6, 10.1, 10.4)])
    px, mark = bt.match_entry(rec, 0, dip_pct=1.0, code="600000")
    assert px is None and mark == bt.S_NO_RETRACE


def test_entry_limit_up_oneword_abandon():
    # 次日涨停一字(open=high=low=close=11=昨收*1.1,主板10%) → 买不进弃单。
    rec = _rec([(10, 10, 10, 10), (11, 11, 11, 11)])
    px, mark = bt.match_entry(rec, 0, dip_pct=0.0, code="600000")
    assert px is None and mark == bt.S_LIMIT_UP


# ---------- simulate_exit ----------
def test_exit_take_profit_absolute():
    # entry=10, tp8% → tp_price=10.8。第2持有日 high=11≥10.8 → 止盈成交于10.8。
    # idx=0 入场在 idx+1；扫 k=1..5。
    rows = [(10, 10, 10, 10)]                      # idx0 信号日
    rows += [(10, 10.2, 9.8, 10.0)]                # k1 入场日,未触
    rows += [(10, 11.0, 10.0, 10.9)]               # k2 high=11≥10.8 止盈
    rows += [(10, 10, 10, 10)] * 5
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.2, code="600000")
    assert out["exit_reason"] == bt.R_TP
    assert abs(out["gross_pct"] - 8.0) < 1e-6
    assert abs(out["net_pct"] - 7.8) < 1e-6      # 扣成本0.2
    assert out["hold_days"] == 2


def test_exit_stop_loss():
    # entry=10, sl5% → sl_price=9.5。k1 low=9.4≤9.5 → 止损成交于9.5, gross=-5%。
    rows = [(10, 10, 10, 10), (10, 10.1, 9.4, 9.6)] + [(10, 10, 10, 10)] * 5
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.2, code="600000")
    assert out["exit_reason"] == bt.R_SL
    assert abs(out["gross_pct"] + 5.0) < 1e-6


def test_exit_same_day_ambiguous_conservative():
    # k1 同日 high=11(≥10.8止盈) 且 low=9.4(≤9.5止损) → 保守取止损。
    rows = [(10, 10, 10, 10), (10, 11.0, 9.4, 10.0)] + [(10, 10, 10, 10)] * 5
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.0, code="600000")
    assert out["exit_reason"] == bt.R_SL_AMBIG and out["path_ambiguous"] is True
    assert abs(out["gross_pct"] + 5.0) < 1e-6


def test_exit_time_stop_at_close():
    # 全程不触线 → 第5持有日收盘了结。
    rows = [(10, 10, 10, 10)] + [(10, 10.3, 9.7, 10.1)] * 5 + [(10, 10, 10, 10)]
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.0, code="600000")
    assert out["exit_reason"] == bt.R_TIME and out["hold_days"] == 5
    assert abs(out["gross_pct"] - 1.0) < 1e-6      # 收盘10.1/10-1 = +1%


def test_exit_seller_limit_up_postpones():
    # k1 触止盈但当日涨停一字(卖不出) → 顺延；k2 正常触止盈成交。
    rows = [(10, 10, 10, 10)]
    rows += [(10.8, 10.8, 10.8, 10.8)]             # k1 涨停一字(=entry*1.08<主板10%? 需≥10%才算涨停)
    # 用主板10%涨停:昨收(k0)=10 → 涨停=11。构造 k1 一字=11(≥tp10.8) 但涨停卖不出
    rows[1] = (11, 11, 11, 11)
    rows += [(10.9, 11.2, 10.7, 11.0)]             # k2 high=11.2≥10.8 且有振幅 → 正常止盈于10.8
    rows += [(10, 10, 10, 10)] * 4
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.0, code="600000")
    assert out["exit_reason"] == bt.R_TP and out["hold_days"] == 2   # 顺延到k2


def test_exit_immature_out_of_range():
    rows = [(10, 10, 10, 10), (10, 10, 10, 10)]     # idx+time_stop 越界
    rec = _rec(rows)
    out = bt.simulate_exit(rec, 0, entry=10.0, tp_pct=8.0, sl_pct=5.0, time_stop=5,
                           cost_pct=0.0, code="600000")
    assert out["matured"] is False


# ---------- _oneword_up ----------
def test_oneword_up_detection():
    assert bt._oneword_up(11, 11, 11, 10, "600000") is True        # 零振幅+涨停10%
    assert bt._oneword_up(11, 10.9, 11, 10, "600000") is False     # 有振幅
    assert bt._oneword_up(30.6, 30.6, 30.6, 25.5, "300001") is True  # 创业板20%
