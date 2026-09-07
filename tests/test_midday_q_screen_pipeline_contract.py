"""午盘 Q · M2 · 选股 pipeline 契约测试。

覆盖:
    · 非交易日 / 无效 stage
    · 上游 gate/snapshot 缺失
    · 三段累加(1430 → 1450 → final)
    · gate.final 空仓 → 强制清空 final
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.analysis.midday_q import gate as G
from tools.pipeline import midday_q_gate as PG
from tools.pipeline import midday_q_screen as PS


def _write_gate(tmp_path: Path, date: str, *,
                 stage1=None, stage2=None, final=None, flipped=False):
    """写一份合成 gate JSON 到 tmp_path/data/analysis/midday_q/gate_<date>.json。"""
    gate_dir = tmp_path / "data" / "analysis" / "midday_q"
    gate_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"date": date}
    if stage1: data["stage1_1430"] = stage1
    if stage2: data["stage2_1450"] = stage2
    if final:  data["final"] = final
    data["flipped"] = flipped
    (gate_dir / f"gate_{date}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_snapshot(tmp_path: Path, date: str, slot: str, codes: dict):
    """写一份合成 intraday snapshot。"""
    p = tmp_path / "data" / "intraday" / date
    p.mkdir(parents=True, exist_ok=True)
    (p / f"T{slot}.json").write_text(json.dumps({
        "date": date, "slot": slot, "codes": codes, "indices": {}, "errors": [],
    }, ensure_ascii=False), encoding="utf-8")


def _install_snapshot_reader(monkeypatch, tmp_path: Path):
    """intraday_snapshot.OUT_ROOT 是模块级静态求值,monkeypatch settings 不影响它。
    → 直接替换 _load_snapshot,读 tmp_path 下的相对路径。
    """
    def _load(date: str, slot: str):
        p = tmp_path / "data" / "intraday" / date / f"T{slot}.json"
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    monkeypatch.setattr(PS, "_load_snapshot", _load)


def _gate_stage(*, state="强势", allowed=("Q1",), pos=1.0):
    return {
        "date": "2026-09-07", "as_of": "14:30", "state": state,
        "indicators": {}, "allowed_strategies": list(allowed),
        "position_pct": pos, "reasons": [],
    }


# ────────────────────────────── 基础契约 ──────────────────────────────

def test_stages():
    assert PS._STAGES == ("1430", "1450", "final")


def test_screen_path_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    p = PS._screen_path("2026-09-07")
    assert p.name == "screen_2026-09-07.json"
    assert p.parent.name == "midday_q"


# ────────────────────────────── 非交易日 / 无效 stage ──────────────────────────────

def test_non_trading_day_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: False)
    assert PS.run("1430", date="2026-09-06") == 0
    assert not PS._screen_path("2026-09-06").exists()


def test_invalid_stage_returns_1():
    assert PS.run("bogus") == 1   # type: ignore[arg-type]


# ────────────────────────────── 上游缺失 ──────────────────────────────

def test_missing_gate_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    assert PS.run("1430", date="2026-09-07") == 1
    assert not PS._screen_path("2026-09-07").exists()


def test_missing_snapshot_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage())
    assert PS.run("1430", date="2026-09-07") == 1


# ────────────────────────────── gate 段不允许 ──────────────────────────────

def test_gate_disallowed_produces_empty(monkeypatch, tmp_path):
    """gate.stage1 allowed=[] → screen.stage1 空 selections。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="崩盘", allowed=(), pos=0.0))
    # 即便 snapshot 缺也不影响,因为 allowed 空直接短路
    assert PS.run("1430", date="2026-09-07") == 0
    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    assert data["stage1_1430"]["selections"] == {}
    assert data["stage1_1430"]["final_codes"] == []


# ────────────────────────────── 幂等 ──────────────────────────────

def test_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="崩盘", allowed=(), pos=0.0))
    PS.run("1430", date="2026-09-07")
    mtime1 = PS._screen_path("2026-09-07").stat().st_mtime
    PS.run("1430", date="2026-09-07")
    mtime2 = PS._screen_path("2026-09-07").stat().st_mtime
    assert mtime1 == mtime2


# ────────────────────────────── 三段完整流程 ──────────────────────────────

def test_full_pipeline_flipped_forces_empty_final(monkeypatch, tmp_path):
    """gate.final.allowed=[] + flipped=True → screen.final 强制空清单。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)

    _install_snapshot_reader(monkeypatch, tmp_path)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="震荡", allowed=("Q1", "Q3"), pos=0.5),
                stage2=_gate_stage(state="弱势", allowed=(), pos=0.0),
                final={"state": "弱势", "allowed_strategies": [], "position_pct": 0.0,
                        "note": "翻转 - 震荡 → 弱势,撤单"},
                flipped=True)
    # 放一只无法被任何策略入选的票(所有信号都缺 → screener 直接跳过)
    dummy_codes = {"600001": {"name": "A", "price": None, "open": None}}
    _write_snapshot(tmp_path, "2026-09-07", "1430", dummy_codes)
    _write_snapshot(tmp_path, "2026-09-07", "1450", dummy_codes)

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0
    assert PS.run("final", date="2026-09-07") == 0

    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    assert data["final"]["final_codes"] == []
    assert data["final"]["position_pct"] == 0.0
    assert data["flipped"] is True


def test_final_needs_gate_final_and_stage2(monkeypatch, tmp_path):
    """final 阶段缺前置 → exit 1。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage())
    assert PS.run("final", date="2026-09-07") == 1


def test_pre_screen_candidates_sort():
    """振幅×成交额 排序取前 top 名。"""
    quotes = {
        "A": {"amplitude": 5.0, "amount_wan": 20000.0},
        "B": {"amplitude": 8.0, "amount_wan": 50000.0},
        "C": {"amplitude": 2.0, "amount_wan": 10000.0},
        "D": {"amplitude": None, "amount_wan": 100000.0},
    }
    picked = PS._pre_screen_candidates(quotes, top=2)
    assert picked[:2] == ["B", "A"]
