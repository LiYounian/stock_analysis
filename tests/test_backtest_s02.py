"""策略 S02 持仓回测适配单测。

锁语义:
  · find_signals_s02 用 S02 的 signal_at 全历史扫信号(不误用 S01 路径);
  · 回测复用 S01 持仓回测器(simulate_position),离场规则来自 THRESHOLDS['趋势深跌反包'];
  · summarize 单进程串行跑通、结构完整、口径明示「机械买入基线」;
  · 防未来函数:追加未来行不改变已发生信号的判定。
"""
import pandas as pd
import pytest

from tools.backtest import backtest_s02 as bt
from tools.collectors import market


# ———————————————————— 构造器(与 S02 screener 测试同口径)————————————————————
def _vols():
    out = []
    for wk in range(9):
        v = 5000.0 if wk == 7 else (500.0 if wk == 8 else 1000.0)
        out += [v] * 5
    return out


def _closes(n=45, slope=0.1):
    cl = [round(100.0 + slope * i, 4) for i in range(n)]
    cl[-1] = round(cl[-2] - 0.2, 4)
    return cl


def _df(vols, closes, opens=None, start="2024-01-01"):
    n = len(vols)
    if opens is None:
        opens = list(closes)
        opens[-1] = round(closes[-1] + 0.3, 4)
    dates = pd.bdate_range(start, periods=n)
    highs = [round(max(o, c) + 0.1, 4) for o, c in zip(opens, closes)]
    lows = [round(min(o, c) - 0.1, 4) for o, c in zip(opens, closes)]
    return pd.DataFrame({"date": dates, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vols})


def _signal_df():
    """t=44 出 S02 信号的基准场景。"""
    return _df(_vols(), _closes())


def _signal_then_crash_df():
    """t=44 出信号,随后暴跌 → 触发 S01 硬止损离场(制造一笔已离场交易)。"""
    base = _signal_df()
    tail_close = [100.0, 98.0, 96.0, 95.0, 95.0]              # 跌破 P0×0.97
    tail = pd.DataFrame({
        "date": pd.bdate_range("2024-03-05", periods=len(tail_close)),
        "open": tail_close, "high": [c + 0.1 for c in tail_close],
        "low": [c - 0.1 for c in tail_close], "close": tail_close,
        "volume": [800.0] * len(tail_close),
    })
    return pd.concat([base, tail], ignore_index=True)


# ———————————————————— find_signals_s02 ————————————————————
def test_find_signals_locates_s02_signal():
    sigs = bt.find_signals_s02(_signal_df())
    assert 44 in sigs                                        # 基准场景在 t=44 命中


def test_find_signals_lookahead_invariant():
    """追加未来行(暴跌尾)不改变 t=44 之前已发生信号的集合。"""
    base_sigs = [t for t in bt.find_signals_s02(_signal_then_crash_df()) if t <= 44]
    assert base_sigs == bt.find_signals_s02(_signal_df())


# ———————————————————— 复用 S01 离场:产生已离场交易 ————————————————————
def test_backtest_one_reuses_s01_exit():
    trades = bt.backtest_one_s02(_signal_then_crash_df(), code="600000")
    assert len(trades) >= 1
    closed = [t for t in trades if t["状态"] == "已离场"]
    assert closed, trades
    tr = closed[0]
    assert tr["进场价P0"] == pytest.approx(_signal_df()["close"].iloc[-1])
    assert tr["离场规则"] in {"硬止损", "趋势止损", "加速止盈", "放量滞涨", "时间成本"}


# ———————————————————— summarize 串行跑通 + 口径 ————————————————————
def test_summarize_serial_wiring(monkeypatch):
    monkeypatch.setattr(market, "load_kline", lambda c: _signal_then_crash_df())
    r = bt.summarize(codes=["600000"], fetch=False)
    assert r["策略"].startswith("放量后缩量回踩")
    assert r["出信号票数"] == 1 and r["有效样本票"] == 1
    assert r["汇总"]["交易数"] >= 1
    assert "机械买入基线" in r["口径"]                        # 口径明示,避免误当最终买法


def test_summarize_skips_insufficient(monkeypatch):
    monkeypatch.setattr(market, "load_kline", lambda c: _signal_df().head(20))
    r = bt.summarize(codes=["000001"], fetch=False)
    assert r["有效样本票"] == 0 and r["跳过票数(历史不足/无K线)"] == 1
