"""WI-6 Phase 1-a · PEAD 消息面层回测的红线单测。

锁死语义(为什么改):
  · **防未来函数**:pead_dir 只能由 公告日 ≤ 信号日 的预告决定;公告日 > 信号日 的信息绝不泄漏。
  · **lookback 窗**:超出 lookback 自然日的陈旧预告不算活跃信号(PEAD 漂移是暂态)。
  · **as-of 取最近**:同票多条历史预告,取"公告日 ≤ T 里最近的一条"。
  · **方向分档**:正超预期 → +1,实质利空 → −1,不确定/未知 → 0。
  · **veto/tilt 增量归因干净**:同 N、同市场基准、同交易日,只差 PEAD 一层。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import pead_fusion_lab as pf


def _events(rows):
    df = pd.DataFrame(rows, columns=["code", "报告期", "公告日期", "预告类型"])
    df["公告日期"] = pd.to_datetime(df["公告日期"])
    df["方向"] = df["预告类型"].map(
        lambda t: 1 if t in pf.POS_TYPES else (-1 if t in pf.NEG_TYPES else 0))
    return df


def test_asof_no_lookahead():
    """公告日 > 信号日 的预告绝不能影响该行(红线)。"""
    ev = _events([("000001", "20240630", "2024-08-01", "预增")])   # 公告在信号日之后
    panel = pd.DataFrame({"date": ["2024-07-15"], "code": ["000001"], "fwd5": [1.0]})
    out = pf.attach_pead(panel, ev, lookback_days=90)
    assert out["pead_dir"].iloc[0] == 0, "公告日晚于信号日,不得泄漏未来信息"


def test_asof_within_lookback():
    """公告日 ≤ 信号日且在窗内 → 取其方向。"""
    ev = _events([("000001", "20240331", "2024-06-01", "预减")])
    panel = pd.DataFrame({"date": ["2024-07-15"], "code": ["000001"], "fwd5": [1.0]})
    out = pf.attach_pead(panel, ev, lookback_days=90)
    assert out["pead_dir"].iloc[0] == -1
    assert abs(out["pead_gap"].iloc[0] - 44) <= 1


def test_lookback_excludes_stale():
    """超出 lookback 的陈旧预告不算活跃信号。"""
    ev = _events([("000001", "20240331", "2024-01-01", "预增")])
    panel = pd.DataFrame({"date": ["2024-07-15"], "code": ["000001"], "fwd5": [1.0]})
    out = pf.attach_pead(panel, ev, lookback_days=90)
    assert out["pead_dir"].iloc[0] == 0, "196 天前的预告应超窗失效"


def test_asof_takes_most_recent():
    """同票多条 ≤T 预告,取最近一条(方向可反转)。"""
    ev = _events([("000001", "20231231", "2024-04-20", "预增"),
                  ("000001", "20240331", "2024-06-25", "预减")])
    panel = pd.DataFrame({"date": ["2024-07-01"], "code": ["000001"], "fwd5": [1.0]})
    out = pf.attach_pead(panel, ev, lookback_days=120)
    assert out["pead_dir"].iloc[0] == -1, "应取最近的 6-25 预减,而非更早的预增"


def test_direction_mapping():
    """正/负/未知分档正确。"""
    assert _events([("c", "p", "2024-01-01", "扭亏")])["方向"].iloc[0] == 1
    assert _events([("c", "p", "2024-01-01", "首亏")])["方向"].iloc[0] == -1
    assert _events([("c", "p", "2024-01-01", "不确定")])["方向"].iloc[0] == 0


def test_eval_layer_apples_to_apples():
    """veto/tilt 与纯技术同 N、同市场基准、同交易日;无负样本时三腿应重合(增量=0)。"""
    # 构造两天、每天 5 票、无任何负 PEAD → veto 不该动、tilt 不该动
    rows = []
    rng = np.random.default_rng(0)
    for dt in ("2024-01-02", "2024-01-03"):
        for i in range(5):
            rows.append({"date": dt, "code": f"{i:06d}", "fwd5": float(rng.normal()),
                         "_tech": float(rng.normal()), "pead_dir": 0})
    pdf = pd.DataFrame(rows)
    r = pf.eval_pead_layer(pdf, "_tech", N=3, tilt_w=1.0)
    assert r["增量_veto减技术"]["增量%"] in (0.0, None) or abs(r["增量_veto减技术"]["增量%"]) < 1e-9
    assert r["增量_tilt减技术"]["增量%"] in (0.0, None) or abs(r["增量_tilt减技术"]["增量%"]) < 1e-9


def test_veto_removes_negative():
    """有实质利空进了技术 Top-N 时,veto 必须把它挤掉。"""
    # 单日 4 票,技术分降序 = A>B>C>D;A 是负 PEAD。N=2。
    # 纯技术 Top2 = {A,B};veto Top2 = {B,C}(跳过 A)。收益设计成能区分。
    rows = [
        {"date": "2024-01-02", "code": "A", "fwd5": -5.0, "_tech": 4.0, "pead_dir": -1},
        {"date": "2024-01-02", "code": "B", "fwd5": 2.0, "_tech": 3.0, "pead_dir": 0},
        {"date": "2024-01-02", "code": "C", "fwd5": 3.0, "_tech": 2.0, "pead_dir": 0},
        {"date": "2024-01-02", "code": "D", "fwd5": 1.0, "_tech": 1.0, "pead_dir": 0},
    ]
    pdf = pd.DataFrame(rows)
    r = pf.eval_pead_layer(pdf, "_tech", N=2, tilt_w=1.0)
    # 纯技术均5日 = mean(-5,2) = -1.5;veto 均5日 = mean(2,3) = 2.5
    assert abs(r["纯技术"]["均5日%"] - (-1.5)) < 1e-6
    assert abs(r["veto断点"]["均5日%"] - 2.5) < 1e-6
    assert r["覆盖"]["neg_in_topN"] == 1
