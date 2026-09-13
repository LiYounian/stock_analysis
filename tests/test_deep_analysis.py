"""headless 逐票研判生成器 + 双跑框架单测(架构③-P2)。

锁"为什么改"的语义(约法第 6 条):
  1. 经验检索:防未来选版本、按 `**#N` 精确切片、关键词召回 + 常驻纪律、不整包返回。
  2. 输入装配:防未来(record.as_of 泄漏标记 / news 晚于 pick_date 剔除)、salient 渲染。
  3. 生成器:枚举兜底 + 一致性修正(不给方向→conf'-'、情绪盲区不买入/首选)、
     桩 client 产出经 picks_schema.validate_picks + write_picks 组装必过。
  4. 双跑:逐字段一致率 / 分歧清单正确、前瞻收益方向命中口径正确。
全程注入桩 client,不联网。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.analysis import deep_analysis as da
from tools.analysis import deep_analysis_inputs as di
from tools.analysis import dualrun
from tools.analysis import experience_recall as er
from tools.analysis import picks_schema as ps
from tools.analysis import write_picks as wp


# ============================================================
# 1. 经验检索
# ============================================================
_EXP_MD = """# 经验沉淀 v2026-09-11

## 4. 经验条目（累积）

**#1 · 游资情绪连板票，不能用方向信号赌次日方向。**
- 为什么：连板惯性+游资护盘让单日方向失真。
- 怎么用：只推"高波动/派发风险→规避"，不给方向。

**#2 · 高位巨量 ≠ 看多信号；区分主力 vs 融资盘。**
- 为什么：融资盘买入被误当主力看多。
- 怎么用：拿到资金流入必辨来源。

**#3 · 分析必须数据×消息面结合。**
- 为什么：只用系统加工分出错。
- 怎么用：SOP 步骤2/3 强制。

**#4 · 系统 events/sentiment 是加工分，要存疑。**
- 为什么：残缺新闻误导。
- 怎么用：原始消息面对账。

**#17 · 航运板块运价是β主驱动。**
- 为什么：航运个股跟运价指数。
- 怎么用：航运票先看BDI/运价。

## 5. 已知陷阱

- 陷阱1：不该被切进条目。
"""


def _write_exp(base: Path, name: str, text: str):
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text(text, encoding="utf-8")


def test_parse_entries_only_section4():
    entries = er.parse_entries(_EXP_MD)
    ids = [e.id for e in entries]
    assert ids == [1, 2, 3, 4, 17]                 # §5 陷阱不被切进来
    assert "游资" in entries[0].title
    assert "为什么" in entries[0].body and "怎么用" in entries[0].body


def test_latest_version_anti_future(tmp_path: Path):
    base = tmp_path / "经验沉淀"
    _write_exp(base, "v2026-09-08.md", _EXP_MD)
    _write_exp(base, "v2026-09-11.md", _EXP_MD)
    _write_exp(base, "v2026-09-15.md", _EXP_MD)     # 晚于 pick_date,不得选
    fp = er.latest_version_file("2026-09-11", base)
    assert fp is not None and fp.name == "v2026-09-11.md"
    # pick_date 早于所有 → None
    assert er.latest_version_file("2026-09-01", base) is None


def test_recall_keyword_and_always_on(tmp_path: Path):
    base = tmp_path / "经验沉淀"
    _write_exp(base, "v2026-09-11.md", _EXP_MD)
    entries, ver = er.load_entries("2026-09-11", base)
    assert ver == "v2026-09-11.md"
    # 航运行业 → 召回 #17 + 常驻 #1~#4
    picked = er.recall(entries, industry="航运港口", qualitative="游资情绪连板", top_k=8)
    ids = {e.id for e in picked}
    assert {1, 2, 3, 4}.issubset(ids)              # 常驻纪律始终在
    assert 17 in ids                                # 行业关键词召回
    # 不整包:top_k 限制生效
    picked2 = er.recall(entries, industry="航运", top_k=5)
    assert len(picked2) <= 5


def test_recall_snippets_shape(tmp_path: Path):
    base = tmp_path / "经验沉淀"
    _write_exp(base, "v2026-09-11.md", _EXP_MD)
    snip, ver = er.recall_snippets_for("2026-09-11", industry="航运", base_dir=base, top_k=5)
    assert ver == "v2026-09-11.md"
    assert snip.startswith("- #")                  # 每条一行
    assert "#1" in snip


# ============================================================
# 2. 输入装配 + 防未来
# ============================================================
def _make_record(code: str, as_of: str, close=10.0, pct=1.0) -> dict:
    return {
        "schema_version": "1.0",
        "meta": {"code": code, "name": f"股{code}", "industry": "航运港口", "as_of": as_of},
        "snapshot": {"close": close, "pct_chg": pct, "bias20": 2.0, "vol_state": "平量"},
        "signals": {"trend": {"评级": "中"}, "reversal": {}, "ob_os": {"verdict": "中性"}},
        "prediction": {"支撑位": [9.5], "压力位": [11.0]},
        "financing": {"固定一问": {"有存续可转债": False, "有推进中定增": False, "有临近解禁_90日": False}},
        "council": {"default": {"综合方向": "偏多", "综合分": 0.6}},
    }


def _seed_day(root: Path, date: str, code: str, record: dict, news=None, sentiment=None,
              market_forecast=None):
    d = root / date
    (d / "news_ai").mkdir(parents=True, exist_ok=True)
    (d / "sentiment").mkdir(parents=True, exist_ok=True)
    (d / f"{code}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if news is not None:
        (d / "news_ai" / f"{code}.json").write_text(json.dumps(news, ensure_ascii=False), encoding="utf-8")
    if sentiment is not None:
        (d / "sentiment" / f"{code}.json").write_text(json.dumps(sentiment, ensure_ascii=False), encoding="utf-8")
    if market_forecast is not None:
        (d / "market_forecast.json").write_text(json.dumps(market_forecast, ensure_ascii=False), encoding="utf-8")


def test_assemble_drops_future_news(tmp_path: Path):
    root = tmp_path / "analysis"
    code, date = "601872", "2026-09-11"
    news = [
        {"title": "同日利好", "time": "2026-09-11 09:30", "source": "公告", "ai": {"影响方向": "利好"}},
        {"title": "次日新闻(未来)", "time": "2026-09-12 10:00", "source": "媒体"},
    ]
    _seed_day(root, date, code, _make_record(code, date), news=news)
    facts = di.assemble(code, date, root)
    assert len(facts.news) == 1                     # 未来那条被剔除
    assert any("剔除 1 条" in n for n in facts.notes)
    assert facts.future_leak is False


def test_assemble_flags_asof_leak(tmp_path: Path):
    root = tmp_path / "analysis"
    code, date = "601872", "2026-09-11"
    _seed_day(root, date, code, _make_record(code, "2026-09-14"))   # as_of 晚于 pick_date
    facts = di.assemble(code, date, root)
    assert facts.future_leak is True
    txt = di.render_facts_text(facts)
    assert "防未来告警" in txt


def test_render_facts_has_salient_blocks(tmp_path: Path):
    root = tmp_path / "analysis"
    code, date = "601872", "2026-09-11"
    _seed_day(root, date, code, _make_record(code, date))
    facts = di.assemble(code, date, root)
    txt = di.render_facts_text(facts)
    assert "技术快照" in txt and "供给面固定一问" in txt and "系统合议" in txt


# ============================================================
# 3. 生成器:枚举兜底 + 一致性 + 组装通过
# ============================================================
class _StubClient:
    """桩 client:按 code 返回预置研判(或统一模板),不联网。"""
    def __init__(self, by_code=None, default=None):
        self.by_code = by_code or {}
        self.default = default or {}

    def extract(self, text, schema, *, instruction, temperature=0.0):
        # 从 text 首行取 code(render_facts_text 第一行含"(code)")
        import re
        m = re.search(r"\((\d{6})\)", text.splitlines()[0])
        code = m.group(1) if m else ""
        return dict(self.by_code.get(code, self.default))


def test_coerce_no_direction_forces_dash():
    raw = {"type": "买入候选", "stance": "可参与", "dir_1d": "不给方向", "dir_1d_conf": "高",
           "dir_5d": "偏多", "dir_5d_conf": "-", "sentiment_quality": "ok",
           "key_reason": "x", "key_risk": "y", "watch_points": ["a？"]}
    unit, co = da._coerce_unit("601872", raw, sentiment_quality_hint="ok")
    assert unit["dir_1d_conf"] == "-"              # 不给方向 → conf '-'
    assert unit["dir_5d_conf"] != "-"              # 有方向但原'-' → 提保守
    assert any("不给方向" in c for c in co)


def test_coerce_sentiment_blind_blocks_buy():
    raw = {"type": "买入候选", "stance": "买入", "stance_qualifier": "首选",
           "dir_1d": "偏多", "dir_1d_conf": "中", "dir_5d": "偏多", "dir_5d_conf": "中",
           "sentiment_quality": "unknown", "key_reason": "x", "key_risk": "y", "watch_points": []}
    unit, co = da._coerce_unit("601872", raw, sentiment_quality_hint=None)
    assert unit["stance"] != "买入"                 # 盲区不得买入
    assert "首选" not in unit["stance_qualifier"]
    assert any("盲区" in c for c in co)


def test_coerce_out_of_enum_to_safe():
    raw = {"type": "乱填", "stance": "梭哈", "dir_1d": "涨", "dir_1d_conf": "极高",
           "dir_5d": "跌", "dir_5d_conf": "满", "sentiment_quality": "好", "watch_points": "单条"}
    unit, co = da._coerce_unit("601872", raw, sentiment_quality_hint="partial")
    assert unit["type"] in ps.TYPE and unit["stance"] in ps.STANCE
    assert unit["dir_1d"] in ps.DIRECTION and unit["dir_1d_conf"] in ps.CONF
    assert unit["sentiment_quality"] == "partial"  # 据 hint 兜底
    assert unit["watch_points"] == ["单条"]         # str → list


def test_generate_output_passes_validate_and_build(tmp_path: Path):
    root = tmp_path / "analysis"
    exp = tmp_path / "经验沉淀"
    _write_exp(exp, "v2026-09-11.md", _EXP_MD)
    date = "2026-09-11"
    codes = ["601872", "002913"]
    for c in codes:
        _seed_day(root, date, c, _make_record(c, date, close=10 + int(c[-1])))
    stub = _StubClient(default={
        "type": "买入候选", "stance": "可参与", "dir_1d": "偏多", "dir_1d_conf": "中",
        "dir_5d": "偏多", "dir_5d_conf": "中高", "sentiment_quality": "ok",
        "key_reason": "逻辑A", "key_risk": "风险B", "alpha_beta": "α/β", "watch_points": ["盯点？"]})
    results = da.generate(date, codes, client=stub, data_root=root, experience_base=exp)
    units = da.units_of(results)
    assert len(units) == 2
    # 组装(注入 record_loader 从 tmp root 读)+ 校验必过
    def _loader(code, pd):
        return di.load_record(code, pd, root)
    doc, errors = wp.build_picks_json(
        date, units, status="ok", analysis_dir=root, record_loader=_loader,
        predict_for="2026-09-12")
    # 客观回填的 name/close 到位、schema 校验通过
    assert errors == [], errors
    assert doc["picks"][0]["name"].startswith("股")


def test_generate_missing_record_marks_error(tmp_path: Path):
    root = tmp_path / "analysis"
    (root / "2026-09-11").mkdir(parents=True)
    stub = _StubClient(default={})
    results = da.generate("2026-09-11", ["999999"], client=stub, data_root=root,
                          experience_base=tmp_path / "none")
    assert results[0].error is not None
    assert da.units_of(results) == []              # error 票不进落盘集


# ============================================================
# 4. 双跑对比
# ============================================================
def test_diff_units_agreement_and_disagreements():
    a = {"code": "1", "type": "买入候选", "stance": "买入", "dir_1d": "偏多", "dir_1d_conf": "中",
         "dir_5d": "偏多", "dir_5d_conf": "中", "sentiment_quality": "ok",
         "key_reason": "主力连续净流入放量反包"}
    b = {"code": "1", "type": "买入候选", "stance": "可参与", "dir_1d": "偏多", "dir_1d_conf": "低",
         "dir_5d": "偏多", "dir_5d_conf": "中", "sentiment_quality": "ok",
         "key_reason": "主力净流入放量"}
    d = dualrun.diff_units(a, b)
    assert set(d["disagreements"]) == {"stance", "dir_1d_conf"}
    assert d["enum_agreement"] == round(5 / 7, 3)
    assert 0 < d["narrative_similarity"]["key_reason"] < 1   # 有重叠但不完全一致


def test_dir_hit_semantics():
    assert dualrun._dir_hit("偏多", 0.03) is True
    assert dualrun._dir_hit("偏多", -0.01) is False
    assert dualrun._dir_hit("偏空", -0.02) is True
    assert dualrun._dir_hit("不给方向", 0.05) is None       # 不给方向不计
    assert dualrun._dir_hit("中性", 0.05) is None
    assert dualrun._dir_hit("偏多", None) is None            # 数据缺不计


def test_forward_return_uses_next_day_close(tmp_path: Path, monkeypatch):
    root = tmp_path / "analysis"
    code = "601872"
    _seed_day(root, "2026-09-11", code, _make_record(code, "2026-09-11", close=10.0))
    _seed_day(root, "2026-09-14", code, _make_record(code, "2026-09-14", close=11.0))
    monkeypatch.setattr("tools.collectors.calendar.next_trading_day",
                        lambda d, allow_fetch=False: "2026-09-14")
    fwd = dualrun.forward_return(code, "2026-09-11", 1, root)
    assert fwd == pytest.approx(0.1)


def test_compare_with_injected_units(tmp_path: Path):
    """双跑对比可注入已跑 units(不触发 LLM),校验一致率聚合正确。"""
    codes = ["1", "2"]
    ua = [
        {"code": "1", "type": "买入候选", "stance": "买入", "dir_1d": "偏多", "dir_1d_conf": "中",
         "dir_5d": "偏多", "dir_5d_conf": "中", "sentiment_quality": "ok"},
        {"code": "2", "type": "规避", "stance": "规避", "dir_1d": "不给方向", "dir_1d_conf": "-",
         "dir_5d": "中性", "dir_5d_conf": "低", "sentiment_quality": "partial"},
    ]
    ub = [dict(ua[0]), dict(ua[1], stance="观望")]   # 仅票2的 stance 不同
    rep = dualrun.compare("2026-09-11", codes,
                          dualrun.Arm("A"), dualrun.Arm("B"),
                          data_root=tmp_path / "none", units_a=ua, units_b=ub)
    # 14 个枚举格,仅 1 个不同 → 13/14
    assert rep.overall_agreement == round(13 / 14, 3)
    assert rep.field_agreement["stance"] == 0.5
    assert rep.field_agreement["dir_1d"] == 1.0
    md = dualrun.render_report_md(rep)
    assert "双跑对比报告" in md and "总字段一致率" in md
