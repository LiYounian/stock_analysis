"""午盘 Q · M1 · 大盘闸门契约测试。

覆盖:
    · 常量/状态映射
    · 路径推导
    · _extract_indicators 从 breadth 副本读 5 项
    · _classify 分档:未知/崩盘/强/弱/震荡
    · compute_gate 端到端(mock 副本读)
    · merge_two_stage 翻转规则
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.analysis.midday_q import gate as G


# ────────────────────────────── 常量/映射 ──────────────────────────────

def test_thresholds_present():
    for key in ("G1_hs300_intraday_pct", "G2_csi1000_intraday_pct",
                "G3_up_down_ratio", "G4_limit_up_down_ratio",
                "G5_net_breadth", "G6_northbound_net_yi"):
        assert key in G.THRESHOLDS
        assert "strong" in G.THRESHOLDS[key] and "weak" in G.THRESHOLDS[key]
        assert G.THRESHOLDS[key]["strong"] > G.THRESHOLDS[key]["weak"]


def test_state_hit_thresholds():
    assert G.THRESHOLDS["STATE_STRONG_HITS"] == 4
    assert G.THRESHOLDS["STATE_WEAK_HITS"] == 4


def test_state_allow_map_complete():
    for state in ("强势", "弱势", "震荡", "崩盘", "未知"):
        assert state in G.STATE_ALLOW
    assert G.STATE_ALLOW["崩盘"] == ([], 0.0)
    assert G.STATE_ALLOW["未知"] == ([], 0.0)


# ────────────────────────────── 路径 ──────────────────────────────

def test_gate_path_shape():
    p = G.gate_path("2026-09-07")
    assert p.name == "gate_2026-09-07.json"
    assert p.parent.name == "midday_q"


def test_breadth_path_uses_slot_suffix():
    p = G.breadth_path_for("2026-09-07", "1430")
    assert p.name == "2026-09-07_T1430.json"


def test_snapshot_path_delegated():
    from tools.pipeline.intraday_snapshot import snapshot_path
    assert G.snapshot_path_for("2026-09-07", "1430") == snapshot_path("2026-09-07", "1430")


# ────────────────────────────── _extract_indicators ──────────────────────────────

def _fake_breadth(*, hs300_pct=0.3, csi1000_pct=0.2,
                  up=3200, down=1500, limit_up=48, limit_down=6, net_breadth=0.35):
    """构造 breadth 副本样子的 dict(仅取本模块用到的字段)。"""
    return {
        "date": "2026-09-07",
        "slot": "1430",
        "up_count": up, "down_count": down,
        "limit_up_n": limit_up, "limit_down_n": limit_down,
        "net_breadth": net_breadth,
        "indices": {
            "000300": {"code": "000300", "pct_chg": hs300_pct},
            "000852": {"code": "000852", "pct_chg": csi1000_pct},
        },
    }


def test_extract_indicators_full():
    ind = G._extract_indicators(_fake_breadth())
    # G1/G2 单位归一(源方 %)→ 小数
    assert ind["hs300_intraday_pct"] == pytest.approx(0.003)
    assert ind["csi1000_intraday_pct"] == pytest.approx(0.002)
    # G3 = up/max(down,1)
    assert ind["up_down_ratio"] == pytest.approx(3200 / 1500)
    # G4 = limit_up/max(limit_down,1)
    assert ind["limit_up_down_ratio"] == pytest.approx(48 / 6)
    assert ind["net_breadth"] == 0.35
    # G6 M1 恒 None
    assert ind["northbound_net_yi"] is None


def test_extract_indicators_none_breadth():
    ind = G._extract_indicators(None)
    for k in ("hs300_intraday_pct", "csi1000_intraday_pct",
              "up_down_ratio", "limit_up_down_ratio", "net_breadth"):
        assert ind[k] is None


def test_extract_indicators_missing_index():
    """缺 000300 → G1=None,不影响其他指标。"""
    b = _fake_breadth()
    del b["indices"]["000300"]
    ind = G._extract_indicators(b)
    assert ind["hs300_intraday_pct"] is None
    assert ind["csi1000_intraday_pct"] is not None


def test_extract_indicators_zero_down_safe():
    """down_count=0 时不 DivisionError,用 max(down,1)。"""
    ind = G._extract_indicators(_fake_breadth(down=0))
    assert ind["up_down_ratio"] == pytest.approx(3200)


# ────────────────────────────── _classify ──────────────────────────────

def test_classify_missing_required_yields_unknown():
    """G1 缺失 → 未知,理由列出。"""
    ind = G.GateIndicators(
        hs300_intraday_pct=None,           # 缺
        csi1000_intraday_pct=0.006,
        up_down_ratio=2.0,
        limit_up_down_ratio=8.0,
        net_breadth=0.4,
        northbound_net_yi=None,
    )
    state, reasons = G._classify(ind)
    assert state == "未知"
    assert any("G1" in r for r in reasons)


def test_classify_strong_all_hit():
    """全部 5 项命中 strong → 强势。"""
    ind = G.GateIndicators(
        hs300_intraday_pct=0.008,          # >= 0.005
        csi1000_intraday_pct=0.006,
        up_down_ratio=2.0,                  # >= 1.5
        limit_up_down_ratio=10.0,           # >= 5
        net_breadth=0.4,                    # >= 0.2
        northbound_net_yi=None,
    )
    state, reasons = G._classify(ind)
    assert state == "强势"


def test_classify_weak_all_hit():
    """全部 5 项命中 weak → 弱势(且不进崩盘,因为 G4 未极低)。"""
    ind = G.GateIndicators(
        hs300_intraday_pct=-0.008,
        csi1000_intraday_pct=-0.006,
        up_down_ratio=0.5,
        limit_up_down_ratio=0.8,            # ≤ 1.0 弱,但 > 0.5(不进崩盘)
        net_breadth=-0.3,                   # ≤ -0.2 弱,但 > -0.5(不进崩盘)
        northbound_net_yi=None,
    )
    state, _ = G._classify(ind)
    assert state == "弱势"


def test_classify_crash_priority():
    """G4 极低 且 G5 极负 → 崩盘,优先于 5 项判定。"""
    ind = G.GateIndicators(
        hs300_intraday_pct=0.001,
        csi1000_intraday_pct=0.001,
        up_down_ratio=1.0,
        limit_up_down_ratio=0.3,            # ≤ 0.5
        net_breadth=-0.7,                    # ≤ -0.5
        northbound_net_yi=None,
    )
    state, _ = G._classify(ind)
    assert state == "崩盘"


def test_classify_mixed_is_neutral():
    """混合命中(强 3 弱 1 中 1)→ 震荡。"""
    ind = G.GateIndicators(
        hs300_intraday_pct=0.008,            # 强
        csi1000_intraday_pct=0.006,          # 强
        up_down_ratio=2.0,                    # 强
        limit_up_down_ratio=3.0,              # 中
        net_breadth=-0.3,                     # 弱
        northbound_net_yi=None,
    )
    state, _ = G._classify(ind)
    assert state == "震荡"


# ────────────────────────────── compute_gate 端到端 ──────────────────────────────

def test_compute_gate_rejects_bad_as_of():
    with pytest.raises(ValueError, match="14:30"):
        G.compute_gate("2026-09-07", "14:00")


def test_compute_gate_missing_breadth_yields_unknown(tmp_path, monkeypatch):
    """副本文件不存在 → state=未知,reasons 提示广度副本缺失。"""
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    result = G.compute_gate("2026-09-07", "14:30")
    assert result["state"] == "未知"
    assert result["allowed_strategies"] == []
    assert result["position_pct"] == 0.0
    assert any("副本缺失" in r for r in result["reasons"])


def test_compute_gate_reads_from_copy(tmp_path, monkeypatch):
    """副本文件在 → 正确读取并出强势。"""
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    breadth_dir = tmp_path / "data" / "breadth"
    breadth_dir.mkdir(parents=True)
    (breadth_dir / "2026-09-07_T1430.json").write_text(
        json.dumps(_fake_breadth(hs300_pct=0.8, csi1000_pct=0.7,
                                   up=3500, down=800,
                                   limit_up=60, limit_down=4, net_breadth=0.5)),
        encoding="utf-8",
    )
    result = G.compute_gate("2026-09-07", "14:30")
    assert result["state"] == "强势"
    assert "Q1" in result["allowed_strategies"] and "Q3" in result["allowed_strategies"]
    assert result["position_pct"] == 1.0


# ────────────────────────────── merge_two_stage ──────────────────────────────

def _stub_result(*, state, allowed=None, pos=0.5):
    return G.GateResult(
        date="2026-09-07", as_of="14:30", state=state,
        indicators=G.GateIndicators(),
        allowed_strategies=allowed if allowed is not None else list(G.STATE_ALLOW[state][0]),
        position_pct=pos, reasons=[],
    )


def test_merge_stage1_weak_forces_flat():
    """首判弱势 → final 空仓,不看 stage2(哪怕 stage2 转强)。"""
    s1 = _stub_result(state="弱势", allowed=[], pos=0.0)
    s2 = _stub_result(state="强势", allowed=["Q1"], pos=1.0)
    out = G.merge_two_stage(s1, s2)
    assert out["final"]["allowed_strategies"] == []
    assert out["final"]["position_pct"] == 0.0
    assert out["flipped"] is False


def test_merge_flip_to_flat():
    """首判震荡 + 复核崩盘 → 翻转空仓。"""
    s1 = _stub_result(state="震荡")
    s2 = _stub_result(state="崩盘", allowed=[], pos=0.0)
    out = G.merge_two_stage(s1, s2)
    assert out["final"]["allowed_strategies"] == []
    assert out["flipped"] is True


def test_merge_flip_to_weak():
    """首判强势 + 复核弱势 → 翻转空仓。"""
    s1 = _stub_result(state="强势")
    s2 = _stub_result(state="弱势", allowed=[], pos=0.0)
    out = G.merge_two_stage(s1, s2)
    assert out["final"]["allowed_strategies"] == []
    assert out["flipped"] is True


def test_merge_no_flip_takes_stage2():
    """首判震荡 + 复核震荡 → 取 stage2,flipped=False。"""
    s1 = _stub_result(state="震荡")
    s2 = _stub_result(state="震荡")
    out = G.merge_two_stage(s1, s2)
    assert out["flipped"] is False
    assert set(out["final"]["allowed_strategies"]) == {"Q1", "Q2", "Q3"}
    assert out["final"]["state"] == "震荡"


def test_merge_unknown_forces_flat():
    """任一 stage 未知 → final 空仓。"""
    s1 = _stub_result(state="未知", allowed=[], pos=0.0)
    s2 = _stub_result(state="强势")
    out = G.merge_two_stage(s1, s2)
    assert out["final"]["allowed_strategies"] == []


def test_merge_output_shape():
    """产出结构:date/stage1_1430/stage2_1450/final/flipped 五键。"""
    s1 = _stub_result(state="震荡")
    s2 = _stub_result(state="强势")
    out = G.merge_two_stage(s1, s2)
    for k in ("date", "stage1_1430", "stage2_1450", "final", "flipped"):
        assert k in out
    for k in ("allowed_strategies", "position_pct", "state", "note"):
        assert k in out["final"]
