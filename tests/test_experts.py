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


def test_fundflow_direction_and_streak(monkeypatch):
    """资金流:净流入符号定方向、连续天数抬强度;**有融资盘证据时必须降级**。

    为什么拆成两段断言(锁"为什么改"):
      自「资金流融资盘甄别」接生产(config『资金流融资盘甄别』,提议见
      docs/每日分析/策略建议/资金流融资盘甄别.md §3.1)起,看多强度**不再只由**净流入
      符号 + 连续天数决定——还要看当日「融资买入额 / 主力净流入」能否解释掉大部分流入
      (能解释 → 更像杠杆资金追高而非主力吸筹 → 软降级)。
      旧写法只有"净流入 3 天 → 强度 1.0"一条,且**不隔离两融数据源**:本地采过两融的
      机器上 000001 恰好有数据并命中判据,断言就挂;换句话说这条断言从甄别上线那天起
      既保护不了原语义、又会随磁盘内容时红时绿。
    现在:hermetic_experts 把两融读取入口置空(见 tests/conftest.py)→ 第一段锁的是
      「**无**融资盘证据 → 顶格看多 1.0(甄别不误伤现状,不回归)」;第二段**显式注入**
      融资盘证据 → 锁「融资解释比 ≥ 阈值 → 必须降级 + 依据里带 ⚠告警 + 原始留审计字段」。
    只锁"降级发生"不锁具体系数:降级系数是 config 可调项,精确口径由
      tests/test_margin_divergence.py 单独锁,此处避免重复锁死数值。
    第二段同时兜住 kill-switch:若「资金流融资盘甄别.启用」被误关成 False,强度会退回
      1.0 → 本断言失败(这正是要防的静默失效)。
    """
    inflow = ex.expert_资金流(_rec(fundflow={"今日主力净流入": 1e8, "主力连续净流入天数": 3}))
    assert inflow.方向 == "看多" and inflow.强度 == 1.0     # 无融资盘证据 → 现状顶格
    assert "融资盘背离" not in inflow.原始
    outflow = ex.expert_资金流(_rec(fundflow={"今日主力净流入": -1e8, "主力连续净流入天数": 0}))
    assert outflow.方向 == "看空" and outflow.强度 < 0

    # —— 注入融资盘证据:融资买入 0.8 亿 / 主力净流入 1.0 亿 = 80% ≥ 融资解释比阈值(0.5)——
    from tools.analysis import margin_divergence as md
    monkeypatch.setattr(md, "load_margin_asof",
                        lambda code, as_of: {"融资买入额": 8e7, "融资余额": 5e9})
    hit = ex.expert_资金流(_rec(fundflow={"今日主力净流入": 1e8, "主力连续净流入天数": 3}))
    assert hit.方向 == "看多"                                # 默认「降级」模式:不翻方向
    assert 0.0 < hit.强度 < 1.0                              # 必须被降级(挤出顶格看多档)
    assert any("疑似融资盘" in d for d in hit.依据)          # 依据里必须带 ⚠告警(人读归因)
    assert hit.原始["融资盘背离"]["融资解释比"] == pytest.approx(0.8)   # 审计字段留痕


def test_sentiment_thresholds_and_sample_confidence():
    bull = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.5, "样本数": 20}))
    assert bull.方向 == "看多" and bull.置信度 == 1.0
    weak = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.5, "样本数": 5}))
    assert weak.方向 == "看多" and weak.置信度 < 1.0 and weak.数据充分度 == "部分降级"
    mid = ex.expert_情绪三层(_rec(sentiment={"净情绪分": 0.1, "样本数": 5}))
    assert mid.方向 == "中性" and mid.强度 == 0.0


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
