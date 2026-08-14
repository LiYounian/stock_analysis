"""回踩低吸统一入场框架 单测(约法6:锁死"为什么改"的语义)。

锁语义:
  · Stage1 收紧箱体 detect_box_strict:振幅窄带 [8,20] + 触碰≥2/横盘/缩量**三软改硬**
    + 站稳突破 + 放量,全部硬门。任一硬门破 → 不达标。
  · Stage2 回踩缩量 pullback_shrink_at:回踩到支撑(≤容差且未有效跌破)+ 缩量
    +(可选)趋势未破;泛化算子(不绑 S02 C1–C5)。缺一不命中。
  · 无未来函数:突破按 b、回踩按 t 只用 ≤ 各自当日;扰动未来根不改历史判定。
  · 显著性 聚类t:同票重叠交易相关 → 聚类 t 比朴素 t 更保守(|t| 更小)。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.pattern_screener import pattern as pat
from tools.backtest import backtest_pullback as bp
from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_pullback as sp

_SC = THRESHOLDS["回踩低吸"]["严格箱体"]
_PB = THRESHOLDS["回踩低吸"]["回踩"]


# ———————————————————— 合成数据 ————————————————————
def _strict_box_df(lo=10.0, hi=11.5, break_c=12.2, cycles=12,
                   front_vol=1000.0, back_vol=650.0, break_vol=2600.0,
                   sideways=True) -> pd.DataFrame:
    """严格横盘箱体(上下轨各多次触碰+回落/反弹,后 1/3 缩量)+ 末根放量突破。

    振幅 (hi-lo)/lo ≈ 15%(落 [8,20] 带内)。sideways=False → 叠加单边漂移破横盘硬门。
    """
    rows = []
    cycle = [
        (hi, hi * 1.004, hi * 0.99),        # 触上轨
        (10.7, hi * 1.0, 10.6),             # 回落
        (lo, lo * 1.03, lo * 0.995),        # 触下轨
        (10.7, 10.9, lo * 1.05),            # 反弹
    ]
    n_box = cycles * 4
    for k in range(cycles):
        for j, (c, h, l) in enumerate(cycle):
            idx = k * 4 + j
            drift = (idx * 0.04) if not sideways else 0.0     # 破横盘用
            v = front_vol if idx < n_box * 2 // 3 else back_vol
            rows.append((c + drift, h + drift, l + drift, v))
    rows.append((break_c, break_c * 1.01, break_c * 0.985, break_vol))
    df = pd.DataFrame(rows, columns=["close", "high", "low", "volume"])
    df["open"] = df["close"]
    df["date"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
    return df[["date", "open", "high", "low", "close", "volume"]]


def _series_df(closes, vols) -> pd.DataFrame:
    closes = np.asarray(closes, float)
    df = pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=len(closes), freq="D"),
        "open": closes, "high": closes * 1.005, "low": closes * 0.995,
        "close": closes, "volume": np.asarray(vols, float),
    })
    return df


# ———————————————————— Stage1 收紧箱体边界 ————————————————————
def test_strict_box_hit_in_band():
    """振幅落 [8,20]、三硬门齐 + 放量站稳突破 → 达标。"""
    df = _strict_box_df()
    r = pat.detect_box_strict(df, _SC)
    assert r["达标"] is True
    assert 8.0 <= r["特征"]["振幅%"] <= 20.0
    assert r["特征"]["箱顶"] > 0                      # 箱顶=支撑,供 Stage2


def test_strict_box_reject_amplitude_too_wide():
    """振幅 > 20%(超窄带上界)→ 严格口径拒(v2 宽带曾放行,收紧后不放行)。"""
    df = _strict_box_df(lo=10.0, hi=13.0, break_c=14.0)   # 振幅 30%
    r = pat.detect_box_strict(df, _SC)
    assert r["达标"] is False


def test_strict_box_reject_when_not_sideways():
    """横盘硬门被破(叠加单边漂移)→ 不达标(三软改硬:横盘不再是可放行软信号)。"""
    df = _strict_box_df(sideways=False)
    r = pat.detect_box_strict(df, _SC)
    assert r["达标"] is False


def test_strict_box_reject_when_no_shrink():
    """后段不缩量 → 缩量硬门破 → 不达标。"""
    df = _strict_box_df(front_vol=1000.0, back_vol=1000.0)   # 后段不缩量
    r = pat.detect_box_strict(df, _SC)
    assert r["达标"] is False


def test_strict_box_reject_when_breakout_not_volume():
    """突破日不放量 → 放量硬门破 → 不达标。"""
    df = _strict_box_df(break_vol=800.0)                 # 突破根量 < 箱体均量×1.8
    r = pat.detect_box_strict(df, _SC)
    assert r["达标"] is False


# ———————————————————— Stage2 回踩缩量判定 ————————————————————
def test_pullback_hit():
    """收盘贴支撑(未有效跌破)+ 缩量 + 趋势未破 → 命中。"""
    closes = [10.0] * 40
    vols = [1000.0] * 39 + [500.0]                      # 末根缩量(<MA10×0.7)
    df = _series_df(closes, vols)
    r = sp.pullback_shrink_at(df, len(df) - 1, support=10.0, cfg=THRESHOLDS["回踩低吸"])
    assert r["命中"] is True
    assert r["回踩到位"] and r["缩量"] and r["趋势未破"]


def test_pullback_miss_support_broken():
    """收盘有效跌破所有支撑 → 回踩不到位 → 不命中(即便缩量)。"""
    closes = [10.0] * 39 + [9.5]                         # 末根较支撑跌 5% (>有效跌破2%)
    vols = [1000.0] * 39 + [500.0]
    df = _series_df(closes, vols)
    r = sp.pullback_shrink_at(df, len(df) - 1, support=10.0, cfg=THRESHOLDS["回踩低吸"])
    assert r["回踩到位"] is False
    assert r["命中"] is False


def test_pullback_miss_high_volume():
    """贴支撑但当日放量(非缩量)→ 缩量门破 → 不命中。"""
    closes = [10.0] * 40
    vols = [1000.0] * 39 + [2000.0]                      # 末根放量
    df = _series_df(closes, vols)
    r = sp.pullback_shrink_at(df, len(df) - 1, support=10.0, cfg=THRESHOLDS["回踩低吸"])
    assert r["缩量"] is False
    assert r["命中"] is False


def test_pullback_miss_trend_broken():
    """贴静态支撑且缩量,但收盘有效跌破 MA20(趋势门破)→ 不命中。"""
    closes = list(np.linspace(11.0, 9.0, 40))            # 单边下行,末根 9.0 远低于 MA20
    vols = [1000.0] * 39 + [500.0]
    df = _series_df(closes, vols)
    # 支撑设在末根价附近,使"回踩到位"成立,单独隔离趋势门
    r = sp.pullback_shrink_at(df, len(df) - 1, support=9.0, cfg=THRESHOLDS["回踩低吸"])
    assert r["回踩到位"] is True and r["缩量"] is True
    assert r["趋势未破"] is False
    assert r["命中"] is False


# ———————————————————— 无未来函数 ————————————————————
def test_no_lookahead_pullback():
    """扰动 t 之后所有根(价×5、量×10)→ 第 t 根回踩判定不变。"""
    closes = [10.0] * 45
    vols = [1000.0] * 39 + [500.0] + [1000.0] * 5
    df = _series_df(closes, vols)
    t = 39
    base = sp.pullback_shrink_at(df, t, support=10.0, cfg=THRESHOLDS["回踩低吸"])["命中"]
    df2 = df.copy()
    df2.loc[df2.index[t + 1:], ["open", "high", "low", "close"]] *= 5.0
    df2.loc[df2.index[t + 1:], "volume"] *= 10.0
    pert = sp.pullback_shrink_at(df2, t, support=10.0, cfg=THRESHOLDS["回踩低吸"])["命中"]
    assert base == pert is True


def test_no_lookahead_strict_box():
    """扰动突破日之后所有根 → 突破日 detect_box_strict 达标判定不变。"""
    df = _strict_box_df()
    b = len(df) - 1
    # 追加未来暴涨根,再截回 [0,b] 判定,结论必须与原判定一致
    extra = df.iloc[[-1]].copy()
    extra.loc[:, ["open", "high", "low", "close"]] = 99.0
    extra.loc[:, "volume"] = 999999.0
    df_future = pd.concat([df, extra, extra], ignore_index=True)
    r0 = pat.detect_box_strict(df.iloc[: b + 1], _SC)["达标"]
    r1 = pat.detect_box_strict(df_future.iloc[: b + 1], _SC)["达标"]
    assert r0 == r1 is True


# ———————————————————— 显著性 · 聚类 t ————————————————————
def test_clustered_t_more_conservative_than_naive():
    """同票内正相关(组内同号)→ 聚类 t 的 |t| 应显著小于朴素 t(更保守、不高估显著性)。"""
    import math
    import statistics
    values = [0.1] * 10 + [-0.05] * 10                  # 两簇,各簇内完全相关
    codes = ["A"] * 10 + ["B"] * 10
    ct = bp._clustered_t(values, codes)
    # 朴素 t(iid 假设)
    mean = statistics.mean(values)
    sd = statistics.pstdev(values) * math.sqrt(len(values) / (len(values) - 1))
    se = sd / math.sqrt(len(values))
    t_naive = mean / se
    assert ct["簇数"] == 2
    assert ct["t"] is not None
    assert abs(ct["t"]) < abs(t_naive)                   # 聚类更保守


def test_clustered_t_small_sample():
    """样本 < 2 → t/p 置空(诚实,不编造)。"""
    ct = bp._clustered_t([0.05], ["A"])
    assert ct["t"] is None and ct["p"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
