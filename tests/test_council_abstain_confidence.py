"""弃权置信度标注单测:锁死"弃权≠中性、少数发声→低置信+权重收缩、多专家一致不劣化、kill-switch no-op"。

诊断/口径源:docs/每日分析/策略建议/合议专家弃权置信度标注.md。
去环境依赖(hermetic):板块轮动/多因子/事件驱动无显式 record 数据时确定性弃权(见 conftest),
使断言只受构造的 record 字段驱动。
"""
import contextlib

import pytest

from tools.analysis import council
from tools.config.strategy import THRESHOLDS

_C = THRESHOLDS["合议"]
_A = _C["弃权置信度标注"]

pytestmark = pytest.mark.usefixtures("hermetic_experts")


@contextlib.contextmanager
def _cfg(**overrides):
    """临时覆盖「弃权置信度标注」配置键,退出还原(不污染其他测试)。"""
    old = {k: _A[k] for k in overrides if k in _A}
    missing = [k for k in overrides if k not in _A]
    _A.update(overrides)
    try:
        yield
    finally:
        _A.update(old)
        for k in missing:
            _A.pop(k, None)


def _rec(**blocks):
    base = {"meta": {"code": "000001", "name": "测试"}}
    base.update(blocks)
    return base


def _few_tech_rec():
    """301246 原型:仅 超买超卖(超卖·共振3)+ 拐点(超跌待反弹) 发声,其余弃权,口径全为技术。"""
    return _rec(signals={"ob_os": {"verdict": "超卖", "resonance": 3},
                         "reversal": {"拐点标签": "超跌待反弹", "拐点评分": 45}})


def _multi_caliber_bull_rec():
    """多口径一致看多:技术 + 资金 + 情绪 多个语义口径同时发声。"""
    return _rec(signals={"ob_os": {"verdict": "超卖", "resonance": 3},
                         "reversal": {"拐点标签": "反弹启动", "拐点评分": 80}},
                fundflow={"今日主力净流入": 1e8, "主力连续净流入天数": 3},
                sentiment={"净情绪分": 0.6, "样本数": 20})


# ————————————————————————————————————————————————
# 1. 弃权 ≠ 中性
# ————————————————————————————————————————————————
def test_abstain_distinct_from_data_backed_neutral():
    """有数据判中性(置信度>0、数据充分度非缺失)算「参与」;无数据弃权(缺失)算「弃权」——两者不混。"""
    # 情绪三层:净情绪 0(中性)但样本20 → 有数据判中性,置信度>0 → 参与、非弃权。
    rec = _rec(signals={"ob_os": {"verdict": "超卖", "resonance": 3}},
               sentiment={"净情绪分": 0.0, "样本数": 20})
    r = council.convene(["超买超卖", "情绪三层", "拐点"], rec)
    by = {a["专家"]: a for a in r["归因"]}
    assert by["情绪三层"]["方向"] == "中性"
    assert by["情绪三层"]["弃权"] is False and by["情绪三层"]["参与"] is True   # 有数据的中性 = 参与
    assert by["拐点"]["弃权"] is True and by["拐点"]["参与"] is False           # 无数据 = 弃权
    assert r["参与专家数"] == 2 and r["弃权专家数"] == 1                        # 超买超卖 + 情绪(中性但在场)


def test_weight_zero_expert_neither_participates_nor_abstains():
    """在场无权(技术趋势 权重0、有数据)既不参与(不进分母)也不算弃权(有数据)。"""
    rec = _rec(signals={"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]},
                        "ob_os": {"verdict": "超卖", "resonance": 3}})
    r = council.convene(["技术趋势", "超买超卖"], rec)
    by = {a["专家"]: a for a in r["归因"]}
    assert by["技术趋势"]["参与"] is False and by["技术趋势"]["弃权"] is False
    assert by["超买超卖"]["参与"] is True
    assert r["参与专家数"] == 1


# ————————————————————————————————————————————————
# 2. 少数发声 → 低置信 + 权重收缩
# ————————————————————————————————————————————————
def test_few_voices_low_confidence_and_shrunk():
    with _cfg(标注启用=True, 收缩启用=True, 收缩门槛=3, 收缩下限=0.4, 低置信阈值=0.6):
        r = council.convene_default(_few_tech_rec())
    assert r["参与专家数"] == 2 and r["口径多样性"] == 1 and r["覆盖口径"] == ["技术"]
    assert r["低合议置信度"] is True                     # 少数+单口径 → 标低
    assert 0.0 < r["收缩系数"] < 1.0                     # 发声<门槛 → 收缩
    assert abs(r["综合分_收缩"]) < abs(r["综合分"])       # 综合分向中性收缩
    # 原始综合分/方向不被收缩改写(前端重合成与既有排序默认口径不漂移)
    assert r["综合方向"] == "看多"


def test_single_voice_lower_confidence_than_two():
    """合议置信度随发声专家数单调:1 位发声 < 2 位发声。"""
    with _cfg(标注启用=True):
        one = council.convene_default(_rec(signals={"ob_os": {"verdict": "超卖", "resonance": 3}}))
        two = council.convene_default(_few_tech_rec())
    assert one["参与专家数"] == 1 and two["参与专家数"] == 2
    assert one["合议置信度"] < two["合议置信度"]


def test_multi_caliber_higher_confidence_than_single_caliber():
    """口径多样 → 合议置信度更高(纯技术单口径票被压低)。"""
    with _cfg(标注启用=True):
        single = council.convene_default(_few_tech_rec())         # 口径=技术
        multi = council.convene_default(_multi_caliber_bull_rec())  # 技术+资金+情绪
    assert multi["口径多样性"] > single["口径多样性"]
    assert multi["合议置信度"] > single["合议置信度"]


# ————————————————————————————————————————————————
# 3. 多专家一致不劣化
# ————————————————————————————————————————————————
def test_consensus_not_degraded():
    """发声专家 ≥ 收缩门槛 → 收缩系数=1、综合分_收缩==综合分、不标低置信(不劣化正常票)。"""
    with _cfg(标注启用=True, 收缩启用=True, 收缩门槛=3, 低置信阈值=0.6):
        r = council.convene_default(_multi_caliber_bull_rec())
    assert r["参与专家数"] >= 3
    assert r["收缩系数"] == 1.0
    assert r["综合分_收缩"] == r["综合分"]
    assert r["低合议置信度"] is False


# ————————————————————————————————————————————————
# 4. kill-switch
# ————————————————————————————————————————————————
def test_killswitch_labeling_off_is_noop():
    """标注启用=False → convene 逐字段回到原合议输出(不含任何新增标注键)。"""
    rec = _few_tech_rec()
    with _cfg(标注启用=True):
        on = council.convene_default(rec)
    with _cfg(标注启用=False):
        off = council.convene_default(rec)
    # 原字段不变
    assert off["综合分"] == on["综合分"] and off["综合方向"] == on["综合方向"]
    # 关掉后不出现任何标注/收缩键
    for k in ("参与专家数", "弃权专家数", "合议置信度", "收缩系数", "综合分_收缩", "低合议置信度"):
        assert k not in off
    # 归因也不带 参与/弃权 布尔
    assert all("参与" not in a and "弃权" not in a for a in off["归因"])


def test_killswitch_shrink_off_labels_but_no_score_change():
    """收缩启用=False → 只标注不动分:收缩系数=1、综合分_收缩==综合分(§5 观察期口径)。"""
    with _cfg(标注启用=True, 收缩启用=False):
        r = council.convene_default(_few_tech_rec())
    assert "合议置信度" in r                     # 标注仍在
    assert r["收缩系数"] == 1.0
    assert r["综合分_收缩"] == r["综合分"]        # 分不动


# ————————————————————————————————————————————————
# 5. 参与/弃权计数一致性
# ————————————————————————————————————————————————
def test_participation_counts_consistent():
    """参与 + 弃权 + 在场无权 == 专家总数;弃权数 == 数据充分度缺失数。"""
    with _cfg(标注启用=True):
        r = council.convene_default(_few_tech_rec())
    n_abstain = sum(1 for a in r["归因"] if a["数据充分度"] == "缺失")
    n_part = sum(1 for a in r["归因"] if a["参与"])
    assert r["弃权专家数"] == n_abstain
    assert r["参与专家数"] == n_part
    assert r["专家总数"] == len(r["归因"])
    assert r["参与专家数"] + r["弃权专家数"] <= r["专家总数"]   # 差额=在场无权(权重0/有数据)


# ————————————————————————————————————————————————
# 6. 同系统对账标记(内部分歧)
# ————————————————————————————————————————————————
def test_reconcile_divergence_and_agreement():
    assert council.reconcile_direction("看多", "偏卖出") == {
        "分歧": True, "程度": "相反", "council方向": "看多", "per_stock倾向": "偏卖出",
        "说明": "内部相反:全A合议看多 vs per-stock买卖倾向偏卖出"}
    # 301246 原型:合议看多 vs per-stock 观望 → 偏离(应命中)
    dev = council.reconcile_direction("看多", "观望")
    assert dev["分歧"] is True and dev["程度"] == "偏离"
    # 一致不误标
    assert council.reconcile_direction("看多", "偏买入")["分歧"] is False
    assert council.reconcile_direction("中性", "观望")["分歧"] is False


def test_reconcile_missing_data_not_flagged():
    for a, b in [("看多", None), (None, "观望"), ("看多", "未知标签")]:
        r = council.reconcile_direction(a, b)
        assert r["分歧"] is False and r["程度"] == "数据不足"
