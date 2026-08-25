"""forward_scorecard 激进版倾斜列 单测(可脱离行情/池验证的部分)。

锁死:
- _tilt_labels 在 池缺失/短kline 时优雅降级为 {}(不崩、该行留空)。
- summarize 的"激进版倾斜A/B":基线命中/倾斜命中/改判行/改判后命中 计算正确;
  dir=0(中性)或 None 不计命中;signal 非零计数正确。
(需行情+state_pool 的端到端跑在数据线环境验证——本 worktree 无行情。)
"""
import pandas as pd

from tools.backtest import forward_scorecard as fs


def test_tilt_labels_degrades_gracefully():
    assert fs._tilt_labels({"sentiment": {}}, pd.DataFrame({"close": range(50)}), "2026-08-24", None, (1, 5)) == {}
    assert fs._tilt_labels({"sentiment": {}}, pd.DataFrame({"close": range(5)}), "2026-08-24", object(), (1, 5)) == {}


def test_summarize_tilt_ab():
    """3 行样本:r1 无改判两命中;r2 倾斜把方向 1→-1 且改对;r3 中性/signal=0 不计。"""
    sc = pd.DataFrame([
        {"date": "2026-08-24", "code": "1", "hit_1": 1, "r_1": 1.0,
         "signal": 0.5, "p_cond_1": 60.0, "dir_cond_1": 1.0, "hit_cond_1": 1,
         "p_adj_1": 64.0, "dir_adj_1": 1.0, "hit_adj_1": 1},
        {"date": "2026-08-24", "code": "2", "hit_1": 0, "r_1": -2.0,
         "signal": -0.6, "p_cond_1": 59.0, "dir_cond_1": 1.0, "hit_cond_1": 0,
         "p_adj_1": 41.0, "dir_adj_1": -1.0, "hit_adj_1": 1},
        {"date": "2026-08-24", "code": "3", "hit_1": None, "r_1": 1.0,
         "signal": 0.0, "p_cond_1": 50.0, "dir_cond_1": 0.0, "hit_cond_1": None,
         "p_adj_1": 50.0, "dir_adj_1": 0.0, "hit_adj_1": None},
    ])
    summ = fs.summarize(sc, horizons=(1,))
    ab = summ["激进版倾斜A/B"]
    assert ab["根源信号非零行"] == 2                 # 0.5, -0.6 非零;0 不算
    a1 = ab["1日"]
    assert a1["基线命中率%"] == 50.0                 # hit_cond: [1,0] → 50%
    assert a1["倾斜命中率%"] == 100.0                # hit_adj: [1,1] → 100%
    assert a1["倾斜改判行"] == 1                     # 仅 code2 方向 1→-1 改判
    assert a1["改判后命中率%"] == 100.0             # 改判行 code2 倾斜后命中


def test_summarize_no_tilt_columns_ok():
    """无 signal 列(tilt=False 产出)时,summarize 不加 A/B、不报错。"""
    sc = pd.DataFrame([{"date": "d", "code": "1", "hit_1": 1, "r_1": 1.0}])
    summ = fs.summarize(sc, horizons=(1,))
    assert "激进版倾斜A/B" not in summ
    assert summ["1日"]["方向命中率%"] == 100.0
