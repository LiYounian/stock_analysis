"""方案B P2 接线契约测试：锁住"每日选股流程 → write_picks CLI → canonical JSON"这条线。

与 test_write_picks.py 的分工：那个测 build_picks_json/validate/render 等**函数级**契约；
本文件测 **CLI 入口 main()**（SKILL 实际调用的范式）+ 多票混合 buy_rank 的接线场景，
锁"接线不回归"：SKILL 里那条命令行的行为（正常落盘 / 校验不过非零退出 / 锚点不一致拦截 /
跳过态显式落盘）未来被重写时不能悄悄坏掉。

hermetic：record 用 monkeypatch 注入桩 loader、策略 view 用 tmp_path、落盘写 tmp_path，
不碰真实 data/analysis（conftest 的 no_writes_into_tracked_analysis 亦兜底）。
"""
from __future__ import annotations

import json

import pytest

from tools.analysis import write_picks as wp

# —— 注入桩：模拟 09-11 那批的客观字段来源（name/industry/close/pct_chg 从这里回填）——
_FAKE_RECORDS = {
    "601872": {"meta": {"code": "601872", "name": "招商轮船", "industry": None,
                        "as_of": "2026-09-11"},
               "snapshot": {"close": 20.0, "pct_chg": -2.82}},
    "002913": {"meta": {"code": "002913", "name": "奥士康", "industry": "电子",
                        "as_of": "2026-09-11"},
               "snapshot": {"close": 72.49, "pct_chg": 2.11}},
}


def _analysis_file(tmp_path, analysis) -> str:
    p = tmp_path / "picks_input.json"
    p.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _wire_env(monkeypatch, tmp_path):
    """把接线依赖的两个回填源都指向桩：record loader + 行业回退表。返回 analysis_dir。"""
    monkeypatch.setattr(wp, "_default_record_loader",
                        lambda code, pick_date: _FAKE_RECORDS.get(code))
    # 601872 record.industry=None → 必须能从 code_industry 回退到申万一级
    monkeypatch.setattr(wp, "_load_code_industry", lambda: {"601872": "交通运输"})
    return tmp_path


# —— 09-11 型的两票研判：1 买入候选(带 buy_rank) + 1 观望(不带 buy_rank) ——
def _mixed_analysis():
    return [
        {"code": "601872", "type": "买入候选", "buy_rank": 1,
         "stance": "可参与", "stance_qualifier": "条件式：放量站上20.98才入",
         "dir_1d": "低波待动", "dir_1d_conf": "中", "dir_5d": "偏多", "dir_5d_conf": "中高",
         "sentiment_quality": "ok", "key_reason": "油运超级周期+净利227%+便宜前瞻PE",
         "key_risk": "高获利盘兑现+地缘事件型β可反转",
         "watch_points": ["周一是否放量站上20.98？"],
         "strategies_hint": [{"name": "最强选股"}]},
        {"code": "002913", "type": "检验样本", "stance": "观望",
         "dir_1d": "不给方向", "dir_1d_conf": "-", "dir_5d": "不给方向", "dir_5d_conf": "-",
         "sentiment_quality": "partial", "key_reason": "PCB逆势但超买+融资盘",
         "key_risk": "超买共振3+净利−57.8%", "watch_points": ["是否放量续创新高？"]},
    ]


# ———————————— 接线主路径：CLI --dry-run 正常回填 + 校验通过（不落盘）————————————
def test_cli_dry_run_ok_backfills_and_validates(monkeypatch, tmp_path, capsys):
    analysis_dir = _wire_env(monkeypatch, tmp_path)
    af = _analysis_file(tmp_path, _mixed_analysis())
    rc = wp.main(["--date", "2026-09-11", "--analysis", af,
                  "--analysis-dir", str(analysis_dir),
                  "--predict-for", "2026-09-14",
                  "--picks-anchor", "601872,002913",
                  "--source-md", "docs/每日分析/选股/2026-09-11.md",
                  "--market-regime", "普跌重挫日、偏空非capitulation",
                  "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["meta"]["status"] == "ok" and doc["meta"]["picks_count"] == 2
    assert doc["meta"]["predict_for"] == "2026-09-14"
    by_code = {p["code"]: p for p in doc["picks"]}
    # 客观字段代码回填（Agent 不手打）；601872 行业走 code_industry 回退
    assert by_code["601872"]["name"] == "招商轮船"
    assert by_code["601872"]["industry"] == "交通运输"
    assert by_code["601872"]["close"] == 20.0 and by_code["601872"]["pct_chg"] == -2.82
    assert by_code["002913"]["name"] == "奥士康" and by_code["002913"]["industry"] == "电子"
    # 混合 buy_rank：仅买入候选带 rank，观望票不带（连续性只按给出的算）
    assert by_code["601872"]["buy_rank"] == 1
    assert "buy_rank" not in by_code["002913"]


# ———————————— 校验不过：客观字段回填失败必须非零退出、不落盘 ————————————
def test_cli_validation_failure_nonzero_and_no_write(monkeypatch, tmp_path, capsys):
    # loader 全返回 None → name/close/pct_chg 缺 → 校验不过
    monkeypatch.setattr(wp, "_default_record_loader", lambda code, pick_date: None)
    monkeypatch.setattr(wp, "_load_code_industry", lambda: {})
    af = _analysis_file(tmp_path, _mixed_analysis())
    rc = wp.main(["--date", "2026-09-11", "--analysis", af,
                  "--analysis-dir", str(tmp_path), "--predict-for", "2026-09-14"])
    assert rc == 2  # 非零退出
    err = capsys.readouterr().err
    assert "校验未通过" in err
    assert not (tmp_path / "2026-09-11" / "每日选股.json").exists()  # 带错不落盘


# ———————————— 锚点不一致拦截：md PICKS 与 picks 全集不符必须非零退出 ————————————
def test_cli_picks_anchor_mismatch_blocks(monkeypatch, tmp_path, capsys):
    analysis_dir = _wire_env(monkeypatch, tmp_path)
    af = _analysis_file(tmp_path, _mixed_analysis())
    rc = wp.main(["--date", "2026-09-11", "--analysis", af,
                  "--analysis-dir", str(analysis_dir), "--predict-for", "2026-09-14",
                  "--picks-anchor", "601872,999999",  # 与 picks(601872,002913) 不符
                  "--dry-run"])
    assert rc == 2
    assert "PICKS 锚点" in capsys.readouterr().err


# ———————————— 跳过态：CLI 显式落盘 canonical JSON（结果未出的一等公民占位）————————————
def test_cli_skipped_writes_placeholder(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wp, "_default_record_loader", lambda code, pick_date: None)
    rc = wp.main(["--date", "2026-09-11", "--status", "skipped",
                  "--skip-reason", "闭环未在门控窗口内完成",
                  "--analysis-dir", str(tmp_path), "--predict-for", "2026-09-14"])
    assert rc == 0
    written = tmp_path / "2026-09-11" / "每日选股.json"
    assert written.exists()  # 跳过态也要显式落盘、非留空
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc["meta"]["status"] == "skipped" and doc["picks"] == []
    assert doc["meta"]["skip_reason"] == "闭环未在门控窗口内完成"


# ———————————— 策略分值 join：命中榜单则回填 rank/score（md 表与 JSON 同源的底料）————————————
def test_cli_strategy_score_joined_into_json(monkeypatch, tmp_path, capsys):
    analysis_dir = _wire_env(monkeypatch, tmp_path)
    day = analysis_dir / "2026-09-11"
    day.mkdir(parents=True)
    (day / "最强选股.json").write_text(json.dumps({
        "入选清单": [{"code": "601872", "综合分": 0.908}]}, ensure_ascii=False),
        encoding="utf-8")
    af = _analysis_file(tmp_path, _mixed_analysis())
    rc = wp.main(["--date", "2026-09-11", "--analysis", af,
                  "--analysis-dir", str(analysis_dir), "--predict-for", "2026-09-14",
                  "--dry-run"])
    assert rc == 0
    doc = json.loads(capsys.readouterr().out)
    s = {p["code"]: p for p in doc["picks"]}["601872"]["strategies"][0]
    assert s["name"] == "最强选股" and s["rank"] == 1 and s["score"] == 0.908
