"""午盘 Q · M2 · signals 纯函数测试(不触外部依赖)。"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.strategy import midday_q_signals as S


# ────────────────────────────── 涨跌停价 ──────────────────────────────

def test_limit_up_price_main_board():
    q = {"prev_close": 10.0, "name": "贵州茅台"}
    assert S.limit_up_price(q, "600519") == pytest.approx(11.0)


def test_limit_up_price_chinext():
    q = {"prev_close": 100.0, "name": "宁德时代"}
    assert S.limit_up_price(q, "300750") == pytest.approx(120.0)


def test_limit_up_price_st():
    q = {"prev_close": 5.0, "name": "*ST 康美"}
    assert S.limit_up_price(q, "600518") == pytest.approx(5.25)


def test_limit_up_price_missing_prev_close():
    assert S.limit_up_price({}, "600519") is None


def test_limit_down_price_main_board():
    q = {"prev_close": 10.0, "name": "贵州茅台"}
    assert S.limit_down_price(q, "600519") == pytest.approx(9.0)


# ────────────────────────────── 卫生过滤 ──────────────────────────────

def test_not_limit_up_true():
    """价 = 10.7 < 11 × 0.985 = 10.835 → True。"""
    q = {"prev_close": 10.0, "price": 10.7, "name": "普通股"}
    assert S.not_limit_up(q, "600519") is True


def test_not_limit_up_false_at_limit():
    """价 = 10.9 > 11 × 0.985 = 10.835 → False(接近涨停,排除)。"""
    q = {"prev_close": 10.0, "price": 10.9, "name": "普通股"}
    assert S.not_limit_up(q, "600519") is False


def test_not_down_limit_true():
    q = {"prev_close": 10.0, "price": 9.2, "name": "普通股"}
    assert S.not_down_limit(q, "600519") is True


def test_not_down_limit_false_at_limit():
    q = {"prev_close": 10.0, "price": 9.05, "name": "普通股"}
    assert S.not_down_limit(q, "600519") is False


def test_liquidity_ok_default():
    assert S.liquidity_ok({"amount_wan": 10000}) is True
    assert S.liquidity_ok({"amount_wan": 3000}) is False
    assert S.liquidity_ok({}) is False


def test_not_st_by_name():
    assert S.not_st_not_new({"name": "*ST 康美"}) is False
    assert S.not_st_not_new({"name": "ST 康美"}) is False
    assert S.not_st_not_new({"name": "贵州茅台"}) is True


def test_not_st_not_new_listing_days():
    assert S.not_st_not_new({"name": "新股 A"}, listing_days=30) is False
    assert S.not_st_not_new({"name": "老股 B"}, listing_days=200) is True


def test_not_st_missing_listing_days_only_st_check():
    """listing_days=None → 只判 ST,不误杀。"""
    assert S.not_st_not_new({"name": "老股 B"}, listing_days=None) is True


# ────────────────────────────── 通用形态信号 ──────────────────────────────

def test_intraday_return():
    assert S.intraday_return({"price": 10.5, "open": 10.0}) == pytest.approx(0.05)
    assert S.intraday_return({"price": None, "open": 10.0}) is None
    assert S.intraday_return({"price": 10.5, "open": 0}) is None


def test_distance_to_day_high():
    assert S.distance_to_day_high({"price": 10.0, "high": 10.5}) == pytest.approx(1 - 10/10.5)
    assert S.distance_to_day_high({"price": 10.0, "high": None}) is None


def test_intraday_low_return():
    assert S.intraday_low_return({"low": 9.7, "open": 10.0}) == pytest.approx(-0.03)


def test_rebound_from_low():
    assert S.rebound_from_low({"price": 10.0, "low": 9.7}) == pytest.approx(10/9.7 - 1)


def test_am_pm_vol_ratio_with_am():
    assert S.am_pm_vol_ratio({"volume": 6000}, {"volume": 3000}) == pytest.approx(2.0)
    assert S.am_pm_vol_ratio({"volume": 6000}, {"volume": 0}) is None


def test_am_pm_vol_ratio_fallback_vol_ratio():
    """am_quote=None → 用 pm.vol_ratio 兜底。"""
    assert S.am_pm_vol_ratio({"vol_ratio": 1.5}, None) == 1.5


# ────────────────────────────── T-1 kline ──────────────────────────────

def test_ma_stacked_bullish_true():
    """20 根递增收盘 → MA5>MA10>MA20 + open 站上 MA5。"""
    df = pd.DataFrame({"close": [i for i in range(1, 26)]})
    assert S.ma_stacked_bullish(df, current_open=27.0) is True


def test_ma_stacked_bullish_short_history_default_true():
    """数据不足 → 默认 True(降级不判,同 not_long_term_downtrend);strict_when_missing=True → False。"""
    df = pd.DataFrame({"close": [1, 2, 3]})
    assert S.ma_stacked_bullish(df, current_open=5.0) is True
    assert S.ma_stacked_bullish(df, current_open=5.0, strict_when_missing=True) is False


def test_ma_stacked_bullish_none_kline_default_true():
    """kline=None 也降级 True(生产没跑 kline 采集时的常见场景)。"""
    assert S.ma_stacked_bullish(None, current_open=10.0) is True
    assert S.ma_stacked_bullish(None, current_open=10.0, strict_when_missing=True) is False


def test_ma_stacked_bullish_false_below_ma5():
    df = pd.DataFrame({"close": [i for i in range(1, 26)]})
    assert S.ma_stacked_bullish(df, current_open=15.0) is False   # MA5 ≈ 23


def test_not_long_term_downtrend_true():
    df = pd.DataFrame({"close": [10.0] * 60})
    assert S.not_long_term_downtrend(df, current_open=9.5) is True   # 9.5 > 10×0.9=9


def test_not_long_term_downtrend_false():
    df = pd.DataFrame({"close": [10.0] * 60})
    assert S.not_long_term_downtrend(df, current_open=8.0) is False   # 8 < 9


def test_not_long_term_downtrend_short_history_ok():
    """样本 < 60 → True(降级不判)。"""
    df = pd.DataFrame({"close": [10.0] * 30})
    assert S.not_long_term_downtrend(df, current_open=1.0) is True


# ────────────────────────────── 板块排名 ──────────────────────────────

def test_sector_rank_in_top():
    ranks = {"电子": 2, "计算机": 6, "银行": 15}
    assert S.sector_rank_in_top("电子", ranks, top_n=5) is True
    assert S.sector_rank_in_top("计算机", ranks, top_n=5) is False
    assert S.sector_rank_in_top(None, ranks) is False
    assert S.sector_rank_in_top("电子", None) is False


# ────────────────────────────── 资金流(Q3) ──────────────────────────────

def _fake_ff(times_pcts: list[tuple[str, float, float, float, float, float]]) -> pd.DataFrame:
    """构造 fake fundflow df:times_pcts = [(HH:MM, 主力, 小单, 中单, 大单, 超大单)]。"""
    rows = []
    for t, m, s, mid, l, sl in times_pcts:
        rows.append({
            "time": pd.Timestamp(f"2026-09-07 {t}"),
            "主力净流入": m, "小单净流入": s, "中单净流入": mid,
            "大单净流入": l, "超大单净流入": sl, "主力净占比": 0.0,
        })
    return pd.DataFrame(rows)


def test_main_net_since_sum():
    df = _fake_ff([
        ("11:30", 100.0, 0, 0, 0, 0),
        ("13:05", 200.0, 0, 0, 0, 0),
        ("13:35", -50.0, 0, 0, 0, 0),
        ("14:00", 300.0, 0, 0, 0, 0),
    ])
    assert S.main_net_since(df, "13:00") == pytest.approx(450.0)   # 200 - 50 + 300


def test_main_net_since_empty():
    assert S.main_net_since(None) is None
    assert S.main_net_since(pd.DataFrame()) is None


def test_large_order_pct():
    """(|大| + |超大|) / 五单绝对值总和 ∈ [0,1]。"""
    df = _fake_ff([("14:00", 100, 50, 30, 80, 40)])
    # 分子 = 80+40 = 120;分母 = 100+50+30+80+40 = 300
    assert S.large_order_pct(df) == pytest.approx(120/300)


def test_large_order_pct_empty():
    assert S.large_order_pct(None) is None
    assert S.large_order_pct(pd.DataFrame()) is None
