"""箱体 v2(提供者规格重构)单测。

锁语义:
  · detect_box_v2:振幅带+站稳突破+放量=硬门;触碰/缩量/横盘=软信号(结构评分≥下限)。
  · 趋势门(screen_box):站上 MA200 + 短均线多头,剔除下跌趋势(治即死61%)。
  · 结构化输出:入选补 箱顶/箱底/止损。
  · 防未来函数:signal_at 只用 [0,t],未来根不改历史判定。
  · 校准:行云 300209 应被识别为标准箱体+有效突破(数据缺失则 skip)。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.pattern_screener import pattern as pat
from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_box as sb

_C2 = THRESHOLDS["形态选股"]["箱体v2"]


# ———————————— 合成数据 ————————————
def _box_df(cycles: int = 10, lo: float = 10.0, hi: float = 11.5,
            break_c: float = 12.2, front_vol: float = 1000.0,
            back_vol: float = 700.0, break_vol: float = 2600.0,
            uptrend_pre: int = 0) -> pd.DataFrame:
    """构造:可选前置上升趋势 + 横盘箱体(上下轨各多次触碰+回落/反弹,后期缩量)+ 末根放量突破。"""
    rows = []
    # 前置上升趋势(供趋势门:站上 MA200 + 短多头)
    for i in range(uptrend_pre):
        c = 5.0 + (lo - 5.0) * (i + 1) / max(1, uptrend_pre)
        rows.append((c, c * 1.01, c * 0.99, c, 900.0))
    # 箱体:4 根一周期 [触顶, 中偏下, 触底, 中偏上]
    cycle = [
        (hi, hi * 1.004, hi * 0.99),        # 触上轨
        (10.7, hi * 1.0, 10.6),             # 回落(low 远离上轨)
        (lo, lo * 1.03, lo * 0.995),        # 触下轨
        (10.7, 10.9, lo * 1.05),            # 反弹(high 远离下轨)
    ]
    n_box = cycles * 4
    for k in range(cycles):
        for j, (c, h, l) in enumerate(cycle):
            idx = k * 4 + j
            v = front_vol if idx < n_box * 2 // 3 else back_vol   # 后 1/3 缩量
            rows.append((c, h, l, c, v))
    rows.append((break_c, break_c * 1.01, break_c * 0.985, break_c, break_vol))  # 突破根
    df = pd.DataFrame(rows, columns=["close_tmp", "high", "low", "_c", "volume"])
    df["open"] = df["close_tmp"]
    df["close"] = df["close_tmp"]
    df["date"] = pd.date_range("2024-01-01", periods=len(df), freq="D")
    return df[["date", "open", "high", "low", "close", "volume"]]


# ———————————— 软信号 helpers ————————————
def test_count_touches_upper_lower():
    highs = np.array([11.5, 11.0, 10.2, 11.48, 11.0, 10.2, 10.6])
    lows = np.array([11.3, 10.5, 9.98, 11.2, 10.5, 9.98, 10.4])
    up = pat._count_touches(highs, lows, rail=11.5, tol=0.01, rebound=0.03, upper=True)
    dn = pat._count_touches(highs, lows, rail=9.98, tol=0.01, rebound=0.03, upper=False)
    assert up >= 2 and dn >= 2


def test_shrinking_volume():
    vols = [1000.0] * 20 + [600.0] * 10          # 后 1/3 明显缩量
    ok, q = pat._shrinking_volume(vols, back_frac=0.3333, ratio=0.8)
    assert ok is True and q < 0.8
    ok2, _ = pat._shrinking_volume([1000.0] * 30, back_frac=0.3333, ratio=0.8)
    assert ok2 is False                          # 不缩量


def test_sideways_flat_vs_trend():
    flat = [10.0 + (0.1 if i % 2 else -0.1) for i in range(30)]
    ok, drift = pat._sideways(flat, slope_cap=0.15)
    assert ok is True and abs(drift) < 0.15
    trend = [10.0 + i * 0.3 for i in range(30)]  # 单边上行
    ok2, _ = pat._sideways(trend, slope_cap=0.15)
    assert ok2 is False


# ———————————— detect_box_v2 ————————————
def test_detect_box_v2_hit():
    df = _box_df()
    r = pat.detect_box_v2(df)
    assert r["达标"] is True
    f = r["特征"]
    assert f["站稳"] and f["放量"] and f["结构达标"]
    assert 8.0 <= f["振幅%"] <= 30.0
    assert f["箱底"] < f["箱顶"] < df["close"].iloc[-1]     # 箱体在突破根之下


def test_detect_box_v2_uptrend_not_box():
    """纯单边上行不是箱体(振幅越界/不横盘)→ 不达标。"""
    up = [(5 + i * 0.2) for i in range(60)]
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=60, freq="D"),
                       "open": up, "high": [c * 1.01 for c in up],
                       "low": [c * 0.99 for c in up], "close": up,
                       "volume": [1000.0] * 60})
    assert pat.detect_box_v2(df)["达标"] is False


def test_detect_box_v2_breakout_needs_volume():
    """箱体成形但突破根不放量 → 硬门不过,不达标。"""
    df = _box_df(break_vol=900.0)                # 突破根量能不足
    r = pat.detect_box_v2(df)
    assert r["特征"]["放量"] is False and r["达标"] is False


# ———————————— 趋势门 ————————————
def _trend_df(slope: float, n: int = 220) -> pd.DataFrame:
    close = [10.0 + slope * i for i in range(n)]
    return pd.DataFrame({"date": pd.date_range("2023-01-01", periods=n, freq="D"),
                         "open": close, "high": [c * 1.005 for c in close],
                         "low": [c * 0.995 for c in close], "close": close,
                         "volume": [1000.0] * n})


def test_trend_gate_uptrend_pass():
    df = _trend_df(slope=0.05)                    # 稳步上行:站上MA200 + 短多头
    ok, d = sb._trend_gate(df, len(df) - 1, _C2)
    assert ok is True and d["站上MA200"] and d["MA5>10>20"]


def test_trend_gate_downtrend_reject():
    df = _trend_df(slope=-0.03)                   # 下跌趋势 → 剔除(治即死)
    ok, _ = sb._trend_gate(df, len(df) - 1, _C2)
    assert ok is False


def test_trend_gate_insufficient_history():
    df = _trend_df(slope=0.05, n=150)             # <200 根 → MA200 不可算 → 不过(保守)
    ok, d = sb._trend_gate(df, len(df) - 1, _C2)
    assert ok is False and "历史不足MA200" in d


# ———————————— signal_at:趋势门集成 + 结构化输出 + 防未来函数 ————————————
def test_signal_at_hit_with_trend_and_stop():
    """上升趋势后的箱体突破 → SELECT,且带 箱顶/箱底/止损。"""
    df = _box_df(uptrend_pre=200)                 # 200 根上行铺垫,末段箱体突破
    r = sb.signal_at(df, len(df) - 1)
    assert r["SELECT"] is True
    assert "止损" in r["特征"] and r["特征"]["止损"]["止损价"] < df["close"].iloc[-1]
    assert r["特征"]["箱底"] < r["特征"]["箱顶"]


def test_signal_at_no_lookahead():
    """signal_at 只用 [0,t]:在 t 处的判定不因后续追加的未来根而改变。"""
    df = _box_df(uptrend_pre=200)
    t = len(df) - 1
    verdict_now = sb.signal_at(df, t)["SELECT"]
    future = pd.DataFrame({"date": pd.date_range("2030-01-01", periods=5, freq="D"),
                           "open": [99] * 5, "high": [110] * 5, "low": [90] * 5,
                           "close": [99] * 5, "volume": [9999.0] * 5})
    ext = pd.concat([df, future], ignore_index=True)
    assert sb.signal_at(ext, t)["SELECT"] == verdict_now


def test_signal_at_downtrend_box_rejected_by_gate():
    """同样的箱体突破,但整体处于下跌趋势 → 趋势门剔除(即使几何达标)。"""
    df = _box_df(uptrend_pre=0)                    # 无上行铺垫,<200 根 → 趋势门 MA200 不过
    r = sb.signal_at(df, len(df) - 1)
    assert r["SELECT"] is False


# ———————————— 校准:行云 300209(数据缺失则 skip)————————————
def test_calibration_xingyun_300209():
    from tools.collectors import market
    try:
        df = market.load_kline("300209").reset_index(drop=True)
    except Exception:
        pytest.skip("300209 主档不可用(需主 repo 数据 symlink)")
    dates = df["date"].astype(str).str[:10].tolist()
    if "2026-01-26" not in dates:
        pytest.skip("300209 数据未覆盖校准日")
    t = dates.index("2026-01-26")
    r = sb.signal_at(df, t)
    assert r["SELECT"] is True                    # 标准箱体+有效突破+趋势门过
    assert r["特征"]["箱底"] < r["特征"]["箱顶"]
