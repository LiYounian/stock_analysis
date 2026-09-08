"""午盘 Q 票池模块测试(纯读、无外网)。"""
from __future__ import annotations

import json

import pytest

from tools.config import midday_q_universe as U


def _mk_universe(tmp_path, payload=None):
    """把 payload 写到 tmp_path/config/midday_q_universe.json 并 monkeypatch _STORE。

    返回 monkeypatch 用的 Path。
    """
    p = tmp_path / "config" / "midday_q_universe.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if payload is None:
        payload = {
            "version": "1.0",
            "updated": "2026-09-08",
            "focus_codes": ["300308", "300502", "603893"],
            "codes": [
                {"code": "300308", "name": "中际旭创", "sw3": "通信网络设备及器件", "in_focus": True},
                {"code": "300502", "name": "新易盛", "sw3": "通信网络设备及器件", "in_focus": True},
                {"code": "603893", "name": "瑞芯微", "sw3": "数字芯片设计", "in_focus": True},
                {"code": "002049", "name": "紫光国微", "sw3": "数字芯片设计", "in_focus": False},
            ],
            "sw3_stats": {"通信网络设备及器件": 2, "数字芯片设计": 2},
        }
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def _install(monkeypatch, path):
    monkeypatch.setattr(U, "_STORE", path)
    U.reload()


# ────────────────────────────── 基础读取 ──────────────────────────────

def test_get_focus_codes(monkeypatch, tmp_path):
    p = _mk_universe(tmp_path)
    _install(monkeypatch, p)
    assert U.get_focus_codes() == ["300308", "300502", "603893"]


def test_get_full_codes(monkeypatch, tmp_path):
    p = _mk_universe(tmp_path)
    _install(monkeypatch, p)
    assert U.get_full_codes() == ["300308", "300502", "603893", "002049"]


def test_get_meta(monkeypatch, tmp_path):
    p = _mk_universe(tmp_path)
    _install(monkeypatch, p)
    meta = U.get_meta("300308")
    assert meta["name"] == "中际旭创"
    assert meta["sw3"] == "通信网络设备及器件"
    assert meta["in_focus"] is True
    assert U.get_meta("999999") is None


def test_by_sw3(monkeypatch, tmp_path):
    p = _mk_universe(tmp_path)
    _install(monkeypatch, p)
    g = U.by_sw3()
    assert set(g["通信网络设备及器件"]) == {"300308", "300502"}
    assert set(g["数字芯片设计"]) == {"603893", "002049"}


def test_stats(monkeypatch, tmp_path):
    p = _mk_universe(tmp_path)
    _install(monkeypatch, p)
    s = U.stats()
    assert s["version"] == "1.0"
    assert s["total"] == 4
    assert s["focus_n"] == 3
    assert s["sw3_stats"]["数字芯片设计"] == 2


# ────────────────────────────── 异常路径 ──────────────────────────────

def test_missing_file_raises(monkeypatch, tmp_path):
    """票池文件不存在 → 抛 FileNotFoundError(不静默默认)。"""
    monkeypatch.setattr(U, "_STORE", tmp_path / "does_not_exist.json")
    U.reload()
    with pytest.raises(FileNotFoundError, match="午盘 Q 票池"):
        U.get_focus_codes()


def test_reload_clears_cache(monkeypatch, tmp_path):
    """修改文件后 reload,读到新数据。"""
    p = _mk_universe(tmp_path, {"focus_codes": ["A"], "codes": []})
    _install(monkeypatch, p)
    assert U.get_focus_codes() == ["A"]

    # 改文件
    p.write_text(json.dumps({"focus_codes": ["B", "C"], "codes": []}), encoding="utf-8")
    # 不 reload,读的仍是旧
    assert U.get_focus_codes() == ["A"]
    # reload 后读新
    U.reload()
    assert U.get_focus_codes() == ["B", "C"]


# ────────────────────────────── 现网数据 sanity ──────────────────────────────

def test_real_universe_file_shape():
    """真实的 config/midday_q_universe.json(如果存在)结构合规。"""
    from tools.config import settings
    real = settings.PROJECT_ROOT / "config" / "midday_q_universe.json"
    if not real.exists():
        pytest.skip("config/midday_q_universe.json 不存在(尚未生成)")

    U.reload()   # 用现网数据
    focus = U.get_focus_codes()
    full = U.get_full_codes()
    # 每只 focus 都必须在主池里
    assert set(focus).issubset(set(full)), "focus_codes 出现了主池之外的代码"
    # focus 数应显著少于 full(否则精选池的意义就没了)
    assert len(focus) < len(full)
    # 生产票池至少 50 只
    assert len(focus) >= 50
