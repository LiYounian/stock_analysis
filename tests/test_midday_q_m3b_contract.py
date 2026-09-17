"""午盘 Q · M3.b 分时回测契约测试。

锁语义(约法第6条):
    · 防未来 —— `_cum_to` 绝不读 as_of 之后的 bar
    · 成交可行性 —— 一字涨停/跌停的识别与"强制顺延持有 + 累计双边成本"
    · 组合口径回撤 —— 修 M3.a 逐笔累加的错口径(这是本模块存在的主要理由之一)
    · Q3 不在 STRATEGIES 里(分时资金流无历史,不能假装能回测)
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.backtest import backtest_midday_q_m3b as M
from tools.backtest import fetch_midday_q_bars as F


# ────────────────────────────── 采集侧 ──────────────────────────────

def test_bs_code_mapping():
    assert F._bs_code("600519") == "sh.600519"
    assert F._bs_code("900001") == "sh.900001"
    assert F._bs_code("000021") == "sz.000021"
    assert F._bs_code("300308") == "sz.300308"
    assert F._bs_code("688008") == "sh.688008"


def test_keep_times_covers_decision_points():
    """1430/1450 是午盘Q的判定时点,必须在采集时刻里;0935 作当日 open 锚。"""
    for t in ("0935", "1430", "1450"):
        assert t in F.KEEP_TIMES, f"{t} 不在 KEEP_TIMES,回测拿不到判定价"


# ────────────────────────────── 防未来(核心红线) ──────────────────────────────

def _bars(rows):
    return pd.DataFrame(rows, columns=["date", "time", "open", "high",
                                        "low", "close", "volume", "amount"])


def test_cum_to_never_reads_future():
    """_cum_to(as_of) 只能累计 time<=as_of 的 bar —— 篡改之后的 bar 不改变结果。"""
    base = [
        ["2026-09-10", "0935", 10, 11, 9.5, 10.5, 100, 1000],
        ["2026-09-10", "1430", 10.5, 12, 10.4, 11.8, 200, 2000],
        ["2026-09-10", "1450", 11.8, 13, 11.7, 12.9, 300, 3000],
        ["2026-09-10", "1500", 12.9, 14, 12.8, 13.9, 400, 4000],
    ]
    real = M._cum_to(_bars(base), "2026-09-10", "1430")
    # 把 1450/1500 篡改成极端值(未来数据污染)
    tainted = [r[:] for r in base]
    tainted[2] = ["2026-09-10", "1450", 999, 9999, 1, 999, 99999, 999999]
    tainted[3] = ["2026-09-10", "1500", 999, 9999, 1, 999, 99999, 999999]
    assert M._cum_to(_bars(tainted), "2026-09-10", "1430") == real, "as_of 之后的 bar 泄漏进信号"


def test_cum_to_includes_asof_bar_itself():
    """as_of 那一根本身要算进去(<=,不是 <)。"""
    b = _bars([
        ["2026-09-10", "0935", 10, 11, 9, 10, 100, 1000],
        ["2026-09-10", "1430", 10, 12, 10, 11, 200, 2000],
    ])
    vol, amt, hi, lo = M._cum_to(b, "2026-09-10", "1430")
    assert vol == 300 and amt == 3000
    assert hi == 12 and lo == 9


def test_daily_ctx_excludes_today():
    """日线上下文只能用 T-1 及以前 —— 当日行绝不能进 MA/昨收。"""
    rows = [{"date": f"2026-08-{d:02d}", "close": 10.0, "volume": 100}
            for d in range(1, 26)]
    rows.append({"date": "2026-08-26", "close": 999.0, "volume": 99999})   # "当日"
    df = pd.DataFrame(rows)
    ctx = M._daily_ctx(df, "2026-08-26")
    assert ctx is not None
    assert ctx["prev_close"] == 10.0, "昨收读到了当日行"
    assert ctx["ma5"] == 10.0, "MA5 被当日行污染"


def test_daily_ctx_insufficient_history_returns_none():
    df = pd.DataFrame([{"date": f"2026-08-{d:02d}", "close": 10.0, "volume": 100}
                        for d in range(1, 10)])
    assert M._daily_ctx(df, "2026-08-20") is None


# ────────────────────────────── 成交可行性 ──────────────────────────────

def _daily_with_prev_close(pc: float, date: str = "2026-09-10"):
    return pd.DataFrame([{"date": "2026-09-09", "close": pc, "volume": 100}])


def test_detects_one_word_limit_up():
    """四价合一 + 涨幅达涨停线 → 一字板(实盘买不进卖不掉)。"""
    b = _bars([
        ["2026-09-10", "1430", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-10", "1450", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-10", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000],
    ])
    # 主板 10% 涨停:昨收 10 → 11.0 正好涨停
    got = M._is_one_word_limit(b, _daily_with_prev_close(10.0), "600001", "2026-09-10")
    assert got == "涨停一字"


def test_normal_limit_up_with_intraday_swing_is_tradeable():
    """涨停但**盘中有波动**(开过板)→ 不算一字板,可成交。"""
    b = _bars([
        ["2026-09-10", "1430", 10.5, 11.0, 10.3, 10.8, 100, 1000],
        ["2026-09-10", "1500", 10.9, 11.0, 10.8, 11.0, 100, 1000],
    ])
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "600001", "2026-09-10") is None


def test_detects_down_limit():
    """跌停 → 卖不掉。"""
    b = _bars([["2026-09-10", "1500", 9.0, 9.0, 9.0, 9.0, 100, 1000]])
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "600001", "2026-09-10") == "跌停"


def test_chinext_uses_20pct_limit():
    """创业板/科创板涨跌停是 20%,不能套主板 10%。"""
    b = _bars([["2026-09-10", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000]])
    # 昨收 10 → 11.0 = +10%,对创业板(20%)**不是**涨停
    assert M._is_one_word_limit(b, _daily_with_prev_close(10.0),
                                 "300001", "2026-09-10") is None
    b2 = _bars([["2026-09-10", "1500", 12.0, 12.0, 12.0, 12.0, 100, 1000]])
    assert M._is_one_word_limit(b2, _daily_with_prev_close(10.0),
                                 "300001", "2026-09-10") == "涨停一字"


def test_sell_defers_when_blocked_and_charges_extra_cost():
    """T+1 卖出日一字涨停 → 按评估口径 §八.2 顺延持有,hold_days 增加(成本按天累计)。"""
    dates = ["2026-09-10", "2026-09-11", "2026-09-14"]
    bars = {"600001": _bars([
        ["2026-09-10", "1450", 10.0, 10.1, 9.9, 10.0, 100, 1000],
        # T+1:一字涨停,卖不掉
        ["2026-09-11", "1450", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        ["2026-09-11", "1500", 11.0, 11.0, 11.0, 11.0, 100, 1000],
        # T+2:正常,可卖
        ["2026-09-14", "1450", 11.5, 11.8, 11.2, 11.6, 100, 1000],
        ["2026-09-14", "1500", 11.6, 11.9, 11.3, 11.7, 100, 1000],
    ])}
    daily = {"600001": pd.DataFrame([
        {"date": "2026-09-09", "close": 10.0, "volume": 100},
        {"date": "2026-09-10", "close": 10.0, "volume": 100},
        {"date": "2026-09-11", "close": 11.0, "volume": 100},
    ])}
    got = M._sell_with_feasibility(bars, daily, "600001", "2026-09-10", dates)
    assert got is not None
    sell, hold_days, note = got
    assert hold_days == 2, "一字板那天应顺延,不该按收盘价成交"
    assert sell == 11.6, "应取顺延日的 14:50 价"
    assert "顺延" in note


def test_sell_normal_case_is_next_day_1450():
    dates = ["2026-09-10", "2026-09-11"]
    bars = {"600001": _bars([
        ["2026-09-10", "1450", 10.0, 10.1, 9.9, 10.0, 100, 1000],
        ["2026-09-11", "1450", 10.3, 10.5, 10.2, 10.4, 100, 1000],
        ["2026-09-11", "1500", 10.4, 10.6, 10.3, 10.5, 100, 1000],
    ])}
    daily = {"600001": pd.DataFrame([
        {"date": "2026-09-09", "close": 10.0, "volume": 100},
        {"date": "2026-09-10", "close": 10.0, "volume": 100},
    ])}
    sell, hold_days, _ = M._sell_with_feasibility(bars, daily, "600001",
                                                    "2026-09-10", dates)
    assert hold_days == 1 and sell == 10.4


# ────────────────────────────── 组合口径回撤(修 M3.a 的核心) ──────────────────────────────

def test_drawdown_is_portfolio_not_per_trade_cumsum():
    """同日多笔应先等权成"组合日收益",再算回撤 —— 不是逐笔累加。

    构造:两天,每天 2 笔。逐笔累加口径会把 4 笔串成序列得出更深的假回撤;
    组合口径下第 1 天 +10%/-10% 抵消为 0%,只有第 2 天的 -20% 是真回撤。
    """
    trades = [
        {"date": "2026-09-10", "ret": +0.10}, {"date": "2026-09-10", "ret": -0.10},
        {"date": "2026-09-11", "ret": -0.20}, {"date": "2026-09-11", "ret": -0.20},
    ]
    m = M.portfolio_metrics(trades)
    assert m["n"] == 4
    assert m["n_days"] == 2
    # 组合日收益 = [0.0, -0.20] → 净值 1.0 → 0.8,回撤 -20%
    assert m["max_dd"] == pytest.approx(-0.20, abs=1e-9)
    assert m["total_ret"] == pytest.approx(-0.20, abs=1e-9)


def test_drawdown_compounding_not_additive():
    """回撤按复利净值算(连续 -10% 两日 = -19%,不是 -20%)。"""
    trades = [{"date": "2026-09-10", "ret": -0.10},
              {"date": "2026-09-11", "ret": -0.10}]
    m = M.portfolio_metrics(trades)
    assert m["max_dd"] == pytest.approx(-0.19, abs=1e-9)


def test_max_consecutive_loss_days():
    trades = [{"date": "2026-09-0%d" % d, "ret": r} for d, r in
              zip(range(1, 7), [-0.01, -0.01, +0.02, -0.01, -0.01, -0.01])]
    assert M.portfolio_metrics(trades)["max_consec_loss"] == 3


def test_metrics_empty_is_safe():
    m = M.portfolio_metrics([])
    assert m["n"] == 0 and m["mean_ret"] is None


# ────────────────────────────── 裁决 / 显著性 ──────────────────────────────

def test_verdict_red_on_deep_drawdown():
    """评估口径 §五:回撤 ≤ -10% 直接红灯(哪怕均值为正)。"""
    m = {"n": 50, "mean_ret": 0.01, "max_dd": -0.15, "sharpe": 2.0}
    assert M.verdict(m) == "🔴红灯"


def test_verdict_red_on_nonpositive_mean():
    m = {"n": 50, "mean_ret": -0.001, "max_dd": -0.02, "sharpe": 1.0}
    assert M.verdict(m) == "🔴红灯"


def test_verdict_green_needs_all_three():
    m = {"n": 50, "mean_ret": 0.005, "max_dd": -0.05, "sharpe": 1.2}
    assert M.verdict(m) == "🟢绿灯"
    # 夏普不够 → 只能黄灯
    assert M.verdict({**m, "sharpe": 0.6}) == "🟡黄灯"


def test_bootstrap_ci_detects_insignificance():
    """均值虽正但波动极大 → CI 下界应 <0(不显著)。"""
    rets = [+0.50, -0.45, +0.48, -0.44, +0.46, -0.42, +0.02, -0.01, +0.03, -0.02]
    ci = M.bootstrap_ci(rets)
    assert ci is not None and ci[0] < 0


def test_bootstrap_ci_detects_significance():
    rets = [0.02] * 40 + [0.018] * 40
    ci = M.bootstrap_ci(rets)
    assert ci is not None and ci[0] > 0


def test_bootstrap_ci_small_sample_returns_none():
    assert M.bootstrap_ci([0.01, 0.02]) is None


# ────────────────────────────── Q3 诚实性 ──────────────────────────────

def test_q3_not_backtestable():
    """东财分时资金流只返当天 → Q3 不能假装能回测,不得出现在 STRATEGIES。"""
    assert "Q3" not in M.STRATEGIES
    assert set(M.STRATEGIES) == {"Q1", "Q2"}
