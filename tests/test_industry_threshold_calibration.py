"""锁住 2026-08 行业财报专家阈值标定(feat/industry-threshold-calib)的语义与取值。

标定方法(见 tools/analysis/financial/industry/_calibration.py):对财报快照池内 175 票、
3 年 12 期报告(时序严格锚披露日,防未来函数)做**行业历史截面分布锚定**——把明显失准的工程
占位(命中≈行业半数=泛滥,或命中≈0=从不发声)改到该行业分布的合理尾部(hi 尾≈p85-90 / lo 尾≈p10-20)。
前瞻价"暴雷"标签在本薄样本上区分度弱,故不做结局监督标定(诚实标注,不硬编)。

本测试双重锁定:
  ① 常量取值锁(防未来重写无意改回旧占位);
  ② 边界 hit/miss 语义锁(旧阈值会误判、新阈值不误判的样本点)。
样本薄行业(<8 票)保留占位、不在此断言取值。
"""
import importlib

import pytest


def _codes(flags):
    return {f["code"] for f in flags if f.get("命中")}


# ── ① 标定后常量取值锁 ─────────────────────────────────────────────────────
# (行业模块, 常量名, 期望值)。任一被未来重写改回旧占位 → 本测试失败,强制复核。
CALIBRATED_CONSTS = [
    ("医药生物", "_HI_研发资本化比", 3.0),      # 原 1.0(命中48%泛滥)→ p80
    ("医药生物", "_HI_商誉占净资产", 0.10),      # 原 0.30(命中0%从不发声)→ p90
    ("基础化工", "_HI_在建占总资产", 0.12),      # 原 0.20 → p90
    ("机械设备", "_HI_应收占营收", 1.0),        # 原 0.4(命中62%泛滥)→ p75
    ("机械设备", "_HI_在建占总资产", 0.10),      # 原 0.15 → p90
    ("有色金属", "_HI_在建占总资产", 0.13),      # 原 0.20 → p90
    ("电力设备", "_HI_在建占总资产", 0.10),      # 原 0.20 → p90+
    ("食品饮料", "_LO_毛利率", 18.0),           # 原 25.0(命中40%泛滥,把大众品常态误当红旗)→ p22
]


@pytest.mark.parametrize("mod_name,const,expected", CALIBRATED_CONSTS)
def test_calibrated_constant_value(mod_name, const, expected):
    mod = importlib.import_module(f"tools.analysis.financial.industry.{mod_name}")
    assert getattr(mod, const) == pytest.approx(expected), (
        f"{mod_name}.{const} 被改动:期望标定值 {expected},实际 {getattr(mod, const)}。"
        f"若确需再标定,请同步更新 _calibration.py 与本断言。")


# ── ② 边界 hit/miss 语义锁(未被既有 per-industry 测试覆盖的 4 行业)────────────
def test_基础化工_扩产激进_boundary():
    m = importlib.import_module("tools.analysis.financial.industry.基础化工")
    # 在建/资产总计 = 0.15 > 新阈值 0.12 → 命中(旧阈值 0.20 会漏)
    hit = m.extra_flags({}, {"资产负债表": {"在建工程": 1.5e8, "资产总计": 1e9}})
    assert "扩产激进" in _codes(hit)
    # 0.08 < 0.12 → 不命中
    miss = m.extra_flags({}, {"资产负债表": {"在建工程": 8e7, "资产总计": 1e9}})
    assert "扩产激进" not in _codes(miss)


def test_有色金属_扩产激进_boundary():
    m = importlib.import_module("tools.analysis.financial.industry.有色金属")
    # 0.16 > 0.13 → 命中(旧阈值 0.20 会漏)
    hit = m.extra_flags({}, {"资产负债表": {"在建工程": 1.6e8, "资产总计": 1e9}})
    assert "扩产激进" in _codes(hit)
    miss = m.extra_flags({}, {"资产负债表": {"在建工程": 8e7, "资产总计": 1e9}})  # 0.08 < 0.13
    assert "扩产激进" not in _codes(miss)


def test_电力设备_扩产激进_boundary():
    m = importlib.import_module("tools.analysis.financial.industry.电力设备")
    # 0.12 > 0.10 → 命中(旧阈值 0.20 会漏)
    hit = m.extra_flags({}, {"资产负债表": {"在建工程": 1.2e8, "资产总计": 1e9}})
    assert "扩产激进" in _codes(hit)
    miss = m.extra_flags({}, {"资产负债表": {"在建工程": 6e7, "资产总计": 1e9}})  # 0.06 < 0.10
    assert "扩产激进" not in _codes(miss)


def test_食品饮料_毛利率_boundary():
    m = importlib.import_module("tools.analysis.financial.industry.食品饮料")
    # 毛利率 15 < 新阈值 18 → 命中(真·低毛利尾部)
    assert "品牌溢价流失" in _codes(m.extra_flags({"毛利率": 15.0}, {}))
    # 毛利率 22:旧阈值 25 会误报(大众品常态低毛利),新阈值 18 不报 → 锁住去泛滥语义
    assert "品牌溢价流失" not in _codes(m.extra_flags({"毛利率": 22.0}, {}))


# ── ③ 标定 harness 可用性 + 时序严格性 ─────────────────────────────────────
def test_calibration_harness_importable():
    """harness 应可导入且不进专家注册表(下划线前缀被 _discover 跳过)。"""
    from tools.analysis.financial.industry import _calibration as C
    from tools.analysis.financial.industry import EXPERTS
    assert "_calibration" not in EXPERTS
    assert hasattr(C, "build_panel") and hasattr(C, "THRESHOLD_REGISTRY")
    # 登记表覆盖 8 个样本充足行业
    inds = {r[0] for r in C.THRESHOLD_REGISTRY}
    assert {"电子", "基础化工", "医药生物", "食品饮料", "交通运输",
            "机械设备", "有色金属", "电力设备"} <= inds


def test_forward_outcome_no_lookahead():
    """前瞻结局只能用披露日**之后**的行情(未来函数红线):
    构造一段行情,断言 as_of 当日及之前的价格不进入前瞻窗口。"""
    import pandas as pd
    from tools.analysis.financial.industry import _calibration as C

    dates = pd.date_range("2020-01-01", periods=200, freq="D")
    # 披露日之前价格 =1000(极高),之后 =10(暴跌);若误用之前价当入场,收益≈0 不算暴雷
    closes = [1000.0] * 100 + [10.0] * 100
    df = pd.DataFrame({"date": dates, "close": closes})

    def fake_kline(code):
        return df

    C._kline.cache_clear()
    orig = C._kline
    try:
        C._kline = fake_kline
        out = C._forward_outcome("TEST", "2020-04-09", n=120)  # 第100天附近
        # 入场价应取披露日之后(=10),前瞻窗内平盘 → 收益≈0,不应把"之前的1000→之后10"记成收益
        assert out["fwd_ret"] is not None
        assert out["fwd_ret"] == pytest.approx(0.0, abs=1e-6)
    finally:
        C._kline = orig
