"""锁 eval_v3.1 离场语义(防未来 prompt/代码重写无意删规则)。

覆盖离场触发口径:
  ① 盘中止盈=在**触线价**成交(不是当天收盘价)
  ② 盘中止跌=触线价成交
  ③ 同日高摸止盈∧低摸止跌 → 保守先触止跌 + path_ambiguous 标记
  ④ 时间止损=第 time_stop 日收盘卖(退出锚 idx+time_stop,与固定持有 horizon=N 对齐)
  ⑤ 危险信号离场=策略当日不再选该票 → 当日收盘卖(纯 as-of),关开关则不触发
  ⑥ 往返成本双档从毛收益扣
  ⑦ 防未来函数:idx+time_stop 越界 → 未成熟(不用未来价)
合成数据,离线可跑。
"""
from __future__ import annotations

import pandas as pd

from tools.backtest.eval_v3 import exit_sim, prices


def _book(prices_by_code):
    def loader(code):
        return pd.DataFrame(prices_by_code[code],
                            columns=["date", "open", "high", "low", "close"])
    return prices.PriceBook(loader=loader)


def _idx2date(rec):
    return {i: d for d, i in rec[4].items()}


# ─────────────── ① 盘中止盈=触线价(非收盘) ───────────────
def test_take_profit_fills_at_trigger_not_close():
    """入场 100,止盈 +8%→触线价 108。某日 high=110 摸到 108,当天收盘仅 101。
    离场收益必须记 +8%(触线价),不是 +1%(收盘)。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),   # T
        ("d1", 100, 110, 99, 101.0),    # 入场 open=100;high=110≥108 触止盈;close=101
        ("d2", 101, 112, 100, 111.0),
        ("d3", 111, 113, 110, 112.0),
        ("d4", 112, 114, 111, 113.0),
        ("d5", 113, 115, 112, 114.0),
    ]})
    rec = book.get("X")
    r = exit_sim.simulate_long_exit(rec, 0, tp_pct=8, sl_pct=5, time_stop=5, cost_pct=0.0)
    assert r["exit_reason"] == exit_sim.R_TP
    assert abs(r["gross_pct"] - 8.0) < 1e-6          # 触线价 108 → +8%,非收盘 +1%
    assert r["hold_days"] == 1 and r["path_ambiguous"] is False


# ─────────────── ② 盘中止跌=触线价 ───────────────
def test_stop_loss_fills_at_trigger():
    """入场 100,止跌 −5%→触线价 95。某日 low=90 击穿,记 −5%(触线价,非收盘更低)。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 101, 90, 92.0),     # 入场 100;low=90≤95 触止跌;close=92
        ("d2", 92, 93, 88, 90.0),
        ("d3", 90, 91, 85, 88.0),
        ("d4", 88, 89, 84, 86.0),
        ("d5", 86, 87, 83, 85.0),
    ]})
    r = exit_sim.simulate_long_exit(book.get("X"), 0, tp_pct=8, sl_pct=5, time_stop=5)
    assert r["exit_reason"] == exit_sim.R_SL
    assert abs(r["gross_pct"] - (-5.0)) < 1e-6       # 触线价 95 → −5%,非收盘 −8%


# ─────────────── ③ 同日双触 → 保守先止跌 + path_ambiguous ───────────────
def test_same_day_both_touched_assumes_stop_loss_and_flags():
    """同日 high≥止盈线 且 low≤止跌线:日K 看不出先后 → 保守记止跌 + path_ambiguous。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 112, 90, 101.0),    # high=112≥108(止盈) 且 low=90≤95(止跌)→ 双触
        ("d2", 101, 113, 100, 111.0),
        ("d3", 111, 114, 110, 112.0),
        ("d4", 112, 115, 111, 113.0),
        ("d5", 113, 116, 112, 114.0),
    ]})
    r = exit_sim.simulate_long_exit(book.get("X"), 0, tp_pct=8, sl_pct=5, time_stop=5)
    assert r["exit_reason"] == exit_sim.R_SL_AMBIG
    assert r["path_ambiguous"] is True
    assert abs(r["gross_pct"] - (-5.0)) < 1e-6       # 保守取止跌线 −5%


# ─────────────── ④ 时间止损=第 N 日收盘 ───────────────
def test_time_stop_sells_at_close_of_Nth_day():
    """全程不触线 → 时间止损:第 time_stop 日收盘卖,退出锚 idx+time_stop。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 101, 99, 100.5),    # 入场 100,窄幅震荡不触 ±(8/5)%
        ("d2", 100.5, 101.5, 99.5, 101.0),
        ("d3", 101, 102, 100, 101.5),
        ("d4", 101.5, 102.5, 100.5, 102.0),
        ("d5", 102, 103, 101, 102.5),   # time_stop=5 → 收盘 102.5
    ]})
    r = exit_sim.simulate_long_exit(book.get("X"), 0, tp_pct=8, sl_pct=5, time_stop=5)
    assert r["exit_reason"] == exit_sim.R_TIME
    assert r["hold_days"] == 5 and r["exit_idx"] == 5
    assert abs(r["gross_pct"] - (102.5 / 100.0 - 1) * 100) < 1e-6


# ─────────────── ⑤ 危险信号离场(as-of;开关) ───────────────
def test_danger_signal_exit_at_close_when_deselected():
    """策略在 T+2 不再选该票 → 当日(T+2)收盘离场;开开关才触发。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 101, 99, 100.5),    # T+1 仍选
        ("d2", 100.5, 102, 99.5, 101.0),  # T+2 被剔除 → 收盘 101 离场
        ("d3", 101, 103, 100, 102.0),
        ("d4", 102, 104, 101, 103.0),
        ("d5", 103, 105, 102, 104.0),
    ]})
    rec = book.get("X")
    i2d = _idx2date(rec)
    selected = {"d1": True, "d2": False}   # d2 起被剔除

    def is_sel(bar_date):
        return selected.get(bar_date)      # 未登记→None(非预测日,继续持有)

    r = exit_sim.simulate_long_exit(rec, 0, tp_pct=8, sl_pct=5, time_stop=5,
                                    is_selected_on=is_sel, idx2date=i2d)
    assert r["exit_reason"] == exit_sim.R_DANGER
    assert r["hold_days"] == 2
    assert abs(r["gross_pct"] - (101.0 / 100.0 - 1) * 100) < 1e-6

    # 关开关(is_selected_on=None):同一路径应走到时间止损,不因剔除离场
    r2 = exit_sim.simulate_long_exit(rec, 0, tp_pct=8, sl_pct=5, time_stop=5)
    assert r2["exit_reason"] == exit_sim.R_TIME


def test_danger_none_means_hold_not_deselect():
    """is_selected_on 返 None(非该策略预测日)≠ 剔除 → 继续持有,不触危险信号。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 101, 99, 100.5),
        ("d2", 100.5, 102, 99.5, 101.0),
        ("d3", 101, 103, 100, 102.0),
        ("d4", 102, 104, 101, 103.0),
        ("d5", 103, 105, 102, 104.0),
    ]})
    rec = book.get("X")
    r = exit_sim.simulate_long_exit(rec, 0, tp_pct=8, sl_pct=5, time_stop=5,
                                    is_selected_on=lambda d: None, idx2date=_idx2date(rec))
    assert r["exit_reason"] == exit_sim.R_TIME   # 全程 None→持有到时间止损


# ─────────────── ⑥ 往返成本双档 ───────────────
def test_cost_deducted_from_gross_both_tiers():
    """净收益 = 毛收益 − 往返成本;0.1% 与 0.2% 双档差 0.1%。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 110, 99, 101.0),    # 止盈 +8% 毛
        ("d2", 101, 112, 100, 111.0),
        ("d3", 111, 113, 110, 112.0),
        ("d4", 112, 114, 111, 113.0),
        ("d5", 113, 115, 112, 114.0),
    ]})
    rec = book.get("X")
    r1 = exit_sim.simulate_long_exit(rec, 0, 8, 5, 5, cost_pct=0.1)
    r2 = exit_sim.simulate_long_exit(rec, 0, 8, 5, 5, cost_pct=0.2)
    assert abs(r1["net_pct"] - (8.0 - 0.1)) < 1e-6
    assert abs(r2["net_pct"] - (8.0 - 0.2)) < 1e-6
    assert abs((r1["net_pct"] - r2["net_pct"]) - 0.1) < 1e-6


# ─────────────── ⑦ 防未来函数:窗口不足 → 未成熟 ───────────────
def test_not_matured_when_time_stop_out_of_range():
    """idx+time_stop 越界 → 未成熟(与固定持有 horizon=N 同一到期条件,不用未来价)。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 100, 101, 99, 100.5),
        ("d2", 100.5, 102, 99.5, 101.0),
    ]})   # 只有 idx 0..2;time_stop=5 → idx+5=5≥3 越界
    r = exit_sim.simulate_long_exit(book.get("X"), 0, 8, 5, 5)
    assert r["matured"] is False and r["net_pct"] is None


def test_entry_fallback_close_when_no_open():
    """open 缺失 → 入场用 close[T+1],entry_fallback=True。"""
    book = _book({"X": [
        ("d0", 100, 100, 100, 100.0),
        ("d1", 0, 101, 99, 100.0),      # open=0 → 用 close[T+1]=100 入场
        ("d2", 100, 108, 99, 101.0),
        ("d3", 101, 109, 100, 102.0),
        ("d4", 102, 110, 101, 103.0),
        ("d5", 103, 111, 102, 104.0),
    ]})
    r = exit_sim.simulate_long_exit(book.get("X"), 0, 8, 5, 5)
    assert r["entry_fallback"] is True and r["entry"] == 100.0
