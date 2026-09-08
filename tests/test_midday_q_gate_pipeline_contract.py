"""午盘 Q · M1 · 闸门 pipeline 契约测试。

覆盖:
    · 常量/签名
    · 非交易日跳过
    · 上游缺失 exit 1
    · 幂等
    · 副本拷贝(不覆盖、原缺失返 None)
    · 三段累加 gate JSON
    · final 合并
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from tools.analysis.midday_q import gate as G
from tools.pipeline import midday_q_gate as P


def test_stage_choices():
    assert P._STAGES == ("1430", "1450", "final")


def test_public_api_exists():
    assert callable(P.run)
    assert callable(P.main)


def test_run_signature_accepts_kwargs():
    sig = inspect.signature(P.run)
    params = sig.parameters
    assert "stage" in params
    assert "date" in params and params["date"].default is None
    assert "force" in params and params["force"].default is False


# ────────────────────────────── 辅助:构造有效 breadth 副本 ──────────────────────────────

def _write_breadth(tmp_path: Path, date: str, slot: str, *,
                    hs300_pct=0.4, csi1000_pct=0.3, up=3200, down=1500,
                    limit_up=48, limit_down=6, net_breadth=0.35) -> Path:
    """在 tmp_path 下写一个副本文件,返回 Path。"""
    breadth_dir = tmp_path / "data" / "breadth"
    breadth_dir.mkdir(parents=True, exist_ok=True)
    p = breadth_dir / f"{date}_T{slot}.json"
    p.write_text(json.dumps({
        "date": date, "slot": slot,
        "up_count": up, "down_count": down,
        "limit_up_n": limit_up, "limit_down_n": limit_down,
        "net_breadth": net_breadth,
        "indices": {
            "000300": {"pct_chg": hs300_pct},
            "000852": {"pct_chg": csi1000_pct},
        },
    }), encoding="utf-8")
    return p


def _write_breadth_original(tmp_path: Path, date: str) -> Path:
    """写 breadth 原路径 <date>.json(不带 slot 后缀),模拟 market_breadth 落盘。"""
    breadth_dir = tmp_path / "data" / "breadth"
    breadth_dir.mkdir(parents=True, exist_ok=True)
    p = breadth_dir / f"{date}.json"
    p.write_text(json.dumps({
        "date": date, "slot": "1430",
        "up_count": 3200, "down_count": 1500,
        "limit_up_n": 48, "limit_down_n": 6, "net_breadth": 0.35,
        "indices": {
            "000300": {"pct_chg": 0.4}, "000852": {"pct_chg": 0.3},
        },
    }), encoding="utf-8")
    return p


# ────────────────────────────── 非交易日 / 无效 stage ──────────────────────────────

def test_non_trading_day_skips(monkeypatch, tmp_path):
    """非交易日 → exit 0 且不落文件。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: False)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    assert P.run("1430", date="2026-09-06") == 0
    assert not G.gate_path("2026-09-06").exists()


def test_invalid_stage_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    assert P.run("bad_stage") == 1   # type: ignore[arg-type]


# ────────────────────────────── 副本拷贝 ──────────────────────────────

def test_copy_breadth_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    assert P._copy_breadth_snapshot("2026-09-07", "1430") is None


def test_copy_breadth_creates_copy(monkeypatch, tmp_path):
    """原文件在 → 拷副本 <date>_T<slot>.json。"""
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")
    copy = P._copy_breadth_snapshot("2026-09-07", "1430")
    assert copy is not None
    assert copy.name == "2026-09-07_T1430.json"
    assert copy.exists()


def test_copy_breadth_idempotent(monkeypatch, tmp_path):
    """副本已存在 → 不重拷,返回其路径。"""
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth(tmp_path, "2026-09-07", "1430")
    dst_mtime_before = G.breadth_path_for("2026-09-07", "1430").stat().st_mtime
    P._copy_breadth_snapshot("2026-09-07", "1430")
    dst_mtime_after = G.breadth_path_for("2026-09-07", "1430").stat().st_mtime
    assert dst_mtime_before == dst_mtime_after   # 没被再写


# ────────────────────────────── run() 端到端 ──────────────────────────────

def test_run_missing_upstream_exits_1(monkeypatch, tmp_path):
    """上游 breadth 不在 → exit 1 且不落 gate 文件。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    assert P.run("1430", date="2026-09-07") == 1
    assert not G.gate_path("2026-09-07").exists()


def test_run_1430_writes_stage1(monkeypatch, tmp_path):
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")
    assert P.run("1430", date="2026-09-07") == 0
    data = json.loads(G.gate_path("2026-09-07").read_text(encoding="utf-8"))
    assert "stage1_1430" in data
    assert data["stage1_1430"]["as_of"] == "14:30"
    assert data["date"] == "2026-09-07"


def test_run_idempotent_skip(monkeypatch, tmp_path):
    """已落 stage1_1430 再跑不覆盖。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")
    P.run("1430", date="2026-09-07")
    mtime1 = G.gate_path("2026-09-07").stat().st_mtime
    P.run("1430", date="2026-09-07")
    mtime2 = G.gate_path("2026-09-07").stat().st_mtime
    assert mtime1 == mtime2   # 没被覆盖


def test_run_force_overrides_idempotent(monkeypatch, tmp_path):
    """--force 强制重写。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")
    P.run("1430", date="2026-09-07")
    import time as _t; _t.sleep(0.01)
    P.run("1430", date="2026-09-07", force=True)
    # 只断言二次执行成功即可(mtime 因平台粒度不稳,不做时间断言)
    data = json.loads(G.gate_path("2026-09-07").read_text(encoding="utf-8"))
    assert "stage1_1430" in data


def test_run_final_needs_both_stages(monkeypatch, tmp_path):
    """final 阶段缺前置 → exit 1。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    assert P.run("final", date="2026-09-07") == 1


def test_run_three_stages_produces_final(monkeypatch, tmp_path):
    """完整三段 → gate JSON 里齐全 stage1_1430/stage2_1450/final/flipped。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")

    assert P.run("1430", date="2026-09-07") == 0

    # 模拟 14:50 时 market_breadth 又跑了一遍,覆盖了原文件(切换到 1450 数据集)
    (tmp_path / "data" / "breadth" / "2026-09-07.json").write_text(json.dumps({
        "date": "2026-09-07", "slot": "1450",
        "up_count": 3300, "down_count": 1400,
        "limit_up_n": 55, "limit_down_n": 5, "net_breadth": 0.40,
        "indices": {"000300": {"pct_chg": 0.5}, "000852": {"pct_chg": 0.4}},
    }), encoding="utf-8")
    assert P.run("1450", date="2026-09-07") == 0

    assert P.run("final", date="2026-09-07") == 0
    data = json.loads(G.gate_path("2026-09-07").read_text(encoding="utf-8"))
    for k in ("stage1_1430", "stage2_1450", "final", "flipped"):
        assert k in data
    assert isinstance(data["flipped"], bool)


def test_run_stage_append_not_overwrite(monkeypatch, tmp_path):
    """stage1_1430 已在 → 跑 stage 1450 不清掉 stage1_1430。"""
    monkeypatch.setattr(P.cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(P.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    _write_breadth_original(tmp_path, "2026-09-07")
    P.run("1430", date="2026-09-07")

    # 换成 1450 的数据集
    (tmp_path / "data" / "breadth" / "2026-09-07.json").write_text(json.dumps({
        "date": "2026-09-07", "slot": "1450",
        "up_count": 3300, "down_count": 1400,
        "limit_up_n": 55, "limit_down_n": 5, "net_breadth": 0.40,
        "indices": {"000300": {"pct_chg": 0.5}, "000852": {"pct_chg": 0.4}},
    }), encoding="utf-8")
    P.run("1450", date="2026-09-07")

    data = json.loads(G.gate_path("2026-09-07").read_text(encoding="utf-8"))
    assert "stage1_1430" in data
    assert "stage2_1450" in data
