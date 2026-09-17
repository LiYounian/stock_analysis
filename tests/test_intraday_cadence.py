"""锁死午盘/日内复盘「隔夜口径」语义（守则6：断言锁"为什么改"，防未来重写无意破坏）。

覆盖：①买卖点时序（买=D收盘·卖=D+1早盘·D<D+1）②防未来 as-of（>卖点的 bar 不进计算）
③绝对收益公式 ④价源分级（noon→t1145→open降级→None，绝不用 D+1 收盘顶替）⑤基准同口径隔夜
⑥未到期 pending。全用桩数据，不触生产 data / 不触网。⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.review import intraday_cadence as ic
from tools.review.types import Pick

D = "2026-09-16"
D1 = "2026-09-17"
FUTURE = "2026-09-18"


def _kline_df():
    """桩日线：含 D / D+1 / 未来行。D收盘=10.0；D+1开=10.5、收=12.0（收≠开，用于证卖不取收）。"""
    return pd.DataFrame([
        {"date": D, "open": 9.8, "low": 9.5, "close": 10.0},
        {"date": D1, "open": 10.5, "low": 10.2, "close": 12.0},
        {"date": FUTURE, "open": 99.0, "low": 98.0, "close": 100.0},   # 未来行：绝不该被取到
    ])


# ── 绝对收益公式 ────────────────────────────────────────────────
def test_overnight_return_formula():
    assert ic.overnight_return(10.0, 11.0) == pytest.approx(10.0)     # +10%
    assert ic.overnight_return(10.0, 9.0) == pytest.approx(-10.0)     # −10%（绝对口径含负）
    assert ic.overnight_return(None, 11.0) is None
    assert ic.overnight_return(10.0, None) is None
    assert ic.overnight_return(0.0, 11.0) is None                     # 买价≤0 → None


# ── 买卖点时序 + 防未来 as-of ──────────────────────────────────
def test_buy_is_d_close_only():
    cache = {"600000": _kline_df()}
    assert ic.buy_price("600000", D, cache) == 10.0                   # 买=D收盘


def test_future_bar_never_leaks():
    """防未来：买只取 D 当日行、卖(open降级)只取 D+1 行；未来行(FUTURE)存在也不改结果。"""
    kcache = {"600000": _kline_df()}
    scache = {("noon", D1): {}, ("t1145", D1): {}}                    # 无快照 → 走 open 降级
    assert ic.buy_price("600000", D, kcache) == 10.0
    price, src = ic.sell_price("600000", D1, None, scache, kcache)
    assert (price, src) == (10.5, ic.SRC_OPEN_DEG)                    # =D+1 open，非 FUTURE 的 99


# ── 价源分级（绝不用 D+1 收盘顶替）────────────────────────────
def test_sell_price_priority_noon_first():
    scache = {("noon", D1): {"600000": {"price": 11.3, "pct_chg": 13.0}},
              ("t1145", D1): {"600000": {"price": 99.9}}}
    price, src = ic.sell_price("600000", D1, None, scache, {})
    assert (price, src) == (11.3, ic.SRC_NOON)                        # noon 优先于 t1145


def test_sell_price_t1145_second():
    scache = {("noon", D1): {}, ("t1145", D1): {"600000": {"price": 11.7}}}
    price, src = ic.sell_price("600000", D1, None, scache, {})
    assert (price, src) == (11.7, ic.SRC_T1145)


def test_sell_price_open_degraded_third():
    scache = {("noon", D1): {}, ("t1145", D1): {}}
    price, src = ic.sell_price("600000", D1, None, scache, {"600000": _kline_df()})
    assert (price, src) == (10.5, ic.SRC_OPEN_DEG)                    # =D+1 open


def test_sell_price_never_uses_d1_close():
    """核心红线：无任何快照且 D+1 open 缺(NaN) → None，绝不回退用 D+1 收盘(12.0)。"""
    df = _kline_df()
    df.loc[df["date"] == D1, "open"] = float("nan")                  # D+1 开盘缺
    scache = {("noon", D1): {}, ("t1145", D1): {}}
    price, src = ic.sell_price("600000", D1, None, scache, {"600000": df})
    assert price is None and src is None                              # 不是 12.0（D+1 收盘）


# ── 基准同口径隔夜 ────────────────────────────────────────────
def test_market_overnight_ew_from_pct_chg():
    """全A等权隔夜 = D+1 noon 快照 pct_chg 等权均值（每只 pct_chg=gtimg D收→D+1 11:30 隔夜收益）。"""
    scache = {("noon", D1): {"a": {"price": 1, "pct_chg": 2.0},
                             "b": {"price": 1, "pct_chg": -1.0},
                             "c": {"price": 1, "pct_chg": 3.0}}}
    assert ic.market_overnight_ew(D1, None, scache) == pytest.approx((2.0 - 1.0 + 3.0) / 3)


def test_market_overnight_ew_missing_snapshot_none():
    assert ic.market_overnight_ew(D1, None, {("noon", D1): {}}) is None   # 快照缺 → None(α null)


# ── compute_labels 集成（时序 + 收益 + α + 状态）──────────────
def _pick():
    return Pick(date=D, code="600000", name="桩票")


def test_compute_labels_settled(monkeypatch):
    monkeypatch.setattr(ic, "next_trading_day", lambda d: D1)
    kcache = {"600000": _kline_df()}
    scache = {("noon", D1): {"600000": {"price": 11.0, "pct_chg": 10.0},
                             "x": {"price": 1, "pct_chg": 1.0}}}      # 基准=(10+1)/2=5.5
    lab = ic.compute_labels(_pick(), kcache, scache, None, {})
    assert lab.buy_price == 10.0 and lab.sell_price == 11.0
    assert lab.sell_src == ic.SRC_NOON
    assert lab.r_overnight == pytest.approx(10.0)                     # (11-10)/10
    assert lab.market_overnight == pytest.approx(5.5)
    assert lab.alpha_overnight == pytest.approx(4.5)                  # 10 − 5.5
    assert lab.win is True and lab.status == "settled" and lab.degraded is False


def test_compute_labels_pending_when_next_day_snapshot_absent(monkeypatch):
    """今天选的票、次日快照未出 → r_overnight=None、status=pending（可回填幂等）。"""
    monkeypatch.setattr(ic, "next_trading_day", lambda d: D1)
    df = _kline_df()
    df.loc[df["date"] == D1, "open"] = float("nan")                  # 次日行还没数据
    kcache = {"600000": df}
    scache = {("noon", D1): {}, ("t1145", D1): {}}
    lab = ic.compute_labels(_pick(), kcache, scache, None, {})
    assert lab.buy_price == 10.0                                      # 买价有（D收盘已出）
    assert lab.r_overnight is None and lab.win is None
    assert lab.status == "pending"


def test_compute_labels_degraded_status(monkeypatch):
    monkeypatch.setattr(ic, "next_trading_day", lambda d: D1)
    kcache = {"600000": _kline_df()}
    scache = {("noon", D1): {}, ("t1145", D1): {}}                    # 无快照 → open 降级
    lab = ic.compute_labels(_pick(), kcache, scache, None, {})
    assert lab.sell_src == ic.SRC_OPEN_DEG and lab.degraded is True
    assert lab.status == "degraded"
    assert lab.r_overnight == pytest.approx((10.5 / 10.0 - 1) * 100)  # +5%


def test_no_data_when_buy_missing(monkeypatch):
    monkeypatch.setattr(ic, "next_trading_day", lambda d: D1)
    empty = pd.DataFrame(columns=["date", "open", "low", "close"])
    lab = ic.compute_labels(_pick(), {"600000": empty},
                            {("noon", D1): {}, ("t1145", D1): {}}, None, {})
    assert lab.buy_price is None and lab.status == "no_data"


# ── 归因 shim 复用（隔夜收益喂尾盘 classify）──────────────────
def test_attr_shim_maps_r_overnight():
    lab = ic.OvernightLabels(r_overnight=3.3)
    shim = ic.to_attr_shim(lab)
    assert shim.untriggered is False and shim.runaway_up is False
    assert shim.r_exit == 3.3                                         # classify 据此判选对/选错桶
