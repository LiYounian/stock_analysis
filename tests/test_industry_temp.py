"""IET 温度打分 + 因果分位 单测(锁语义,守则6)。

锁定:
  - test_gate_constants:闸门/切点命名常量从 THRESHOLDS 读取,值与探针规格 §7 一致。
  - test_ab_classify:估值/换手分位在切点两侧、RS-Momentum 在中枢两侧的 A/B 判定;缺失→None。
  - test_k_aggregate:3 维 A/B → k 计数;任一维弃权 → 行业弃权(k=None)。
  - test_rolling_pctile_causal:因果分位右移不变(无未来泄漏)+ min_periods 弃权。
这些断言锁住"为什么这么改"的语义,防未来重写无意改动规则。
"""
import numpy as np
import pandas as pd
import pytest

from tools.config.strategy import THRESHOLDS
from tools.analysis.industry_temp import temperature as T


# ————————————————————— 闸门常量 —————————————————————
def test_gate_constants():
    iet = THRESHOLDS["行业环境温度计"]
    # 预注册闸门(先锁后评):数值锁死,评测前不看结果再改
    assert iet["IET_IC_BAR"] == 0.03
    assert iet["IET_T_BAR"] == 2.0
    assert iet["IET_VAL_CUT"] == 0.5
    assert iet["IET_TURN_CUT"] == 0.5
    assert iet["IET_MOM_CENTER"] == 100.0
    assert iet["IET_PCTILE_WIN"] == 250
    assert iet["IET_MIN_MEMBERS"] == 5
    # 回退开关默认关(no-op),探针阶段合议零变化
    assert iet["IET_启用"] is False
    # 模块常量与 THRESHOLDS 同源
    assert T.VAL_CUT == iet["IET_VAL_CUT"]
    assert T.MOM_CENTER == iet["IET_MOM_CENTER"]


# ————————————————————— 单维 A/B —————————————————————
def test_ab_classify():
    # 估值:≥切点=A(贵/过热),<切点=B
    assert T.ab_valuation(0.5) == "A"      # 切点含于 A(≥)
    assert T.ab_valuation(0.8) == "A"
    assert T.ab_valuation(0.49) == "B"
    assert T.ab_valuation(0.0) == "B"
    # 换手:≥切点=A(拥挤)
    assert T.ab_turnover(0.6) == "A"
    assert T.ab_turnover(0.3) == "B"
    # 动量:≥中枢=A(过热/向上),<中枢=B
    assert T.ab_momentum(100.0) == "A"     # 中枢含于 A(≥)
    assert T.ab_momentum(101.5) == "A"
    assert T.ab_momentum(99.9) == "B"
    # 缺失 → None(弃权)
    assert T.ab_valuation(None) is None
    assert T.ab_turnover(np.nan) is None
    assert T.ab_momentum(None) is None
    # 自定义切点(敏感性网格)
    assert T.ab_valuation(0.65, cut=0.7) == "B"
    assert T.ab_valuation(0.72, cut=0.7) == "A"


# ————————————————————— 聚合 k —————————————————————
def test_k_aggregate():
    # 三维全 A → k=3 强A
    r = T.temperature_k("A", "A", "A")
    assert r["k"] == 3 and r["档"] == "强A" and r["弃权"] is False
    # 全 B → k=0 强B
    r = T.temperature_k("B", "B", "B")
    assert r["k"] == 0 and r["档"] == "强B"
    # 混合 → 计 A 数
    assert T.temperature_k("A", "B", "A")["k"] == 2   # 偏A
    assert T.temperature_k("B", "A", "B")["k"] == 1   # 偏B
    # 任一维缺失 → 整行业弃权
    r = T.temperature_k("A", None, "A")
    assert r["k"] is None and r["弃权"] is True
    assert T.temperature_k(None, None, None)["弃权"] is True
    # score_row 便捷入口一致
    r = T.score_row(rs_momentum=105.0, val_pctile=0.9, turn_pctile=0.1)
    assert r["维度"] == {"动量": "A", "估值": "A", "换手": "B"} and r["k"] == 2


# ————————————————————— 因果 rolling 分位 —————————————————————
def test_rolling_pctile_causal():
    s = pd.Series([10.0, 20.0, 15.0, 30.0, 5.0, 25.0])
    p_full = T.causal_rolling_pctile(s, win=100, min_periods=1)
    # 首点:窗口只有自身 → 分位=1.0(自身≤自身)
    assert p_full.iloc[0] == 1.0
    # 第2点 20 在 {10,20} 中 ≤20 占 2/2 = 1.0
    assert p_full.iloc[1] == 1.0
    # 第3点 15 在 {10,20,15} 中 ≤15 占 2/3
    assert p_full.iloc[2] == pytest.approx(2 / 3)
    # 因果性(右移不变):截断到前 k 个点,历史分位不变
    for k in range(1, len(s)):
        p_trunc = T.causal_rolling_pctile(s.iloc[:k], win=100, min_periods=1)
        assert np.allclose(p_trunc.to_numpy(), p_full.iloc[:k].to_numpy(), equal_nan=True)
    # min_periods:样本不足 → NaN 弃权
    p_min = T.causal_rolling_pctile(s, win=100, min_periods=4)
    assert np.isnan(p_min.iloc[0]) and np.isnan(p_min.iloc[2])
    assert not np.isnan(p_min.iloc[3])   # 第4点满 4 样本
    # 滚动窗只含 ≤date 的最近 win 个:窗口滑动丢弃更早点
    s2 = pd.Series([100.0, 1.0, 2.0, 3.0])   # 若 win=2,第4点窗口={2,3},3 分位=1.0(不受首点100影响)
    p2 = T.causal_rolling_pctile(s2, win=2, min_periods=1)
    assert p2.iloc[3] == 1.0


def test_pctile_nan_handling():
    # 含 NaN 的序列:NaN 点自身出 NaN,但不破坏其后有效点的窗口统计
    s = pd.Series([10.0, np.nan, 20.0, 15.0])
    p = T.causal_rolling_pctile(s, win=100, min_periods=1)
    assert np.isnan(p.iloc[1])
    # 第3点 20,窗口有效值{10,20} → 1.0
    assert p.iloc[2] == 1.0
    # 第4点 15,窗口有效值{10,20,15} → 2/3
    assert p.iloc[3] == pytest.approx(2 / 3)
