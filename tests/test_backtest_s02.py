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


# ════════════════════════════════════════════════════════════════════
# 趋势过滤 A/B:RS 横截面面板 + Welch t + A/B 分组(窗内 PASS→B / FAIL→对照)
# ════════════════════════════════════════════════════════════════════
def _trend_df(n=300, base=10.0, slope=0.1, start="2023-01-02"):
    """线性趋势 df(≥252 根);slope>0 上升、<0 下降。用于 RS 面板与趋势门联测。"""
    closes = [round(base + slope * i, 4) for i in range(n)]
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "date": dates,
        "open": [round(c - 0.02, 4) for c in closes],
        "high": [round(c + 0.05, 4) for c in closes],
        "low": [round(c - 0.05, 4) for c in closes],
        "close": closes, "volume": [1000.0] * n,
    })


def _flat_bench(n=300, start="2023-01-02"):
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": dates, "close": [4000.0] * n})


def test_build_rs_panel_rank_direction():
    """上升票的 RS(相对平基准)高于下降票 → 当日横截面百分位更高。"""
    up, dn = _trend_df(slope=0.1), _trend_df(slope=-0.03, base=100.0)
    rank, code_rs = bt.build_rs_panel({"UP": up, "DN": dn}, _flat_bench(), window=126)
    d = pd.to_datetime(up["date"].iloc[-1]).strftime("%Y-%m-%d")
    assert rank[d]["UP"] > rank[d]["DN"]
    assert rank[d]["UP"] == pytest.approx(100.0)             # 两票中最高 → pct rank 100
    assert code_rs["UP"][d] > code_rs["DN"][d]               # RS 原值方向一致


def test_build_rs_panel_no_lookahead():
    """某日 RS 只用 ≤ 当日的 window 日涨跌幅:截断未来行后同日 RS 不变。"""
    up = _trend_df(slope=0.1)
    dn = _trend_df(slope=-0.03, base=100.0)
    bench = _flat_bench()
    t = 200
    d = pd.to_datetime(up["date"].iloc[t]).strftime("%Y-%m-%d")
    full = bt.build_rs_panel({"UP": up, "DN": dn}, bench, window=126)[1]["UP"][d]
    trunc = bt.build_rs_panel(
        {"UP": up.iloc[: t + 1].reset_index(drop=True),
         "DN": dn.iloc[: t + 1].reset_index(drop=True)},
        bench.iloc[: t + 1].reset_index(drop=True), window=126)[1]["UP"][d]
    assert full == pytest.approx(trunc)


def test_welch_t_small_sample_guarded():
    assert bt._welch_t([0.1], [0.2, 0.3])["t"] is None       # 任一组<2 → 不算
    w = bt._welch_t([0.05, 0.06, 0.07], [-0.02, -0.03, -0.01])
    assert w["t"] is not None and w["均值差(a−b)"] > 0        # a 组明显更高 → 正 t


def test_summarize_ab_partitions_pass_vs_fail(monkeypatch):
    """A/B 分组:窗内趋势门 PASS 的票入 B、FAIL 的入对照组;离场复用 S01(未改)。

    只测**新增的分组/RS 查表/窗口门**逻辑(S02 信号检测已在别处覆盖),故 monkeypatch
    find_signals_s02 强制在窗内 t=270 出信号,趋势门对真实 df 实算。
    """
    up = _trend_df(slope=0.1)                                # 上升 + 高 RS → 趋势门 PASS
    dn = _trend_df(slope=-0.03, base=100.0)                  # 下降 → 趋势门 FAIL
    kl = {"UP": up, "DN": dn}
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    monkeypatch.setattr(bt.pb, "_load_bench", lambda fetch: _flat_bench())
    monkeypatch.setattr(bt, "find_signals_s02", lambda kdf, cfg=None: [270])

    r = bt.summarize_ab(codes=["UP", "DN"], fetch=False)
    w = r["窗内信号统计"]
    assert w["窗内信号数"] == 2
    assert w["趋势门PASS"] == 1 and w["趋势门FAIL"] == 1
    assert r["B_S02+趋势过滤(窗内PASS)"]["交易数"] == 1
    assert r["对照_窗内FAIL"]["交易数"] == 1
    assert r["A_S02窗内(原版·对照)"]["交易数"] == 2           # A = 窗内全部
    assert "机械" not in r["口径"] or "趋势门" in r["口径"] or "S01" in r["口径"]


def test_summarize_ab_out_of_window_signal_excluded(monkeypatch):
    """信号落在趋势门不可计算窗(t+1<252)→ 不进 A_窗内/B,只进 A_full。"""
    up = _trend_df(slope=0.1)
    monkeypatch.setattr(market, "load_kline", lambda c: up)
    monkeypatch.setattr(bt.pb, "_load_bench", lambda fetch: _flat_bench())
    monkeypatch.setattr(bt, "find_signals_s02", lambda kdf, cfg=None: [100])  # 100+1<252
    r = bt.summarize_ab(codes=["UP"], fetch=False)
    assert r["窗内信号统计"]["窗内信号数"] == 0
    assert r["A_full_S02全历史"]["交易数"] == 1                # 全历史仍计入
