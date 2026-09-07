"""午盘 Q · M1 · 大盘闸门契约测试(骨架期)。

同 test_fundflow_intraday_contract 的思路:契约立即生效,行为断言待实现后放开 skip。
"""
from __future__ import annotations

import pytest

from tools.analysis.midday_q import gate as G


# ────────────────────────────── 常量/结构 ──────────────────────────────

def test_thresholds_present():
    """6 项指标阈值都存在,strong/weak 双向配对(G6 M1 跳过但阈值先在)。"""
    for key in ("G1_hs300_intraday_pct", "G2_csi1000_intraday_pct",
                "G3_up_down_ratio", "G4_limit_up_down_ratio",
                "G5_money_effect", "G6_northbound_net_yi"):
        assert key in G.THRESHOLDS, f"缺阈值:{key}"
        assert "strong" in G.THRESHOLDS[key] and "weak" in G.THRESHOLDS[key]
        assert G.THRESHOLDS[key]["strong"] > G.THRESHOLDS[key]["weak"], f"{key}:strong 应 > weak"


def test_state_hit_thresholds():
    """强/弱定档命中数 = 4/6(方案 00_大盘闸门.md)。"""
    assert G.THRESHOLDS["STATE_STRONG_HITS"] == 4
    assert G.THRESHOLDS["STATE_WEAK_HITS"] == 4


def test_state_allow_map_complete():
    """4 挡状态 + 未知,每挡映射到 (允许策略, 仓位) 二元组。"""
    for state in ("强势", "弱势", "震荡", "崩盘", "未知"):
        assert state in G.STATE_ALLOW, f"缺状态:{state}"
        allowed, pos = G.STATE_ALLOW[state]
        assert isinstance(allowed, list)
        assert 0.0 <= pos <= 1.0
    # 保守方向:崩盘/未知 → 空仓
    assert G.STATE_ALLOW["崩盘"] == ([], 0.0)
    assert G.STATE_ALLOW["未知"] == ([], 0.0)


# ────────────────────────────── 路径 ──────────────────────────────

def test_gate_path_shape():
    """产出路径 = data/analysis/midday_q/gate_<date>.json。"""
    p = G.gate_path("2026-09-07")
    assert p.name == "gate_2026-09-07.json"
    assert p.parent.name == "midday_q"


def test_breadth_path_uses_slot_suffix():
    """副本命名:<date>_T<slot>.json(与 breadth 原路径 <date>.json 区分)。"""
    p = G.breadth_path_for("2026-09-07", "1430")
    assert p.name == "2026-09-07_T1430.json"


def test_snapshot_path_delegated():
    """快照路径委托 intraday_snapshot.snapshot_path(单一真源)。"""
    from tools.pipeline.intraday_snapshot import snapshot_path
    assert G.snapshot_path_for("2026-09-07", "1430") == snapshot_path("2026-09-07", "1430")


# ────────────────────────────── 骨架期:NotImplementedError ──────────────────────────────

def test_compute_gate_is_stub():
    with pytest.raises(NotImplementedError):
        G.compute_gate("2026-09-07", "14:30")


def test_merge_two_stage_is_stub():
    stub = {"date": "2026-09-07", "as_of": "14:30", "state": "震荡",
            "indicators": {}, "allowed_strategies": [], "position_pct": 0.5, "reasons": []}
    with pytest.raises(NotImplementedError):
        G.merge_two_stage(stub, stub)


# ────────────────────────────── 契约细节:实现后要保留 ──────────────────────────────

@pytest.mark.skip(reason="M1 骨架期;实现后:任一必需指标缺失 → state='未知'")
def test_missing_required_indicator_yields_unknown():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:G6 M1 跳过不算必需(仅 5 项判定)")
def test_g6_optional_in_m1():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:strong 命中≥4 → 强势;weak≥4 → 弱势")
def test_state_boundary():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:merge 首判弱势/崩盘 → final 空仓,不看 stage2")
def test_merge_stage1_weak_forces_flat():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:stage1 强/震荡 + stage2 弱/崩 → flipped=True + 空仓")
def test_merge_flip_to_flat():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:stage2 保持/加强 → final 取 stage2,flipped=False")
def test_merge_no_flip():
    pass


@pytest.mark.skip(reason="M1 骨架期;实现后:任一 stage 为未知 → final 空仓")
def test_merge_unknown_forces_flat():
    pass
