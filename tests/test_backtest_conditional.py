"""F6 条件化回测的指标语义单测(计划文档1 F6)。

锁死:Brier(条件化/无条件)、区间覆盖、ΔBrier=无条件−条件化、聚类t(按测试日)、
按regime/放宽层级/预注册主格分层、校准曲线分箱(up 为分数 0-1)。
无未来函数(od_N≤as_of)由 test_conditional_predict 的 _gather 前缀锁,此处不重复。
"""
import numpy as np
import pandas as pd

from tools.backtest import backtest_conditional as bc


def test_brier_and_dist():
    r = np.array([-2.0, 1.0, 3.0, -1.0])
    d = bc._dist(r, 7, 50, 93)
    assert d["up"] == 0.5 and d["n"] == 4
    assert bc._brier([0.5, 0.5], [1.0, 0.0]) == 0.25


def _eval_df():
    # 两测试日,N=1;up/base_up 为分数;actual/q_lo/q_hi 为百分比收益
    return pd.DataFrame([
        # day1: 条件化预测 0.6,基线 0.5,实际涨(hit=1)
        {"day": "d1", "regime": "牛", "N": 1, "cell": "多头排列×强×触上轨", "level": "精确",
         "fallback": False, "up": 0.6, "q_lo": -3.0, "q_hi": 4.0, "mean": 0.5,
         "base_up": 0.5, "base_q_lo": -4.0, "base_q_hi": 4.0, "actual": 2.0, "hit": 1.0},
        {"day": "d1", "regime": "牛", "N": 1, "cell": "纠缠×中×中性", "level": "精确",
         "fallback": False, "up": 0.4, "q_lo": -3.0, "q_hi": 3.0, "mean": -0.1,
         "base_up": 0.5, "base_q_lo": -4.0, "base_q_hi": 4.0, "actual": -1.0, "hit": 0.0},
        # day2
        {"day": "d2", "regime": "熊", "N": 1, "cell": "空头排列×弱×触下轨", "level": "放宽1",
         "fallback": False, "up": 0.55, "q_lo": -5.0, "q_hi": 5.0, "mean": 0.2,
         "base_up": 0.5, "base_q_lo": -4.0, "base_q_hi": 4.0, "actual": 6.0, "hit": 1.0},
        {"day": "d2", "regime": "熊", "N": 1, "cell": "纠缠×中×中性", "level": "退回",
         "fallback": True, "up": 0.5, "q_lo": -3.0, "q_hi": 3.0, "mean": 0.0,
         "base_up": 0.5, "base_q_lo": -4.0, "base_q_hi": 4.0, "actual": -2.0, "hit": 0.0},
    ])


def test_summarize_metrics():
    ev = _eval_df()
    res = bc.summarize(ev)
    blk = res["1日"]
    # Brier 条件化 = mean((up-hit)^2)
    up = np.array([0.6, 0.4, 0.55, 0.5]); hit = np.array([1, 0, 1, 0.0])
    assert abs(blk["Brier条件化"] - float(np.mean((up - hit) ** 2))) < 5e-4   # summarize 保留4位
    assert abs(blk["Brier无条件"] - 0.25) < 5e-4            # base_up 全 0.5
    assert abs(blk["ΔBrier(无条件−条件化)"] - (blk["Brier无条件"] - blk["Brier条件化"])) < 5e-4
    # 覆盖:actual 落在 [q_lo,q_hi];第3行 actual=6 超出 [-5,5] → 未覆盖 → 3/4
    assert blk["区间覆盖_条件化%"] == 75.0
    assert blk["退回率%"] == 25.0                            # 4 行 1 行退回


def test_summarize_stratify_keys():
    res = bc.summarize(_eval_df())["1日"]
    assert set(res["按regime"]) == {"牛", "熊"}
    assert "精确" in res["按放宽层级"] and "放宽1" in res["按放宽层级"] and "退回" in res["按放宽层级"]
    # 预注册主格必现(即便某格无样本也不报错;有样本的必在)
    assert "纠缠×中×中性" in res["预注册主格"]
    assert res["预注册主格"]["纠缠×中×中性"]["n"] == 2


def test_cluster_t_sign():
    """条件化每日都更好(ΔBrier>0)→ 聚类t 为正。"""
    ev = _eval_df()
    ct = bc._cluster_t(ev)
    assert ct["n_days"] == 2
    # day1: cond Brier=((.6-1)^2+(.4-0)^2)/2=0.16; base=0.25 → Δ=+0.09
    # day2: cond=((.55-1)^2+(.5-0)^2)/2=0.226; base=0.25 → Δ=+0.024 → 均值>0
    assert ct["ΔBrier均值"] > 0
