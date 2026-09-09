"""每日选股结构化产物 Tool 单测：锁 schema 校验（枚举/必填/情绪闸门/防未来/skipped）
+ 客观字段回填正确 + 策略 join + PICKS 一致性 + md 渲染 + 原子落盘。

hermetic：record 用注入桩 loader、策略 view 用 tmp_path，不碰真实 data/analysis
（conftest 的 no_writes_into_tracked_analysis 护栏亦兜底）。
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from tools.analysis import picks_schema as ps
from tools.analysis import write_picks as wp

_NOW = _dt.datetime(2026, 9, 9, 20, 15, 3, tzinfo=_dt.timezone(_dt.timedelta(hours=8)))

# —— 注入桩：三只票的 record（name/industry/close/pct_chg 从这里回填）——
_FAKE_RECORDS = {
    "601061": {"meta": {"code": "601061", "name": "中信金属", "industry": "有色/金属贸易",
                        "as_of": "2026-09-09"},
               "snapshot": {"close": 12.18, "pct_chg": 4.91}},
    "600356": {"meta": {"code": "600356", "name": "恒丰纸业", "industry": None,
                        "as_of": "2026-09-09"},
               "snapshot": {"close": 8.30, "pct_chg": 1.10}},
}


def _loader(records=None):
    records = records if records is not None else _FAKE_RECORDS

    def _f(code, pick_date):
        return records.get(code)
    return _f


def _analysis_unit(code="601061", **over):
    u = {"code": code, "type": "买入候选", "buy_rank": 1,
         "stance": "买入", "stance_qualifier": "首选",
         "dir_1d": "偏多", "dir_1d_conf": "中", "dir_5d": "偏多", "dir_5d_conf": "中高",
         "sentiment_quality": "ok",
         "key_reason": "净利大增+低估+主力净流入", "key_risk": "铜价回调",
         "watch_points": ["明日主力是否维持净流入？"]}
    u.update(over)
    return u


def _build(analysis, tmp_path, **kw):
    kw.setdefault("record_loader", _loader())
    kw.setdefault("analysis_dir", tmp_path)
    kw.setdefault("predict_for", "2026-09-10")
    kw.setdefault("now", _NOW)
    return wp.build_picks_json("2026-09-09", analysis, **kw)


# ———————————————————— 正常路径：回填 + 校验通过 ————————————————————
def test_ok_backfills_objective_and_validates(tmp_path):
    doc, errors = _build([_analysis_unit()], tmp_path)
    assert errors == [], errors
    p = doc["picks"][0]
    # 客观字段由代码回填、Agent 不手打
    assert p["name"] == "中信金属"
    assert p["industry"] == "有色/金属贸易"
    assert p["close"] == 12.18 and p["pct_chg"] == 4.91
    assert doc["meta"]["status"] == "ok" and doc["meta"]["picks_count"] == 1
    assert doc["meta"]["predict_for"] == "2026-09-10"
    assert doc["schema_version"] == ps.SCHEMA_VERSION


def test_industry_falls_back_to_code_industry_map(tmp_path, monkeypatch):
    # record.meta.industry=None（600356）→ 回退 code_industry.json 映射
    monkeypatch.setattr(wp, "_load_code_industry", lambda: {"600356": "造纸"})
    doc, errors = _build([_analysis_unit(code="600356", stance="可参与",
                                         stance_qualifier="", buy_rank=1)], tmp_path)
    assert errors == [], errors
    assert doc["picks"][0]["industry"] == "造纸"


# ———————————————————— 必填回填缺失：防"远端只有代码" ————————————————————
def test_missing_record_leaves_required_fields_and_fails(tmp_path):
    # 注入 loader 对该 code 返回 None → name/close/pct_chg 缺 → 校验报错
    doc, errors = _build([_analysis_unit(code="000019", stance="观望",
                                         stance_qualifier="")],
                         tmp_path, record_loader=_loader({}))
    assert any("name" in e for e in errors)
    assert any("close" in e for e in errors)


# ———————————————————— 情绪质量闸门：盲区不得买入/首选 ————————————————————
@pytest.mark.parametrize("sq", ["unknown", "missing"])
def test_sentiment_gate_blocks_buy(tmp_path, sq):
    doc, errors = _build([_analysis_unit(sentiment_quality=sq)], tmp_path)
    assert any("不得给 stance=买入" in e for e in errors)
    assert any("不得含'首选'" in e for e in errors)


def test_sentiment_gate_allows_non_buy(tmp_path):
    doc, errors = _build([_analysis_unit(sentiment_quality="unknown",
                                         stance="观望", stance_qualifier="")], tmp_path)
    assert errors == [], errors


# ———————————————————— 枚举非法 ————————————————————
def test_illegal_enum_flagged(tmp_path):
    doc, errors = _build([_analysis_unit(stance="满仓干")], tmp_path)
    assert any("stance" in e for e in errors)


def test_no_direction_requires_dash_conf(tmp_path):
    doc, errors = _build([_analysis_unit(dir_1d="不给方向", dir_1d_conf="中")], tmp_path)
    assert any("不给方向" in e for e in errors)
    # 修正为 "-" 后通过
    doc2, e2 = _build([_analysis_unit(dir_1d="不给方向", dir_1d_conf="-")], tmp_path)
    assert e2 == [], e2


# ———————————————————— buy_rank 连续无重复 ————————————————————
def test_buy_rank_must_be_contiguous(tmp_path):
    a = [_analysis_unit(code="601061", buy_rank=1),
         _analysis_unit(code="600356", buy_rank=3, stance="可参与", stance_qualifier="")]
    doc, errors = _build(a, tmp_path)
    assert any("buy_rank" in e for e in errors)


# ———————————————————— 防未来 ————————————————————
def test_future_as_of_rejected(tmp_path):
    doc, errors = _build([_analysis_unit()], tmp_path, as_of="2026-09-10")
    assert any("防未来" in e for e in errors)


def test_record_as_of_later_than_pick_date_warns(tmp_path):
    recs = {"601061": {"meta": {"code": "601061", "name": "中信金属",
                                "industry": "有色", "as_of": "2026-09-11"},
                       "snapshot": {"close": 12.18, "pct_chg": 4.91}}}
    doc, errors = _build([_analysis_unit()], tmp_path, record_loader=_loader(recs))
    assert errors == [], errors  # 软告警不阻断
    assert any("疑似未来数据" in w for w in doc["meta"].get("_backfill_warnings", []))


# ———————————————————— skipped 态 ————————————————————
def test_skipped_status(tmp_path):
    doc, errors = _build([], tmp_path, status="skipped",
                         skip_reason="闭环未在门控窗口内完成")
    assert errors == [], errors
    assert doc["picks"] == [] and doc["meta"]["picks_count"] == 0


def test_skipped_requires_reason(tmp_path):
    doc, errors = _build([], tmp_path, status="skipped")
    assert any("skip_reason" in e for e in errors)


# ———————————————————— 策略 join：rank 取榜单位次、score 回填 ————————————————————
def test_strategy_join_from_view(tmp_path):
    day = tmp_path / "2026-09-09"
    day.mkdir(parents=True)
    (day / "策略0合议.json").write_text(json.dumps({
        "top": [{"code": "601825", "综合分": 0.6},
                {"code": "601061", "综合分": 0.7107}]}, ensure_ascii=False), encoding="utf-8")
    (day / "动量组合.json").write_text(json.dumps({
        "入选清单": [{"code": "601061", "特征": {"动量分": 210787.2}}]},
        ensure_ascii=False), encoding="utf-8")
    unit = _analysis_unit(strategies_hint=[{"name": "策略0合议", "rank": 99},
                                           {"name": "动量组合"}])
    doc, errors = _build([unit], tmp_path)
    assert errors == [], errors
    strat = {s["name"]: s for s in doc["picks"][0]["strategies"]}
    assert strat["策略0合议"]["rank"] == 2  # 榜单位次覆盖 hint 的 99
    assert strat["策略0合议"]["score"] == 0.7107
    assert strat["动量组合"]["score"] == 210787.2  # 嵌套 特征 里取分


def test_strategy_hint_rank_when_code_absent(tmp_path):
    unit = _analysis_unit(strategies_hint=[{"name": "最大范围选股", "rank": 171}])
    doc, errors = _build([unit], tmp_path)  # 无该 view 文件
    s = doc["picks"][0]["strategies"][0]
    assert s["rank"] == 171 and s["score"] is None


# ———————————————————— PICKS 锚点一致性 ————————————————————
def test_picks_anchor_consistency(tmp_path):
    doc, _ = _build([_analysis_unit()], tmp_path)
    assert wp.validate_picks(doc, picks_anchor="601061") == []
    bad = wp.validate_picks(doc, picks_anchor="601061,600356")
    assert any("PICKS 锚点" in e for e in bad)


# ———————————————————— md 渲染 ————————————————————
def test_render_md_tables(tmp_path):
    doc, _ = _build([_analysis_unit()], tmp_path)
    md = wp.render_picks_md_tables(doc)
    assert "<!-- PICKS: 601061 -->" in md
    assert "中信金属" in md and "有色/金属贸易" in md


def test_render_md_skipped(tmp_path):
    doc, _ = _build([], tmp_path, status="skipped", skip_reason="闭环超时")
    md = wp.render_picks_md_tables(doc)
    assert "<!-- PICKS: none -->" in md
    assert "闭环超时" in md


# ———————————————————— 原子落盘 round-trip ————————————————————
def test_write_picks_atomic_roundtrip(tmp_path):
    doc, errors = _build([_analysis_unit()], tmp_path)
    assert errors == []
    path = wp.write_picks(doc, tmp_path, "2026-09-09")
    assert path.endswith("2026-09-09/每日选股.json")
    reread = json.loads((tmp_path / "2026-09-09" / "每日选股.json").read_text(encoding="utf-8"))
    assert reread["picks"][0]["code"] == "601061"
    # collect_date 口径：非 6 位文件名 → 归入 views["每日选股"]（免改传输层的关键）
    assert not (tmp_path / "2026-09-09" / "每日选股.json").stem.isdigit()
