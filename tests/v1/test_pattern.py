"""形态识别引擎单测(V1 F2.1)。

锁语义:形态必须落**可计算几何特征**——对人工构造的标准序列断言命中、对反例断言不命中;
且几何参数**可配**(改 Config 行为随之变)。构造数据、不依赖真实行情。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis.v1 import pattern
from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["V1形态选股"]


def mk(closes, vols=None):
    """收盘价序列 → kline DataFrame(high/low 贴着 close,volume 可逐根给)。"""
    closes = [float(x) for x in closes]
    n = len(closes)
    vols = vols if vols is not None else [1000.0] * n
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n, freq="D"),
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes,
        "volume": [float(v) for v in vols],
    })


# ---------- 箱体 / 平台突破 ----------
def _box_df():
    base = [100 + (2 if i % 2 else -2) for i in range(20)]   # 20 根窄幅箱体 ~[98,102]
    closes = base + [108]                                     # 末根突破
    vols = [1000.0] * 20 + [2500.0]                           # 末根放量
    return mk(closes, vols)


def test_box_hit():
    r = pattern.detect_box(_box_df())
    assert r["达标"] is True
    assert r["特征"]["突破"] and r["特征"]["放量"] and r["特征"]["窄幅"]


def test_box_no_volume_no_hit():
    """突破但不放量 → 不达标(锁'量能配合'语义)。"""
    base = [100 + (2 if i % 2 else -2) for i in range(20)]
    df = mk(base + [108], [1000.0] * 21)                      # 末根不放量
    assert pattern.detect_box(df)["达标"] is False


def test_box_config_tighten_flips():
    """把'高度上限%'收到极小 → 原本窄幅的箱体判为不窄 → 不达标(锁可配)。"""
    import copy
    cfg = copy.deepcopy(_CFG)
    cfg["箱体"]["高度上限%"] = 0.5
    assert pattern.detect_box(_box_df(), cfg)["达标"] is False


# ---------- 杯柄 ----------
def _cup_df():
    rise = [95, 97, 99, 100]                                  # 左沿 rim=100
    down = list(np.linspace(99, 80, 15))                      # 回落成杯(深 20%)
    up = list(np.linspace(81, 99, 13))                        # 回补接近左沿
    handle = [98, 96, 97, 98]                                 # 浅手柄
    brk = [101]                                               # 突破左沿
    closes = rise + down + up + handle + brk
    vols = [1000.0] * (len(closes) - 1) + [3000.0]            # 末根放量
    return mk(closes, vols)


def test_cup_handle_hit():
    r = pattern.detect_cup_handle(_cup_df())
    assert r["达标"] is True, r["特征"]
    assert 12 <= r["特征"]["杯深%"] <= 40


def test_cup_depth_out_of_range_no_hit():
    """杯太浅(深度<下限)→ 不达标(锁杯深几何约束)。"""
    rise = [99, 99.5, 100]
    down = list(np.linspace(99.5, 97, 15))                    # 仅 ~3% 浅坑
    up = list(np.linspace(97, 99.5, 13))
    closes = rise + down + up + [98, 99] + [101]
    assert pattern.detect_cup_handle(mk(closes))["达标"] is False


# ---------- 楔形(收敛)----------
def _wedge_df():
    first = [90 + (20 if i % 2 else 0) for i in range(10)]    # 前段大幅 [90,110]
    second = [99 + (3 if i % 2 else 0) for i in range(10)]    # 后段收窄 [99,102]
    closes = first + second + [107]                           # 末根突破
    vols = [1000.0] * 20 + [2000.0]
    return mk(closes, vols)


def test_wedge_hit():
    r = pattern.detect_wedge(_wedge_df())
    assert r["达标"] is True and r["特征"]["收敛"] and r["特征"]["突破"]


# ---------- 旗形 ----------
def _flag_df():
    flat = [100, 100, 100]
    pole = list(np.linspace(100, 122, 10))                    # 旗杆急涨 ~22%
    flagf = [121, 120, 121, 122, 120, 121, 120, 122, 121, 120, 121, 122]  # 旗面浅横盘
    return mk(flat + pole + flagf)


def test_flag_hit():
    r = pattern.detect_flag(_flag_df())
    assert r["达标"] is True and r["特征"]["旗杆"] and r["特征"]["旗面"]


# ---------- 反例:平盘噪声,四类都不该命中 ----------
def test_flat_noise_no_pattern():
    rng = [100 + (1 if i % 2 else -1) for i in range(40)]     # 一直窄幅震荡、无突破
    d = pattern.detect(mk(rng))
    assert d["达标"] is False and d["命中形态"] == []


# ---------- detect() 汇总结构 ----------
def test_detect_aggregates():
    d = pattern.detect(_box_df())
    assert "箱体" in d["命中形态"]
    assert set(d["明细"]) == {"箱体", "杯柄", "楔形", "旗形"}
    assert d["达标"] is True
