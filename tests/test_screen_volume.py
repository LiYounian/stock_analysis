"""策略 S04「量价放量」单测:3 子信号各自命中语义 + 换手 NA 跳过 + 历史门槛。

纯合成 K 线,不触网。锁住:换手率缺失(NA)时"单日放量"对该票不适用(不误选);
低位放量的 30 周线上穿判定(无未来函数);连续放量的连涨+量增门槛。
"""
import numpy as np
import pandas as pd

from tools.pipeline import screen_volume as sv


def _frame(closes, volumes=None, turnover=None, lows=None):
    n = len(closes)
    dates = pd.bdate_range(end="2026-08-14", periods=n)
    closes = np.asarray(closes, dtype=float)
    vol = np.full(n, 1e6) if volumes is None else np.asarray(volumes, dtype=float)
    turn = np.full(n, np.nan) if turnover is None else np.asarray(turnover, dtype=float)
    lows = closes - 0.5 if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({
        "date": dates, "open": closes, "high": closes + 0.5, "low": lows, "close": closes,
        "volume": vol, "amount": closes * vol, "turnover": turn, "pct_chg": np.zeros(n),
    })


def _rising(n=230, base=10.0, slope=0.05):
    return [base + i * slope for i in range(n)]


def test_single_volume_fires_with_turnover():
    """上升趋势(MA200上行、MA50>MA200),末日换手放大+涨>3% → 单日放量命中。"""
    c = _rising()
    c[-1] = c[-2] * 1.04                       # 末日涨 4% > 3%
    turn = np.full(len(c), 1.0)
    turn[-1] = 3.0                             # 换手放大 3× > 1.7×
    r = sv.screen_latest(_frame(c, turnover=turn))
    assert "单日放量" in r["组合"], r["明细"]["单日放量"]


def test_single_volume_skipped_when_turnover_na():
    """同样的价格形态,但换手率 NA → 单日放量对该票不适用,不得进入组合。"""
    c = _rising()
    c[-1] = c[-2] * 1.04
    r = sv.screen_latest(_frame(c, turnover=None))   # turnover 全 NA
    assert "单日放量" not in r["组合"]


def test_continuous_volume_fires():
    """末 3 日连续放量走高、各涨 > 4%、量递增、站上均线 → 连续放量命中。"""
    c = _rising()
    c[-3] = c[-4] * 1.05
    c[-2] = c[-3] * 1.05
    c[-1] = c[-2] * 1.05
    vol = np.full(len(c), 1e6)
    vol[-1] = 3e6                              # 量递增(> 前一日)
    r = sv.screen_latest(_frame(c, volumes=vol))
    assert "连续放量" in r["组合"], r["明细"]["连续放量"]


def test_low_volume_upcross_30week():
    """长期贴 30 周线下方(prev ≤ 周线),末日放量跳上 30 周线 → 低位放量命中(上穿)。"""
    n = 250
    c = [99.9] * (n - 1) + [103.0]            # 末日上穿
    vol = np.full(n, 1e6)
    vol[-1] = 5e6                             # 近10日最大量
    r = sv.screen_latest(_frame(c, volumes=vol))
    assert "低位放量" in r["组合"], r["明细"]["低位放量"]


def test_low_volume_no_cross_when_already_above():
    """已在 30 周线上方(prev 也 > 周线)→ 非上穿 → 低位放量不命中。"""
    c = _rising(n=250, base=10.0, slope=0.1)  # 持续在周线上方,无上穿
    r = sv.screen_latest(_frame(c))
    assert "低位放量" not in r["组合"]


def test_history_short_not_selected():
    c = _rising(n=150)                         # < 201 → 历史不足
    assert sv.screen_latest(_frame(c))["SELECT"] is False


def test_single_volume_degrade_marked_loud():
    """换手 NA:单日放量不进组合(不误选),但明细**明确标降级**(不静默"不适用")。
    锁死 #20:94/94 换手=None 时必须可见,不能哑火无痕。"""
    c = _rising()
    c[-1] = c[-2] * 1.04
    r = sv.screen_latest(_frame(c, turnover=None))
    assert "单日放量" not in r["组合"]
    assert r["明细"]["单日放量"].get("降级")           # 明细带降级标记


def test_run_screen_counts_single_degrade(monkeypatch):
    """run_volume_screen 汇总「单日放量降级数(换手缺失)」,让整批哑火可见。"""
    c = _rising()
    c[-1] = c[-2] * 1.04
    kdf = _frame(c, turnover=None)                      # 全 NA → 单日放量降级
    monkeypatch.setattr(sv, "_load_or_fetch_kline", lambda code, fetch: kdf)
    monkeypatch.setattr(sv.store, "put_view", lambda name, view: "stub")  # 不落盘
    view = sv.run_volume_screen(["AAA", "BBB"], fetch=False)
    assert view["单日放量降级数(换手缺失)"] == 2
