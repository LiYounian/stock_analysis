"""午盘 Q · web 层端到端测试(需 fastapi)。

覆盖:
    · /midday-q 路由渲染 200
    · 数据缺失时的降级路径
    · 数据齐全时清单/闸门/翻转标记的正确显示
    · 首页导航含"午盘 Q"入口
    · 综合选股页含全选按钮
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")   # 本机无 fastapi → 跳过
from fastapi.testclient import TestClient   # noqa: E402

from web.app import app   # noqa: E402


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def midday_q_dir(tmp_path, monkeypatch):
    """把 data/analysis/midday_q 指向 tmp_path,避免污染真实数据目录。"""
    from tools.config import settings
    from web import data_access as da
    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)
    # da 里可能 cache 了 settings,直接返回 tmp_path 下的目标目录路径
    root = tmp_path / "data" / "analysis" / "midday_q"
    root.mkdir(parents=True)
    return root


# ────────────────────────────── 路由基本可用性 ──────────────────────────────

def test_midday_q_route_ok_when_no_data(client):
    """无数据也应 200(降级页,不 500)。"""
    r = client.get("/midday-q")
    assert r.status_code == 200
    assert "午盘 Q" in r.text


def test_index_has_midday_q_nav(client):
    """首页导航含 "午盘 Q" 入口。"""
    r = client.get("/")
    assert r.status_code == 200
    assert "午盘 Q" in r.text
    assert "/midday-q" in r.text


def test_selection_has_toggle_all_button(client):
    """综合选股页含 "全选/全不选" 切换按钮。"""
    r = client.get("/selection")
    assert r.status_code == 200
    assert "combinedToggleAll" in r.text
    assert "全选 / 全不选" in r.text


# ────────────────────────────── data_access 层降级 ──────────────────────────────

def test_midday_q_page_missing_dir(monkeypatch, tmp_path):
    """data/analysis/midday_q/ 不存在 → available=False。"""
    from tools.config import settings
    from web import data_access as da
    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)
    result = da.midday_q_page("latest")
    assert result["available"] is False
    assert "尚未生成" in result["reason"]


def test_midday_q_page_no_screen_file(monkeypatch, tmp_path):
    """目录存在但无 screen_*.json → available=False。"""
    from tools.config import settings
    from web import data_access as da
    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)
    (tmp_path / "data" / "analysis" / "midday_q").mkdir(parents=True)
    result = da.midday_q_page("latest")
    assert result["available"] is False


def test_midday_q_page_broken_json(monkeypatch, tmp_path):
    """损坏 JSON → available=False + reason 提示读失败,不抛异常。"""
    from tools.config import settings
    from web import data_access as da
    monkeypatch.setattr(settings, "PROJECT_ROOT", tmp_path)
    root = tmp_path / "data" / "analysis" / "midday_q"
    root.mkdir(parents=True)
    (root / "screen_2026-09-08.json").write_text("{ 半个大括号")
    result = da.midday_q_page("2026-09-08")
    assert result["available"] is False
    assert "失败" in result["reason"] or "读" in result["reason"]


# ────────────────────────────── 有数据时的完整渲染 ──────────────────────────────

def _write_full_data(root: Path, date: str = "2026-09-08", *, flipped: bool = False):
    """在 root 下写完整的 gate + screen 数据。"""
    gate_final = ({"state": "弱势", "allowed_strategies": [], "position_pct": 0.0,
                    "note": "翻转 - 震荡 → 弱势,撤单"} if flipped else
                   {"state": "震荡", "allowed_strategies": ["Q1", "Q2", "Q3"],
                    "position_pct": 0.5, "note": "复核确认(震荡)"})
    (root / f"gate_{date}.json").write_text(json.dumps({
        "date": date,
        "stage1_1430": {"state": "震荡", "reasons": ["G1 沪深300=+0.10% → 中",
                                                       "G3 涨跌比=1.15 → 中"]},
        "stage2_1450": {"state": "弱势" if flipped else "震荡",
                         "reasons": ["G1 沪深300=-0.35% → 弱" if flipped else "G1=+0.15% → 中"]},
        "final": gate_final,
        "flipped": flipped,
    }, ensure_ascii=False), encoding="utf-8")

    if flipped:
        final_seg = {"gate_state": "弱势", "position_pct": 0.0,
                     "note": "翻转 · 撤单", "selections": {}, "final_codes": []}
    else:
        final_seg = {
            "gate_state": "震荡", "position_pct": 0.5, "note": "复核确认",
            "selections": {
                "Q1": [{"code": "300308", "name": "中际旭创",
                         "rank_score": 0.955, "from_strategy": "Q1",
                         "signals": {"IntradayReturn": 0.046, "AmPmRatio": 2.46}}],
                "Q2": [{"code": "688012", "name": "中微公司",
                         "rank_score": 0.534, "from_strategy": "Q2",
                         "signals": {"IntradayLow": -0.034, "Rebound": 0.026}}],
            },
            "final_codes": [
                {"code": "300308", "name": "中际旭创", "rank_score": 0.955,
                 "from_strategy": "Q1",
                 "signals": {"IntradayReturn": 0.046, "AmPmRatio": 2.46}},
                {"code": "688012", "name": "中微公司", "rank_score": 0.534,
                 "from_strategy": "Q2",
                 "signals": {"IntradayLow": -0.034, "Rebound": 0.026}},
            ],
        }
    (root / f"screen_{date}.json").write_text(json.dumps({
        "date": date, "flipped": flipped,
        "final": final_seg,
        "stage1_1430": {"gate_state": "震荡", "position_pct": 0.5,
                         "selections": {"Q1": [{"code": "300308", "name": "中际旭创",
                                                  "rank_score": 0.95,
                                                  "from_strategy": "Q1", "signals": {}}]},
                         "final_codes": [{"code": "300308", "name": "中际旭创",
                                          "rank_score": 0.95, "from_strategy": "Q1",
                                          "signals": {}}]},
        "stage2_1450": final_seg,
    }, ensure_ascii=False), encoding="utf-8")


def test_midday_q_renders_selections_with_data(client, midday_q_dir):
    _write_full_data(midday_q_dir)
    r = client.get("/midday-q?date=2026-09-08")
    assert r.status_code == 200
    # 关键内容都要出现
    assert "300308" in r.text
    assert "中际旭创" in r.text
    assert "688012" in r.text
    assert "中微公司" in r.text
    assert "Q1" in r.text
    assert "Q2" in r.text
    assert "震荡" in r.text
    assert "50%" in r.text
    # rank_score 格式化
    assert "0.955" in r.text
    # 跳详情页链接
    assert "/stock/300308" in r.text


def test_midday_q_shows_flipped_warning(client, midday_q_dir):
    """flipped=True → 显示翻转警示,清单为空。"""
    _write_full_data(midday_q_dir, flipped=True)
    r = client.get("/midday-q?date=2026-09-08")
    assert r.status_code == 200
    assert "翻转" in r.text
    assert "今日最终清单为空" in r.text or "无票命中" in r.text
