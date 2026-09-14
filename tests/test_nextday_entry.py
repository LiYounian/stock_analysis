"""锁定次日入场回测底座的核心语义(防未来 / 成交判定 / 绝对收益·α 口径)。

这些断言锁住"为什么这么做":未来 prompt / 代码重写若无意破坏,测试应红。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import nextday_entry as ne


def _mk_df(rows):
    """rows: list of (date, o, h, l, c)。turnover 填 1.0。"""
    df = pd.DataFrame(
        [dict(date=d, open=o, high=h, low=l, close=c, volume=1.0,
              amount=1.0, turnover=1.0, pct_chg=0.0) for d, o, h, l, c in rows]
    )
    df["date"] = pd.to_datetime(df["date"])   # load_kline 保证 date 为 datetime
    return df


# ────────────────────────────── 成交判定 ──────────────────────────────
def test_marketable_fill_price_improvement():
    """挂价 P≥开盘 → 立即以 open 成交(价更优),不按 P。"""
    P = np.array([10.5])           # 挂价高于开盘
    o = np.array([10.0]); h = np.array([11.0]); l = np.array([9.8]); c = np.array([10.8])
    fill, ret, filled = ne.fill_and_return(P, o, h, l, c, "marketable")
    assert filled[0]
    assert fill[0] == 10.0                       # 成交在 open,不是 10.5
    assert abs(ret[0] - (10.8 / 10.0 - 1)) < 1e-12


def test_marketable_fill_touch_below_open():
    """挂价 P<开盘,盘中低点 low≤P → 以 P 成交。"""
    P = np.array([9.9])
    o = np.array([10.0]); h = np.array([10.5]); l = np.array([9.8]); c = np.array([10.2])
    fill, ret, filled = ne.fill_and_return(P, o, h, l, c, "marketable")
    assert filled[0] and fill[0] == 9.9
    assert abs(ret[0] - (10.2 / 9.9 - 1)) < 1e-12


def test_marketable_no_fill_when_low_above_P():
    """挂价 P<开盘 但当日 low 未跌到 P → 未触发。"""
    P = np.array([9.5])
    o = np.array([10.0]); h = np.array([10.5]); l = np.array([9.8]); c = np.array([10.2])
    fill, ret, filled = ne.fill_and_return(P, o, h, l, c, "marketable")
    assert not filled[0] and np.isnan(ret[0])


def test_doc_vs_marketable_divergence_above_open():
    """P 介于 open 与 high 之间(P≥open):doc 按 P 成交(偏贵),marketable 按 open(更优)。

    锁住报告 §6 结论:doc-literal 在 P≥open 时系统性偏悲观。
    """
    P = np.array([10.3])
    o = np.array([10.0]); h = np.array([10.5]); l = np.array([9.9]); c = np.array([10.4])
    f_m, r_m, _ = ne.fill_and_return(P, o, h, l, c, "marketable")
    f_d, r_d, _ = ne.fill_and_return(P, o, h, l, c, "doc")
    assert f_m[0] == 10.0 and f_d[0] == 10.3   # marketable 更优
    assert r_m[0] > r_d[0]


def test_doc_no_fill_when_P_above_high():
    """doc:P>high → 未触发(当日没到过这个价上沿之上)。"""
    P = np.array([11.0])
    o = np.array([10.0]); h = np.array([10.5]); l = np.array([9.9]); c = np.array([10.4])
    _, _, filled = ne.fill_and_return(P, o, h, l, c, "doc")
    assert not filled[0]


# ────────────────────────────── 防未来(硬红线) ──────────────────────────────
def test_entry_price_only_uses_close_t_and_open_next():
    """入场档挂价只依赖 close[t] 与 open[t+1];改动 t+2 及以后不影响信号 t 的挂价。"""
    base = _mk_df([("2020-01-01", 10, 10.2, 9.8, 10.0),
                   ("2020-01-02", 10.1, 10.5, 9.9, 10.3),
                   ("2020-01-03", 10.3, 10.6, 10.0, 10.4),
                   ("2020-01-06", 10.4, 10.9, 10.2, 10.7)])
    feat = ne.precompute(base)
    t = np.array([0])
    P0 = ne.entry_prices(feat, t)

    mutated = base.copy()
    mutated.loc[2:, ["open", "high", "low", "close"]] *= 1.5   # 篡改 t+2 起的未来
    P1 = ne.entry_prices(ne.precompute(mutated), t)
    for k in P0:
        assert np.allclose(P0[k], P1[k]), f"{k} 泄露了未来"


def test_beta_is_asof_no_future_leak():
    """β[i] 只用 ≤i 的 close-to-close;改动 i+1 起的价格不改变 β[i]。"""
    rng = np.random.default_rng(0)
    n = 120
    cc_stock = rng.normal(0, 0.02, n)
    cc_mkt = rng.normal(0, 0.015, n)
    b0 = ne.rolling_beta(cc_stock.copy(), cc_mkt.copy())
    i = 80
    cc_stock2 = cc_stock.copy(); cc_mkt2 = cc_mkt.copy()
    cc_stock2[i + 1:] += 5.0; cc_mkt2[i + 1:] -= 5.0             # 篡改未来
    b1 = ne.rolling_beta(cc_stock2, cc_mkt2)
    assert np.isfinite(b0[i]) and abs(b0[i] - b1[i]) < 1e-12


# ────────────────────────────── 收益 / α 口径 ──────────────────────────────
def test_absolute_return_definition():
    """绝对收益 = close[D+1]/成交价 − 1(open 档成交价=open[D+1])。"""
    P = np.array([10.0]); o = np.array([10.0]); h = np.array([10.6])
    l = np.array([9.9]); c = np.array([10.5])
    _, ret, _ = ne.fill_and_return(P, o, h, l, c, "marketable")
    assert abs(ret[0] - (10.5 / 10.0 - 1.0)) < 1e-12


def test_alpha_is_return_minus_market_same_window():
    """α = 绝对收益 − mkt_ir[D+1];经 accumulate→_agg_cells 端到端验证。"""
    res = ne.Result()
    strat = np.array([0, 0])                # 同一 stratum
    filled = np.array([True, True])
    ret = np.array([0.03, -0.01])           # 两单绝对收益
    mkt = np.array([0.01, 0.01])            # 市场同期
    beta = np.array([1.0, 1.0])
    ne.accumulate(res, "open", "marketable", strat, filled, ret, mkt, beta, cost=0.0)
    agg = ne._agg_cells(res.cell("open", "marketable"), ne._mask())
    assert abs(agg["mean_gross"] - 0.01) < 1e-12          # (0.03-0.01)/2
    assert abs(agg["mean_alpha"] - (0.01 - 0.01)) < 1e-12  # meanret - meanmkt = 0.01-0.01
    assert agg["win_gross"] == 0.5                         # 一胜一负


def test_beta_drag_flags_market_driven_loss():
    """β 拖累:亏损单 且 m<0 且 β·m<0 且 |β·m|≥0.5|ret| → 记 β 拖累致亏。"""
    res = ne.Result()
    strat = np.array([0, 0])
    filled = np.array([True, True])
    # 单1:亏 -2%,市场 -3%,β=1 → βm=-3% ≥ 0.5*2% → β拖累; 单2:亏 -2%,市场 +1% → 非β拖累
    ret = np.array([-0.02, -0.02])
    mkt = np.array([-0.03, 0.01])
    beta = np.array([1.0, 1.0])
    ne.accumulate(res, "open", "marketable", strat, filled, ret, mkt, beta, cost=0.0)
    agg = ne._agg_cells(res.cell("open", "marketable"), ne._mask())
    assert agg["n_loss"] == 2
    assert abs(agg["beta_drag_share"] - 0.5) < 1e-12       # 2 单亏,1 单归因 β


def test_net_of_cost_reduces_return():
    """扣往返成本后净收益 = (1+毛)(1−cost)−1 < 毛。"""
    res = ne.Result()
    strat = np.array([0]); filled = np.array([True])
    ret = np.array([0.005]); mkt = np.array([0.0]); beta = np.array([1.0])
    ne.accumulate(res, "open", "marketable", strat, filled, ret, mkt, beta, cost=0.001)
    agg = ne._agg_cells(res.cell("open", "marketable"), ne._mask())
    assert agg["mean_net"] < agg["mean_gross"]
    assert abs(agg["mean_net"] - ((1.005 * 0.999) - 1.0)) < 1e-12


# ────────────────────────────── 分桶 ──────────────────────────────
def test_bbucket_boundaries():
    beta = np.array([0.5, 0.8, 1.0, 1.2, 1.5, np.nan])
    b = ne.bbucket_of(beta)
    assert list(b) == [0, 1, 1, 1, 2, -1]   # <0.8 低;[0.8,1.2] 中;>1.2 高;NaN 剔除


def test_stratum_code_roundtrip():
    for p in range(2):
        for r in range(3):
            for bb in range(3):
                code = ne.stratum_code(np.array([p]), np.array([r]), np.array([bb]))[0]
                assert code // 9 == p and (code % 9) // 3 == r and code % 3 == bb
