"""锁死 factor_nextday harness 的关键语义(防未来 + 成交口径 + 涨停过滤 + 因子as-of)。

这些断言锁住"为什么这么写":一旦未来重写把防未来/口径/涨停过滤删掉,测试会红。
⚠️ 测试环境研究模拟,非投资建议。
"""
import numpy as np
import pandas as pd

from tools.research import factor_nextday as fn


def _mk_df(n=200, seed=0):
    rng = np.random.default_rng(seed)
    close = 10 * np.cumprod(1 + rng.normal(0, 0.02, n))
    openp = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.005, n)))
    vol = rng.uniform(1e6, 5e6, n)
    return pd.DataFrame(dict(
        date=pd.date_range("2020-01-01", periods=n, freq="B"),
        open=openp, high=high, low=low, close=close,
        volume=vol, amount=vol * close, turnover=rng.uniform(0.2, 2.0, n),
        pct_chg=np.r_[0, np.diff(close) / close[:-1] * 100]))


# ── 成交口径 ──────────────────────────────────────────────────────────
def test_marketable_fill_open_better_than_limit():
    """open≤P → 成交于 open(更优价),而非 P。"""
    P = np.array([10.0]); o = np.array([9.5]); h = np.array([10.5])
    lo = np.array([9.0]); c = np.array([10.2]); ct = np.array([10.0])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, 0.10, 0.0)
    assert filled[0]
    # 成交价应为 open=9.5(更优),收益=10.2/9.5-1
    assert abs(gross[0] - (10.2 / 9.5 - 1)) < 1e-9


def test_marketable_fill_touch_at_P():
    """open>P 但 low≤P → 成交于 P。"""
    P = np.array([9.8]); o = np.array([10.0]); h = np.array([10.3])
    lo = np.array([9.5]); c = np.array([10.1]); ct = np.array([9.9])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, 0.10, 0.0)
    assert filled[0]
    assert abs(gross[0] - (10.1 / 9.8 - 1)) < 1e-9


def test_marketable_untriggered_when_no_dip():
    """open>P 且 low>P → 未触发(买不进,不计交易)。"""
    P = np.array([9.5]); o = np.array([10.0]); h = np.array([10.4])
    lo = np.array([9.8]); c = np.array([10.2]); ct = np.array([9.6])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, 0.10, 0.0)
    assert not filled[0]
    assert np.isnan(gross[0])


def test_limit_up_gap_unbuyable():
    """D+1 gap 到涨停(open≥昨收×(1+涨停−0.5%)) → 剔除(买不进),即便 P 够低。"""
    ct = np.array([10.0]); limit = 0.10
    o = np.array([11.0])  # +10% 一线,≥10×1.095
    P = np.array([9.9]); h = np.array([11.0]); lo = np.array([10.8]); c = np.array([11.0])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, limit, 0.0)
    assert not filled[0]


def test_one_word_board_unbuyable():
    """一字板(high==low 且 open>昨收) → 剔除。"""
    ct = np.array([10.0])
    o = np.array([11.0]); h = np.array([11.0]); lo = np.array([11.0]); c = np.array([11.0])
    P = np.array([9.9])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, 0.10, 0.0)
    assert not filled[0]


def test_cost_reduces_net_below_gross():
    """net = (1+gross)(1-cost)-1 < gross(成本必扣)。"""
    P = np.array([10.0]); o = np.array([9.8]); h = np.array([10.5])
    lo = np.array([9.5]); c = np.array([10.3]); ct = np.array([10.0])
    net, gross, filled = fn.fill_and_return(P, o, h, lo, c, ct, 0.10, 10 / 1e4)
    assert filled[0]
    assert net[0] < gross[0]


# ── 防未来(硬红线) ────────────────────────────────────────────────────
def test_factors_are_asof_no_lookahead():
    """篡改未来 K 线不改变 ≤t 的因子值(因子只用滚动窗口 ≤t)。"""
    df = _mk_df(200, seed=1)
    f1 = fn.precompute(df)
    t = 150
    base = {k: f1["factors"][k][t] for k in fn.FACTORS}
    # 篡改 t 之后的所有 K 线
    df2 = df.copy()
    df2.loc[df2.index > t, ["open", "high", "low", "close"]] *= 3.0
    f2 = fn.precompute(df2)
    for k in fn.FACTORS:
        a, b = base[k], f2["factors"][k][t]
        assert (np.isnan(a) and np.isnan(b)) or abs(a - b) < 1e-9, f"因子 {k} 前视泄漏"


def test_overnight_intraday_decomposition():
    """隔夜=open/prev_close-1、日内=close/open-1,两段乘积=昨收→今收。"""
    df = _mk_df(50, seed=2)
    o, c = df["open"].to_numpy(), df["close"].to_numpy()
    prev = np.r_[np.nan, c[:-1]]
    overnight = o / prev - 1
    intraday = c / o - 1
    cc = c / prev - 1
    ok = ~np.isnan(prev)
    assert np.allclose((1 + overnight[ok]) * (1 + intraday[ok]) - 1, cc[ok], atol=1e-9)


# ── regime / 分层 ─────────────────────────────────────────────────────
def test_regime_labels_three_buckets():
    """regime as-of 三分位应产出 0/1/2 三态。"""
    mkt_cc = {f"2020-{m:02d}-{d:02d}": (0.01 * ((m * d) % 7 - 3))
              for m in range(1, 13) for d in range(1, 25)}
    reg = fn.regime_asof_series(mkt_cc)
    assert set(reg.values()) <= {0, 1, 2}
    assert len(set(reg.values())) == 3


def test_board_limit_routing():
    """创业板/科创板 20%,主板 10%。"""
    assert fn.board_limit("300001") == 0.20
    assert fn.board_limit("688001") == 0.20
    assert fn.board_limit("600001") == 0.10
    assert fn.board_limit("000001") == 0.10
