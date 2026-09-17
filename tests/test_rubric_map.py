"""文字档 rubric 整改单测:锁住「先定义再归类 + 代码回填」范式的四条语义。

用户原则(硬约束):大模型不擅长打数值分 → 提示词只让 LLM 输出**文字档**(每档有明确定义),
代码再把档回填成 标准描述 / 数值。本测试锁四点:
  ① 每个 rubric 档的**定义**都出现在对应提示词里(先定义再归类,防未来重写时把定义删掉);
  ② 文字档 → 标准描述 / 数值 映射正确(rubric_map);
  ③ 下游计算改读映射后的数值后仍跑通(event 聚合 / market_forecast 净利好度 / focus 净催化);
  ④ 提示词不再要求 LLM 给数值(SCHEMA 去数值口径)。
全离线,LLM 一律 monkeypatch,不触网。⚠️ 测试环境研究模拟,非投资建议。
"""
import pytest

from tools.llm import prompts
from tools.llm import rubric_map as rm


# ───────────────────────── ① 每档定义都在提示词里 ─────────────────────────
def test_news_extract_every_level_defined_in_prompt():
    instr = prompts.news_extract_instruction("比亚迪", "002594")
    # 方向 / 强度 / 关系 三个维度的每一档档名都必须出现(附带其定义文本)
    for levels in (rm.DIRECTION_LEVELS, rm.STRENGTH_LEVELS, rm.RELATION_LEVELS):
        for name, definition in levels.items():
            assert name in instr, f"档名缺失: {name}"
            assert definition[:12] in instr, f"定义缺失: {name}"


def test_policy_score_levels_in_prompt():
    instr = prompts.policy_score_instruction(["半导体"])
    for levels in (rm.DIRECTION_LEVELS, rm.STRENGTH_LEVELS):
        for name, definition in levels.items():
            assert name in instr and definition[:12] in instr


def test_ugc_stance_levels_in_prompt():
    instr = prompts.ugc_sentiment_instruction("比亚迪", "002594")
    for name, definition in rm.STANCE_LEVELS.items():
        assert name in instr and definition[:10] in instr


def test_financial_verdict_levels_in_prompt():
    instr = prompts.financial_verdict_instruction("某公司", "000001")
    for levels in (rm.FINANCIAL_RATING_LEVELS, rm.FINANCIAL_PROFIT_QUALITY_LEVELS,
                   rm.FINANCIAL_CONFIDENCE_LEVELS):
        for name, definition in levels.items():
            assert name in instr and definition[:10] in instr


# ───────────────────────── ④ SCHEMA 去数值口径 ─────────────────────────
def test_schema_drops_numeric_asks():
    # 影响强度改文字档:不再出现「1~5」数值口径,出现「强/中/弱」文字档
    assert "1~5" not in prompts.NEWS_EXTRACT_SCHEMA["影响强度"]
    assert "1~5" not in prompts.POLICY_SCORE_SCHEMA["影响强度"]
    for tok in ("强", "中", "弱"):
        assert tok in prompts.NEWS_EXTRACT_SCHEMA["影响强度"]
    # UGC 去掉「净情绪」数值字段,只留文字档「多空」
    assert "净情绪" not in prompts.UGC_SENTIMENT_SCHEMA
    assert "多空" in prompts.UGC_SENTIMENT_SCHEMA


# ───────────────────────── ② 文字档 → 描述 / 数值 映射正确 ─────────────────────────
def test_strength_to_num_text_and_legacy():
    assert rm.strength_to_num("强") == 5.0
    assert rm.strength_to_num("中") == 3.0
    assert rm.strength_to_num("弱") == 1.0
    # legacy 数值 / 数字串透传(向后兼容老缓存)
    assert rm.strength_to_num(4) == 4.0
    assert rm.strength_to_num("2") == 2.0
    # 缺失 / 未知 → default
    assert rm.strength_to_num(None, default=0.0) == 0.0
    assert rm.strength_to_num("???", default=1.0) == 1.0


def test_strength_desc_is_standard_text():
    assert rm.strength_desc("强") == rm.STRENGTH_LEVELS["强"]
    # legacy 数值也能映到最近档描述(口径统一)
    assert rm.strength_desc(5) == rm.STRENGTH_LEVELS["强"]
    assert rm.strength_desc(1) == rm.STRENGTH_LEVELS["弱"]


def test_direction_and_relation_maps():
    assert rm.direction_sign("利好") == 1
    assert rm.direction_sign("利空") == -1
    assert rm.direction_sign("中性") == 0
    assert rm.relation_weight("直接") == 1.0
    assert rm.relation_weight("间接") == 0.5
    assert rm.relation_weight("无关") == 0.0


def test_stance_to_net_text_and_legacy():
    assert rm.stance_to_net("强多") == 1.0
    assert rm.stance_to_net("偏多") == 0.5
    assert rm.stance_to_net("中性") == 0.0
    assert rm.stance_to_net("偏空") == -0.5
    assert rm.stance_to_net("强空") == -1.0
    # legacy 净情绪小数透传 + clamp
    assert rm.stance_to_net(0.3) == 0.3
    assert rm.stance_to_net(5.0) == 1.0


# ───────────────────────── ③ 下游用映射后数值仍跑通 ─────────────────────────
def test_event_aggregate_uses_text_strength():
    """event.aggregate_sentiment 吃文字档强度(强→5),与旧数值 5 等价。"""
    from tools.analysis import event as ev
    text = ev.aggregate_sentiment([{"影响方向": "利好", "影响强度": "强", "与本股关系": "直接"}])
    num = ev.aggregate_sentiment([{"影响方向": "利好", "影响强度": 5, "与本股关系": "直接"}])
    assert text["净情绪分"] == num["净情绪分"] == 1.0     # 5*1.0/5 = 1.0


def test_event_aggregate_mixed_legacy_and_text():
    """文字档与 legacy 数值混跑不崩,方向×强度×关系口径正确。"""
    from tools.analysis import event as ev
    s = ev.aggregate_sentiment([
        {"影响方向": "利好", "影响强度": "中", "与本股关系": "间接"},   # +3*0.5/5 = +0.3
        {"影响方向": "利空", "影响强度": 3, "与本股关系": "间接"},      # -3*0.5/5 = -0.3
    ])
    assert s["样本数"] == 2 and s["净情绪分"] == 0.0


def test_market_forecast_net_uses_text_strength():
    """market_forecast.sentiment 净利好度 = Σ(方向×强度),文字档回填后与旧数值等价。"""
    from tools.analysis.market_forecast import sentiment as se
    agg = se._agg_one([
        {"影响方向": "利好", "影响强度": "强", "受影响行业": ["半导体"]},   # +5
        {"影响方向": "利空", "影响强度": "弱", "受影响行业": ["银行"]},     # -1
    ])
    assert agg["se_net"] == 4.0 and agg["se_bull"] == 1 and agg["se_bear"] == 1


def test_focus_catalyst_uses_text_strength(monkeypatch, tmp_path):
    """focus.news_catalyst_by_sector 净催化 = Σ(方向×强度),文字档回填。"""
    import json
    from tools.analysis.sector_forecast import focus as fc
    from tools.analysis import industry_map
    p = tmp_path / "sentiment_policy.json"
    p.write_text(json.dumps([
        {"影响方向": "利好", "影响强度": "强", "industries": ["半导体"]},
        {"影响方向": "利空", "影响强度": "中", "industries": ["半导体"]},
    ], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fc, "resolve_analysis_file", lambda date, name: p, raising=False)
    # 让 resolve_analysis_file 在函数内 import 时也命中:直接 patch market_step
    from tools.analysis.sector_forecast import market_step
    monkeypatch.setattr(market_step, "resolve_analysis_file", lambda date, name: p)
    monkeypatch.setattr(industry_map, "to_sw", lambda x: "半导体")
    out = fc.news_catalyst_by_sector("2026-08-01")
    # 强(+5) + 中利空(-3) = +2
    assert out["半导体"]["净催化"] == 2.0
    assert out["半导体"]["利好"] == 1 and out["半导体"]["利空"] == 1


def test_ugc_sentiment_backfills_net_from_stance(monkeypatch, tmp_path):
    """event.ugc_sentiment:LLM 只给文字档「多空」,净情绪由代码回填(偏多→+0.5)。"""
    from tools.analysis import event as ev

    class _C:
        def extract(self, text, schema, *, instruction, temperature=0.0):
            return {"多空": "偏多", "依据": "多数看多"}

    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path / "c")
    r = ev.ugc_sentiment("000001", client=_C(), posts=[{"text": "冲"}])
    assert r["多空"] == "偏多" and r["净情绪"] == 0.5 and r["status"] == "ok"
