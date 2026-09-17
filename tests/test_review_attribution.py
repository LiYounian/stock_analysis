"""tools/review/attribution：规则归因分桶。每桶判据 + 优先级 + 规则判不了留 None（有声缺失）。"""
from __future__ import annotations

from tools.review.attribution import classify
from tools.review.types import ModelALabels, Pick


def _pick(**kw) -> Pick:
    base = dict(date="2026-09-16", code="000001", name="X")
    base.update(kw)
    return Pick(**base)


def _lab(r_exit=None, untriggered=False, runaway_up=False) -> ModelALabels:
    return ModelALabels(r_exit=r_exit, untriggered=untriggered, runaway_up=runaway_up,
                        filled=(False if untriggered else True))


STRONG_EVT = {"方向": "利好", "影响程度": "大", "来源": "一手", "执行度": "高", "可信度": "可信"}
WEAK_EVT = {"方向": "利好", "影响程度": "小", "来源": "二手", "执行度": "低", "可信度": "存疑"}


# ── 选对 ──
def test_correct_leader_catalyst():
    p = _pick(来源="板块催化", 角色="龙头", 板块消息面={"强弱": "强"})
    assert classify(p, _lab(r_exit=5.0)).correct_bucket == "板块-龙头催化"


def test_correct_follow():
    p = _pick(来源="板块催化", 角色="跟涨")
    assert classify(p, _lab(r_exit=2.0)).correct_bucket == "板块-联动跟涨"


def test_correct_strategy_and_report():
    assert classify(_pick(来源="策略直选", 角色="策略", 策略命中=["council合议"]),
                    _lab(r_exit=3.0)).correct_bucket == "策略"
    assert classify(_pick(来源="策略直选", 角色="策略", 策略命中=["财报专家"]),
                    _lab(r_exit=3.0)).correct_bucket == "财报"


def test_correct_form():
    p = _pick(来源="策略直选", 角色="策略", 策略命中=[], 形态={"均线多头": True})
    assert classify(p, _lab(r_exit=1.5)).correct_bucket == "形态"


# ── 选错 ──
def test_wrong_stepped_empty_runaway():
    """踏空：板块强信号 + 未触发 + 冲走。"""
    p = _pick(来源="板块催化", 角色="龙头", 板块消息面={"强弱": "强", "关键事件": [STRONG_EVT]})
    a = classify(p, _lab(untriggered=True, runaway_up=True))
    assert a.wrong_bucket == "踏空"


def test_untriggered_not_runaway_is_none():
    p = _pick(来源="板块催化", 角色="龙头", 板块消息面={"强弱": "弱"})
    a = classify(p, _lab(untriggered=True, runaway_up=False))
    assert a.wrong_bucket is None            # 未触发但非踏空 → 有声缺失


def test_wrong_fake_good_news():
    p = _pick(来源="板块催化", 角色="龙头", 板块消息面={"强弱": "中", "关键事件": [WEAK_EVT]},
              形态={"当日涨跌": 0.01})       # 未已动，避开追高
    assert classify(p, _lab(r_exit=-3.0)).wrong_bucket == "假利好"


def test_wrong_chase_high():
    p = _pick(来源="板块催化", 角色="龙头", 形态={"当日涨跌": 0.06})  # 已动 6%
    assert classify(p, _lab(r_exit=-2.0)).wrong_bucket == "追高"


def test_wrong_report_flag_missed():
    p = _pick(来源="策略直选", 角色="策略", 档="推荐",
              council={"财报红旗数": 2}, 形态={"当日涨跌": 0.0})
    assert classify(p, _lab(r_exit=-1.0)).wrong_bucket == "财报红旗漏判"


def test_wrong_board_regime_off():
    p = _pick(来源="策略直选", 角色="策略", 形态={"当日涨跌": 0.0},
              board_regime={"冷热标签": "过热"})
    assert classify(p, _lab(r_exit=-1.0)).wrong_bucket == "踏错板块基调"


def test_wrong_market_regime_off():
    p = _pick(来源="策略直选", 角色="策略", 形态={"当日涨跌": 0.0})
    a = classify(p, _lab(r_exit=-1.0), market_ctx={"宏观净方向": "看空"})
    assert a.wrong_bucket == "踏错大盘基调"


def test_priority_flag_over_chase():
    """财报红旗漏判 优先于 追高（同时命中时）。"""
    p = _pick(来源="策略直选", 角色="策略", 档="推荐",
              council={"财报红旗数": 1}, 形态={"当日涨跌": 0.07})
    assert classify(p, _lab(r_exit=-2.0)).wrong_bucket == "财报红旗漏判"


# ── 边界 ──
def test_pending_not_judged():
    p = _pick(来源="板块催化", 角色="龙头")
    a = classify(p, _lab(r_exit=None, untriggered=False))   # 已成交但收益未定
    assert a.correct_bucket is None and a.wrong_bucket is None


def test_unknown_correct_is_none():
    p = _pick(来源=None, 角色=None)
    assert classify(p, _lab(r_exit=5.0)).correct_bucket is None   # 有声缺失
