"""F2 单测:experts 适配器。锁语义:4+1 内置专家映射、缺数据弃权、通用三类适配、恒过契约。"""
import pandas as pd
import pytest

from tools.analysis import experts as ex
from tools.contracts.expert import validate_verdict

# 去环境依赖(hermetic):板块轮动/多因子/事件驱动会从 data/analysis 读盘,有缓存时不弃权,
# 使 test_missing_blocks_abstain 这类"缺数据即弃权"红线在有缓存环境误挂。统一让这三位在无
# 显式 record 数据时确定性走各自的弃权分支(见 tests/conftest.py::hermetic_experts)。
pytestmark = pytest.mark.usefixtures("hermetic_experts")


def _rec(**blocks) -> dict:
    base = {"meta": {"code": "000001", "name": "测试"}}
    base.update(blocks)
    return base


# ———————————— 内置专家:方向映射 ————————————
def test_trend_bull_bear_neutral():
    bull = ex.expert_技术趋势(_rec(signals={"trend": {"评级": "偏多", "得分": 60, "依据": ["a"]}}))
    assert bull.方向 == "看多" and bull.强度 > 0 and validate_verdict(bull) == []
    bear = ex.expert_技术趋势(_rec(signals={"trend": {"评级": "偏空", "得分": -60, "依据": []}}))
    assert bear.方向 == "看空" and bear.强度 < 0
    neu = ex.expert_技术趋势(_rec(signals={"trend": {"评级": "中性", "得分": 12, "依据": []}}))
    assert neu.方向 == "中性" and neu.强度 == 0.0        # 中性强度必须归 0(契约)


def test_ob_os_oversold_is_bullish_confidence_by_resonance():
    v = ex.expert_超买超卖(_rec(signals={"ob_os": {"verdict": "超卖", "resonance": 3}}))
    assert v.方向 == "看多" and v.强度 > 0
    v2 = ex.expert_超买超卖(_rec(signals={"ob_os": {"verdict": "超买", "resonance": 2}}))
    assert v2.方向 == "看空" and v2.置信度 < v.置信度   # 共振越多越自信


def test_reversal_bullish_only():
    v = ex.expert_拐点(_rec(signals={"reversal": {"拐点标签": "反弹启动", "拐点评分": 70}}))
    assert v.方向 == "看多" and v.强度 > 0
    n = ex.expert_拐点(_rec(signals={"reversal": {"拐点标签": "无", "拐点评分": 0}}))
    assert n.方向 == "中性" and n.强度 == 0.0


def test_fundflow_direction_and_streak():
    inflow = ex.expert_资金流(_rec(fundflow={"今日主力净流入": 1e8, "主力连续净流入天数": 3}))
    assert inflow.方向 == "看多" and inflow.强度 == 1.0
    outflow = ex.expert_资金流(_rec(fundflow={"今日主力净流入": -1e8, "主力连续净流入天数": 0}))
    assert outflow.方向 == "看空" and outflow.强度 < 0


def test_sentiment_thresholds_and_sample_confidence():
    bull = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.5, "样本数": 20}))
    assert bull.方向 == "看多" and bull.置信度 == 1.0
    weak = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.5, "样本数": 5}))
    assert weak.方向 == "看多" and weak.置信度 < 1.0 and weak.数据充分度 == "部分降级"
    mid = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.1, "样本数": 5}))
    assert mid.方向 == "中性" and mid.强度 == 0.0


# ———————————— 板块轮动:行业回退链(industry → industry_asof → board_of)————————————
def _fake_row(quad="领先", dirn="看多"):
    return {"方向": dirn, "强度": 0.7, "数据充分度": "充分", "象限": quad,
            "RS_Ratio": 102.5, "RS_Momentum": 101.5, "依据": [f"{quad}象限·RS"]}


def test_板块轮动_uses_meta_industry(monkeypatch):
    """meta.industry 有值 → 以它查 RRG,不弃权。"""
    from tools.analysis import rrg
    seen = {}
    def _row(name):
        seen["name"] = name
        return _fake_row()
    monkeypatch.setattr(rrg, "industry_row", _row)
    rec = {"meta": {"code": "000712", "industry": "J67资本市场服务"}}
    v = ex.expert_板块轮动(rec)
    assert seen["name"] == "J67资本市场服务"
    assert v.方向 == "看多" and v.数据充分度 == "充分" and v.置信度 > 0


def test_板块轮动_falls_back_to_industry_asof(monkeypatch):
    """meta.industry/sector 皆空时,回退读 meta.industry_asof(point-in-time),不弃权。"""
    from tools.analysis import rrg
    seen = {}
    def _row(name):
        seen["name"] = name
        return _fake_row()
    monkeypatch.setattr(rrg, "industry_row", _row)
    rec = {"meta": {"code": "000712", "industry": None, "industry_asof": "金融业"}}
    v = ex.expert_板块轮动(rec)
    assert seen["name"] == "金融业"                       # 用了 industry_asof 这一档
    assert v.数据充分度 != "缺失" and v.置信度 > 0        # 不再"无所属行业"弃权


def test_板块轮动_abstains_when_no_industry_anywhere(monkeypatch):
    """三档皆空(industry/asof/board_of 都无)→ 诚实弃权。"""
    from tools.analysis import rrg
    from tools.collectors import board
    monkeypatch.setattr(rrg, "industry_row", lambda name: _fake_row())
    monkeypatch.setattr(board, "board_of", lambda code: None)
    v = ex.expert_板块轮动({"meta": {"code": "000001"}})
    assert v.方向 == "中性" and v.置信度 == 0.0 and v.数据充分度 == "缺失"


# ———————————— 缺数据:弃权而非跳过 ————————————
def test_missing_blocks_abstain():
    for name in ex.BUILTIN:
        v = ex.build(name, _rec())                    # 空记录
        assert v.方向 == "中性" and v.强度 == 0.0
        assert v.置信度 == 0.0 and v.数据充分度 == "缺失"
        assert validate_verdict(v) == []


def test_sentiment_zero_sample_is_missing():
    v = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.9, "样本数": 0}))
    assert v.数据充分度 == "缺失" and v.置信度 == 0.0 and v.方向 == "中性"


# ———————————— build 恒过契约 ————————————
def test_build_always_valid_and_weight_from_config():
    from tools.config.strategy import THRESHOLDS
    w = THRESHOLDS["合议"]["默认权重"]
    v = ex.build("技术趋势", _rec(signals={"trend": {"评级": "偏多", "得分": 40, "依据": ["x"]}}))
    assert v.默认权重 == w["技术趋势"]
    assert validate_verdict(v) == []


def test_unknown_expert_abstains():
    v = ex.build("不存在的专家", _rec())
    assert v.数据充分度 == "缺失" and validate_verdict(v) == []


# ———————————— 通用三类适配 ————————————
def test_from_score_registry_example():
    # registry 内置"买卖倾向评分"读 prediction.买卖倾向.得分
    rec = _rec(prediction={"买卖倾向": {"结论": "偏买入", "得分": 4, "依据": ["超卖+2"]}})
    v = ex.from_score("买卖倾向评分", rec)
    assert v.方向 == "看多" and v.强度 > 0 and validate_verdict(v) == []


def test_from_signal_takes_latest_bar():
    kl = pd.DataFrame({"close": [10, 11, 12, 11, 10, 9, 10, 11, 12, 13,
                                 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24]})
    v = ex.from_signal("均线金叉", kl)
    assert v.方向 in ("看多", "看空", "中性") and validate_verdict(v) == []
    assert v.能力类型 == "信号"


def test_from_screen_selected_is_bullish_not_selected_neutral():
    from tools.strategy import registry

    if "临时选股X" not in registry.list_strategies("选股"):
        registry.register("临时选股X", "选股", lambda records: [c for c in records])
    rec = _rec()
    sel = ex.from_screen("临时选股X", {"000001": rec}, "000001")
    assert sel.方向 == "看多" and sel.能力类型 == "入选"
    non = ex.from_screen("临时选股X", {}, "000001")     # 未入选
    assert non.方向 == "中性" and non.强度 == 0.0        # 不反推看空
