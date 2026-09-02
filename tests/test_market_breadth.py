"""市场广度聚合器单测(大盘预测 v0.5)。

锁语义:
  · 涨停线**按板块**路由(主板10/创业·科创20/北交所30;主板ST 5% 启发式);
  · 封板判定(close≈high)+ pct 落在允许限价容差带才记涨停,防"涨5%但没封"误记;
  · 全市场聚合的涨跌/涨停/中位涨幅家数与手工构造一致。
构造数据、monkeypatch store,不依赖真实行情。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.market_forecast import breadth as B


# ————————————————————————— 板块 / 涨停线路由 —————————————————————————
def test_board_routing():
    assert B.board_of("600000") == "主板"
    assert B.board_of("000001") == "主板"
    assert B.board_of("300750") == "创业板"
    assert B.board_of("688981") == "科创板"
    assert B.board_of("920992") == "北交所"
    assert B.board_of("830799") == "北交所"


def test_allowed_limits_by_board():
    assert set(B.allowed_limits("600000")) == {10.0, 5.0}   # 主板含 ST 5%
    assert B.allowed_limits("300750") == [20.0]
    assert B.allowed_limits("688981") == [20.0]
    assert B.allowed_limits("920992") == [30.0]


def _hit(code, pct, close, high, low, up=True):
    from tools.config.strategy import THRESHOLDS
    cfg = THRESHOLDS["大盘预测"]
    return bool(B._hit_limit(
        np.array([pct]), np.array([close]), np.array([high]), np.array([low]),
        B.allowed_limits(code, cfg), cfg["涨停容差"], cfg["封板容差"], up=up)[0])


def test_limit_up_by_board():
    # 主板 +10% 封板 → 涨停
    assert _hit("600000", 10.0, 11.0, 11.0, 10.5, up=True)
    # 创业板 +10% 封板 → **非**涨停(创业板限价 20%)
    assert not _hit("300750", 10.0, 11.0, 11.0, 10.5, up=True)
    # 创业板 +20% 封板 → 涨停
    assert _hit("300750", 20.0, 12.0, 12.0, 11.0, up=True)
    # 北交所 +30% 封板 → 涨停
    assert _hit("920992", 30.0, 13.0, 13.0, 11.0, up=True)


def test_st_5pct_heuristic_and_no_false_positive():
    # 主板 ST:+5% 封板 → 记涨停(ST 启发式)
    assert _hit("600000", 5.0, 10.5, 10.5, 10.1, up=True)
    # 主板普通票 +7% 未封板(close<high)→ **不**记涨停(既不在带内也没封)
    assert not _hit("600000", 7.0, 10.7, 10.9, 10.2, up=True)
    # 主板 +5% 但**没封板**(close 明显低于 high)→ 不记(防误记 ST)
    assert not _hit("600000", 5.0, 10.5, 10.9, 10.1, up=True)


def test_limit_down_by_board():
    # 主板 −10% 封死(close≈low)→ 跌停
    assert _hit("600000", -10.0, 9.0, 9.4, 9.0, up=False)
    # 创业板 −10% → 非跌停(限价 20%)
    assert not _hit("300750", -10.0, 9.0, 9.4, 9.0, up=False)


# ————————————————————————— 全市场聚合 —————————————————————————
def _mk(dates, pct, close=None, high=None, low=None):
    n = len(dates)
    close = close if close is not None else [10.0] * n
    high = high if high is not None else close
    low = low if low is not None else close
    return pd.DataFrame({"date": pd.to_datetime(dates), "open": close,
                         "high": high, "low": low, "close": close,
                         "volume": [1000.0] * n, "pct_chg": pct})


def test_compute_breadth_counts(monkeypatch):
    """3 票 × 2 日 的确定性宇宙 → 校验 adv/dec/limit_up/中位涨幅。"""
    dates = ["2024-01-02", "2024-01-03"]
    universe = {
        # 主板涨停(第2日 +10% 封板)
        "600000": _mk(dates, [1.0, 10.0], close=[10.0, 11.0], high=[10.2, 11.0], low=[9.9, 10.5]),
        # 创业板下跌
        "300750": _mk(dates, [-2.0, -3.0], close=[20.0, 19.4], high=[20.1, 19.8], low=[19.5, 19.3]),
        # 主板小涨(不涨停)
        "000001": _mk(dates, [0.5, 1.0], close=[8.0, 8.08], high=[8.1, 8.1], low=[7.9, 8.0]),
    }
    from tools.store import repo as store
    monkeypatch.setattr(store, "list_master_codes", lambda: list(universe))
    monkeypatch.setattr(store, "get_master_kline", lambda c: universe[c])
    # 免去数据根解析
    monkeypatch.setattr("tools.analysis.market_forecast.dataroot.ensure_data_root",
                        lambda *a, **k: None)

    res = B.compute_breadth(codes=list(universe))
    d2 = pd.Timestamp("2024-01-03")
    row = res.loc[d2]
    assert row["total"] == 3
    assert row["adv"] == 2 and row["dec"] == 1          # 600000、000001 涨;300750 跌
    assert row["limit_up"] == 1 and row["limit_down"] == 0   # 仅 600000 涨停
    # 中位涨幅 = median(10, -3, 1) = 1
    assert abs(row["median_pct"] - 1.0) < 1e-9
    # 净涨占比 = (2-1)/3
    assert abs(row["net_adv"] - (1 / 3)) < 1e-9


def test_new_high_is_causal(monkeypatch):
    """创 N 日新高只用回看窗(rolling max 含当日),不看未来 → 末日创新高、非末日不因未来降级。"""
    dates = pd.date_range("2024-01-01", periods=30, freq="D")
    # 单调上升 → 每天都是"至今新高";用 20 日窗
    close = list(np.linspace(10, 20, 30))
    df = pd.DataFrame({"date": dates, "open": close, "high": close, "low": close,
                       "close": close, "volume": [1.0] * 30, "pct_chg": [1.0] * 30})
    ind = B._per_stock_indicators(df, "600000")
    # 第 20 根起 nh20 有效(min_periods=20),且单调上升 → 全为 1
    assert ind["nh20"].iloc[19:].sum() == len(ind) - 19
    # 前 19 根 nh20=0(窗口未满,rolling 产生 NaN→比较为 False→0)
    assert ind["nh20"].iloc[:19].sum() == 0
