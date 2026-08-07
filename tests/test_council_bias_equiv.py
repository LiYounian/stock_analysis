"""F4 红线:买卖倾向等价迁移回归。

锁语义(D6=A):council.bias_council(经 predict.bias_recommendation_council)与旧
predict.bias_recommendation **逐票 100% 等价**——结论、得分、依据全等。

用 **exhaustive grid**(穷举全部输入组合)证明等价,强于"抽 N 个交易日":
  ob_os ∈ {超卖,超买,中性,缺失}
  reversal ∈ {反弹启动,超跌待反弹,无,缺失}
  signal ∈ {偏多,偏空,中性,缺失}
  fundflow ∈ {None, 净流入streak0, 净流入streak3, 净流出, 净流入为0}
  sentiment ∈ {None, 偏多有样本, 偏空有样本, 中性区间, 样本0(不计), 阈值边界±0.2}
"""
import itertools

import pytest

from tools.analysis import predict as pr
from tools.analysis import council


# ———————————— 输入枚举 ————————————
_OB = [{"结论": "超卖"}, {"结论": "超买"}, {"结论": "中性"}, {}]
_REV = [{"拐点标签": "反弹启动"}, {"拐点标签": "超跌待反弹"}, {"拐点标签": "无"}, {}]
_SIG = [{"评级": "偏多"}, {"评级": "偏空"}, {"评级": "中性"}, {}]
_FLOW = [
    None,
    {"今日主力净流入": 1e8, "主力连续净流入天数": 0},
    {"今日主力净流入": 1e8, "主力连续净流入天数": 3},
    {"今日主力净流入": -1e8, "主力连续净流入天数": 0},
    {"今日主力净流入": 0, "主力连续净流入天数": 0},
]
_SENT = [
    None,
    {"净情绪分": 0.5, "样本数": 8},      # 偏多
    {"净情绪分": -0.5, "样本数": 8},     # 偏空
    {"净情绪分": 0.1, "样本数": 8},      # 中性区间
    {"净情绪分": 0.9, "样本数": 0},      # 样本0 → 不计
    {"净情绪分": 0.2, "样本数": 3},      # 偏多阈值边界
    {"净情绪分": -0.2, "样本数": 3},     # 偏空阈值边界
]


def _grid():
    for ob, rev, sig, flow, sent in itertools.product(_OB, _REV, _SIG, _FLOW, _SENT):
        yield {"ob_os": ob, "reversal": rev, "signal": sig}, flow, sent


def test_exhaustive_equivalence_conclusion_score_reasons():
    """穷举全部组合:旧函数 vs 合议预设 —— 结论/得分/依据 完全相等。"""
    total = mismatch = 0
    first_bad = None
    for tech, flow, sent in _grid():
        total += 1
        old = pr.bias_recommendation(tech, flow, sent)
        new = pr.bias_recommendation_council(tech, flow, sent)
        if old != new:
            mismatch += 1
            first_bad = first_bad or (tech, flow, sent, old, new)
    assert mismatch == 0, f"{mismatch}/{total} 不等价,首个:{first_bad}"
    assert total == len(_OB) * len(_REV) * len(_SIG) * len(_FLOW) * len(_SENT)
    assert total >= 2000        # 覆盖规模远超"2 个交易日"


def test_direct_council_matches_predict_entry():
    """council.bias_council 与 predict 迁移入口一致(路径无偏差)。"""
    tech = {"ob_os": {"结论": "超卖"}, "reversal": {"拐点标签": "反弹启动"}, "signal": {"评级": "偏空"}}
    assert council.bias_council(tech, None, None) == pr.bias_recommendation_council(tech, None, None)


def test_known_buy_case():
    tech = {"ob_os": {"结论": "超卖"}, "reversal": {"拐点标签": "反弹启动"}, "signal": {"评级": "偏空"}}
    r = pr.bias_recommendation_council(tech, {"今日主力净流入": 1e8, "主力连续净流入天数": 3}, None)
    assert r["结论"] == "偏买入" and r["得分"] >= 2


def test_known_sell_case():
    tech = {"ob_os": {"结论": "超买"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏空"}}
    r = pr.bias_recommendation_council(tech, {"今日主力净流入": -1e8, "主力连续净流入天数": 0}, None)
    assert r["结论"] == "偏卖出" and r["得分"] <= -2


def test_sentiment_zero_sample_not_counted():
    tech = {"ob_os": {"结论": "中性"}, "reversal": {"拐点标签": "无"}, "signal": {"评级": "偏多"}}
    base = pr.bias_recommendation_council(tech, None, None)
    zero = pr.bias_recommendation_council(tech, None, {"净情绪分": 0.9, "样本数": 0})
    assert base == zero and not any("情绪" in r for r in zero["依据"])
