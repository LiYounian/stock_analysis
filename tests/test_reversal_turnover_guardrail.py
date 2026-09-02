"""反转低换手组合(策略10)换手数据护栏单测。

需求源:docs/每日分析/策略建议/反转策略换手因子覆盖率退化.md
锁住"为什么这么设计"的语义,防未来重写误删规则:
  · 治本现算:近端缺 turnover/amount 时按 volume 现算兜底,恢复换手因子覆盖;
  · 自校验:比率不稳(除权/单位漂移)/ 成交量单位跳变 → 拒填(留 NaN 交护栏),绝不注入伪值;
  · 防未来函数:现算只用缺口之前的有效点,切片重算不受未来影响;
  · 覆盖率熔断:低覆盖 → present=False 本日不出 / ⚠ 警示;正常覆盖不误触发;
  · 极小样本 z-score 下限:有效样本 < 门槛 → 本日不出(防伪极值);
  · kill-switch:护栏.启用=False → 现算与熔断全 no-op(回到现状)。
"""
from __future__ import annotations

import math

from tools.strategy import reversal_turnover as rt

NAN = float("nan")

# 稳定配置(显式传入,锁语义不受默认调参影响)
CFG_ON = {
    "启用": True,
    "换手率现算兜底": True,
    "成交额现算兜底": True,
    "现算_参考窗口": 60,
    "现算_最少参考点": 20,
    "现算_比率变异上限": 0.15,
    "现算_成交量跳变上限": 5.0,
    "覆盖率_不出下限": 0.30,
    "覆盖率_警示下限": 0.50,
    "zscore_最小样本": 200,
}


def _finite(x):
    return isinstance(x, (int, float)) and math.isfinite(x)


# ————————————————————— 治本:现算兜底 —————————————————————
def test_derive_turnover_fills_recent_gap():
    """近端 turnover 整片 NaN 但有 volume → 按近端稳定比率现算,换手因子恢复可算。"""
    n = 40
    r = 2e-4                                        # turnover/volume 稳定比率
    volumes = [1_000_000 + (i % 5) * 1000 for i in range(n)]
    turnovers = [v * r for v in volumes]
    closes = [10.0 + 0.01 * i for i in range(n)]
    amounts = [v * c for v, c in zip(volumes, closes)]
    # 制造近端缺口:最后 13 根 turnover/amount 置 NaN(仿腾讯 volume-only 推进近端;
    # 20 窗口内有效点仅 7 < 门槛 10 → 修复前换手因子失效)
    gap = 13
    for i in range(n - gap, n):
        turnovers[i] = NAN
        amounts[i] = NAN
    assert rt.low_turnover_factor(turnovers, n=20) is None   # 修复前:近端有效点不足 → None

    dt, da, info = rt.derive_missing_turnover_amount(closes, volumes, turnovers, amounts, cfg=CFG_ON)
    assert info["turnover_derived"] == gap
    assert info["amount_derived"] == gap
    f = rt.low_turnover_factor(dt, n=20)
    assert f is not None and f < 0                           # 修复后:恢复可算(低换手取负均值)
    # 现算值贴近真实比率
    for i in range(n - gap, n):
        assert math.isclose(dt[i], volumes[i] * r, rel_tol=1e-6)
        assert math.isclose(da[i], volumes[i] * closes[i], rel_tol=1e-9)


def test_derive_refuses_when_ratio_unstable():
    """比率不稳(除权/口径漂移,MAD/median 超上限)→ 拒绝现算,留 NaN 交护栏。"""
    n = 40
    volumes = [1_000_000] * n
    # turnover 与 volume 比率剧烈跳动(cv 远超 0.15)
    turnovers = [(200.0 if i % 2 else 1.0) for i in range(n)]
    closes = [10.0] * n
    amounts = [v * c for v, c in zip(volumes, closes)]
    for i in range(n - 8, n):
        turnovers[i] = NAN
    dt, da, info = rt.derive_missing_turnover_amount(closes, volumes, turnovers, amounts, cfg=CFG_ON)
    assert info["turnover_derived"] == 0                     # 不稳 → 全拒填
    assert all(not _finite(dt[i]) for i in range(n - 8, n))


def test_derive_refuses_on_volume_unit_jump():
    """近端 volume 单位跳变(远超参考中位量×跳变上限)→ 该 bar 拒填,不注入量级失真伪值。"""
    n = 40
    r = 2e-4
    volumes = [1_000_000] * (n - 4) + [1_000_000 * 80] * 4   # 最后 4 根量放大 80×(单位跳变)
    turnovers = [v * r for v in volumes]
    closes = [10.0] * n
    amounts = [v * c for v, c in zip(volumes, closes)]
    for i in range(n - 4, n):
        turnovers[i] = NAN
    dt, da, info = rt.derive_missing_turnover_amount(closes, volumes, turnovers, amounts, cfg=CFG_ON)
    assert info["turnover_refused"] == 4                     # 越界 → 拒填
    assert info["turnover_derived"] == 0
    assert all(not _finite(dt[i]) for i in range(n - 4, n))


def test_derive_amount_equals_volume_times_close():
    """成交额现算 = volume × close(VWAP 近似)。"""
    n = 30
    volumes = [500_000] * n
    turnovers = [100.0] * n                                  # turnover 齐全,只测 amount
    closes = [12.5] * n
    amounts = [v * c for v, c in zip(volumes, closes)]
    for i in range(n - 5, n):
        amounts[i] = NAN
    dt, da, info = rt.derive_missing_turnover_amount(closes, volumes, turnovers, amounts, cfg=CFG_ON)
    assert info["amount_derived"] == 5
    for i in range(n - 5, n):
        assert math.isclose(da[i], 500_000 * 12.5, rel_tol=1e-9)


def test_derive_kill_switch_noop():
    """kill-switch:护栏.启用=False → 现算 no-op,序列原样返回。"""
    n = 40
    volumes = [1_000_000] * n
    turnovers = [200.0] * (n - 8) + [NAN] * 8
    closes = [10.0] * n
    amounts = [NAN] * n
    cfg_off = dict(CFG_ON, **{"启用": False})
    dt, da, info = rt.derive_missing_turnover_amount(closes, volumes, list(turnovers), list(amounts), cfg=cfg_off)
    assert info == {"turnover_derived": 0, "turnover_refused": 0, "amount_derived": 0}
    assert all(not _finite(dt[i]) for i in range(n - 8, n))


def test_derive_anti_future_function():
    """防未来函数:'截至 t' 现算只用缺口之前的有效点;追加未来 bar 再切回 [:t+1] 结果不变。"""
    n = 30
    r = 2e-4
    volumes = [1_000_000 + (i % 3) * 500 for i in range(n)]
    turnovers = [v * r for v in volumes]
    closes = [10.0] * n
    amounts = [v * c for v, c in zip(volumes, closes)]
    turnovers[-1] = NAN                                      # 当日 t 缺 turnover
    amounts[-1] = NAN
    dt_now, _, _ = rt.derive_missing_turnover_amount(closes, volumes, list(turnovers), list(amounts), cfg=CFG_ON)
    # 追加 3 根"未来" bar(值任意),再切回 [:t+1] 重算,当日现算值应完全一致
    fut_v = volumes + [9_999_999, 1, 5_000_000]
    fut_t = list(turnovers) + [123.0, 0.001, 50.0]
    fut_c = closes + [99.0, 0.5, 20.0]
    fut_a = amounts + [1.0, 2.0, 3.0]
    dt_fut, _, _ = rt.derive_missing_turnover_amount(
        fut_c[:n], fut_v[:n], fut_t[:n], fut_a[:n], cfg=CFG_ON)
    assert math.isclose(dt_now[-1], dt_fut[-1], rel_tol=1e-12)


# ————————————————————— 护栏:覆盖率熔断 —————————————————————
def test_gate_normal_coverage_not_tripped():
    """正常覆盖(≥警示下限)+ 足量样本 → present=True 正常,不误触发。"""
    g = rt.coverage_gate(0.88, 4567, cfg=CFG_ON)
    assert g["present"] is True and g["level"] == "正常" and g["熔断"] is False


def test_gate_warn_band():
    """覆盖率 ∈ [不出下限, 警示下限) → 仍出但打 ⚠,熔断标记 True。"""
    g = rt.coverage_gate(0.42, 3000, cfg=CFG_ON)
    assert g["present"] is True and g["level"] == "警示" and g["熔断"] is True
    assert "⚠" in g["note"]


def test_gate_blackout_low_coverage():
    """覆盖率 < 不出下限 → present=False 本日不出。"""
    g = rt.coverage_gate(0.002, 8, cfg=CFG_ON)
    assert g["present"] is False and g["level"] == "不出" and g["熔断"] is True


def test_gate_min_zscore_sample_floor():
    """有效样本 < zscore 最小样本 → 本日不出,即使覆盖率看似很高(极小样本 z 虚高)。"""
    g = rt.coverage_gate(0.99, 50, cfg=CFG_ON)
    assert g["present"] is False and g["level"] == "不出"


def test_gate_thresholds_configurable():
    """阈值可配:把不出下限调到 0.6,则 0.42 覆盖率从'警示'升级为'不出'。"""
    cfg = dict(CFG_ON, **{"覆盖率_不出下限": 0.60, "zscore_最小样本": 0})
    g = rt.coverage_gate(0.42, 3000, cfg=cfg)
    assert g["present"] is False and g["level"] == "不出"


def test_gate_kill_switch_noop():
    """kill-switch:护栏.启用=False → 恒 present=True 正常(现状,不熔断)。"""
    g = rt.coverage_gate(0.002, 6, cfg=dict(CFG_ON, **{"启用": False}))
    assert g["present"] is True and g["level"] == "正常" and g["熔断"] is False


def test_default_config_guard_enabled():
    """默认配置(单一真源 THRESHOLDS)护栏应处于启用态(接生产默认开)。"""
    cfg = rt.turnover_guard_cfg()
    assert cfg.get("启用") is True
    assert cfg.get("换手率现算兜底") is True
