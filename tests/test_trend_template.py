"""趋势模板筛选 · 单测。

锁住需求 §12 验收标准的可测部分:指标与标准 SMA 一致、无未来函数、52 周高低窗、
Return250、ValidBars 数据完整性、配置零硬编码(§8 全参数在位)。
后续步骤(conditions/rps/编排)的用例追加到本文件。
"""
from __future__ import annotations

import math

import pandas as pd

from tools.analysis.trend_template import indicators as ind
from tools.config.strategy import THRESHOLDS


# ————————————————————— 指标层:MA —————————————————————

def test_ma_matches_standard_sma():
    xs = [float(i) for i in range(1, 21)]          # 1..20
    # 第 19 根(0-based)的 5 日均线 = mean(16,17,18,19,20)
    assert ind.ma(xs, 19, 5) == sum([16, 17, 18, 19, 20]) / 5
    # 与 pandas rolling 口径一致
    s = pd.Series(xs)
    assert math.isclose(ind.ma(xs, 19, 5), float(s.rolling(5).mean().iloc[19]))


def test_ma_insufficient_returns_none_not_zero():
    xs = [10.0, 11.0, 12.0]
    assert ind.ma(xs, 1, 5) is None          # 只有 2 根,不足 5 → None(不是 0)
    assert ind.ma(xs, 2, 3) == 11.0          # 恰好 3 根可算


def test_ma_no_lookahead_truncation_invariance():
    xs = [float(i) for i in range(1, 31)]
    t, n = 20, 10
    full = ind.ma(xs, t, n)
    trunc = ind.ma(xs[: t + 1], t, n)        # 砍掉 t 之后 → 结果不变
    assert full == trunc


# ————————————————————— 52 周高/低 —————————————————————

def test_lowest_highest_window():
    low = [5.0] * 249 + [3.0, 9.0]           # 251 根,最后两根 3、9
    high = [8.0] * 249 + [7.0, 12.0]
    t = 250
    assert ind.lowest_low(low, t, win=250) == 3.0     # 窗 [1..250] 内最低=3
    assert ind.highest_high(high, t, win=250) == 12.0


def test_52w_insufficient_returns_none():
    low = [5.0] * 100
    assert ind.lowest_low(low, 99, win=250) is None   # 不足 250 → None
    assert ind.highest_high([8.0] * 100, 99, win=250) is None


# ————————————————————— Return250 —————————————————————

def test_return_n_basic():
    xs = [10.0] * 1 + [0.0] * 249 + [13.0]   # index0=10, index250=13
    xs[0] = 10.0
    # 用干净序列避免 0 干扰:构造 index (t-250)=10, t=13
    close = [1.0] * 251
    close[0] = 10.0
    close[250] = 13.0
    assert math.isclose(ind.return_n(close, 250, 250), 13.0 / 10.0 - 1.0)


def test_return_n_guards():
    close = [1.0] * 251
    assert ind.return_n(close, 100, 250) is None      # t-250<0 不足
    close[0] = 0.0
    assert ind.return_n(close, 250, 250) is None      # 基准价 ≤0 → None


# ————————————————————— ValidBars —————————————————————

def test_valid_bars_excludes_invalid():
    df = pd.DataFrame({"close": [10.0, float("nan"), 0.0, -1.0, 12.0]})
    # 有效:index0(10)、index4(12);NaN/0/负 都不计
    assert ind.valid_bars(df, 4) == 2


def test_valid_bars_respects_is_trading():
    df = pd.DataFrame({
        "close": [10.0, 11.0, 12.0],
        "is_trading": [True, False, True],
    })
    assert ind.valid_bars(df, 2) == 2         # 停牌那根(index1)不计


# ————————————————————— 配置零硬编码(§8) —————————————————————

def test_config_block_has_all_section8_params():
    cfg = THRESHOLDS["趋势模板"]
    for k in ("ma_short", "ma_medium", "ma_long", "trend_lookback_days",
              "week52_window", "min_bars", "min_gain_from_52w_low",
              "max_distance_from_52w_high", "min_rps", "min_price",
              "min_amount", "adjustment", "universe"):
        assert k in cfg, f"缺配置参数 {k}"
    assert cfg["adjustment"] == "qfq"
    assert cfg["week52_window"] == 250 and cfg["min_bars"] == 250
