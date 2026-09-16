"""H1/H2 回测 harness 单测:锁向量化动量等价性 + 组合模拟器口径不破。

⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.backtest.reselection import data as D
from tools.backtest.reselection import rank as R
from tools.backtest.reselection import portfolio as P
from tools.strategy.momentum import weighted_log_momentum


def test_momentum_vectorized_equiv():
    """向量化 momentum_score_series 与逐点 weighted_log_momentum 等价(锁复刻正确性)。"""
    rng = np.random.default_rng(42)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.001, 0.02, 300)))
    s = D.momentum_score_series(close, lookback=25)
    # 抽若干下标逐点比对
    for t in [25, 26, 50, 100, 200, 299]:
        ref = weighted_log_momentum(pd.DataFrame({"close": close[: t + 1]}), lookback_days=25)
        assert np.isclose(s[t], ref["score"], rtol=1e-9, atol=1e-12), (t, s[t], ref["score"])
    # 不足窗口 → NaN
    assert np.isnan(s[:25]).all()


def _mk_feat(dates, o, h, lo, c):
    dates = np.array(dates)
    return {"dates": dates, "o": np.array(o, float), "h": np.array(h, float),
            "lo": np.array(lo, float), "c": np.array(c, float),
            "didx": {d: i for i, d in enumerate(dates)}}


def test_baseline_trade_return_matches_fill():
    """baseline 单票单笔:入场 limit_pc_0.0(挂昨收)成交、D+2 收盘卖出,net 应等于手算。"""
    dates = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
    # 决策日 D=01-02(close=10);D+1=01-03 low=9.5≤10→成交@10;D+2=01-06 close=11
    feat = _mk_feat(dates, o=[10, 10.2, 10.8, 11.2], h=[10, 10.5, 11.1, 11.5],
                    lo=[10, 9.5, 10.5, 10.9], c=[10, 10.3, 11.0, 11.1])
    feats = {"000001": feat}
    market = {"mkt_ir": {d: 0.0 for d in dates}}
    ranks = {"2020-01-02": ["000001"], "2020-01-03": [], "2020-01-06": []}
    res = P.simulate(feats, ranks, market, arm="baseline", N=1, topn=1,
                     entry_rule="limit_pc_0.0", cost_bps=10.0)
    assert len(res.trades) == 1
    tr = res.trades.iloc[0]
    assert tr["entry_date"] == "2020-01-03" and tr["exit_date"] == "2020-01-06"
    assert np.isclose(tr["entry"], 10.0)
    assert np.isclose(tr["exit"], 11.0)
    gross = 11.0 / 10.0 - 1.0
    net = (1 + gross) * (1 - 10 / 1e4) - 1
    assert np.isclose(tr["net"], net)


def test_treatment_rolls_when_still_top_and_above_line():
    """treatment:到期仍在 TopN 且收盘≥入场价 → 续持(hold_days>1);baseline 同票只持 1 天。"""
    dates = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08"]
    feat = _mk_feat(dates, o=[10, 10, 11, 12, 13], h=[10, 11, 12, 13, 14],
                    lo=[10, 9.9, 10.9, 11.9, 12.9], c=[10, 10.5, 11.5, 12.5, 13.5])
    feats = {"000001": feat}
    market = {"mkt_ir": {d: 0.0 for d in dates}}
    # 每个决策日 TopN 都含该票 → treatment 应一直续持到数据末
    ranks = {d: ["000001"] for d in dates}
    base = P.simulate(feats, ranks, market, arm="baseline", N=1, topn=1,
                      entry_rule="limit_pc_0.0", cost_bps=0.0)
    trt = P.simulate(feats, ranks, market, arm="treatment", N=1, topn=1,
                     entry_rule="limit_pc_0.0", cost_bps=0.0)
    assert base.trades["hold_days"].iloc[0] == 1
    assert trt.trades["hold_days"].max() >= 2   # 续持多日
    # 续持吃到更长上涨 → treatment 末笔 net ≥ baseline 首笔 net
    assert trt.trades["net"].max() > base.trades["net"].iloc[0]


def test_treatment_exits_when_breaks_line():
    """treatment:跌破入场价(未破卖出线不成立)→ 到期即卖,不续持。"""
    dates = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07"]
    feat = _mk_feat(dates, o=[10, 10, 9.5, 9.0], h=[10, 10.2, 9.8, 9.2],
                    lo=[10, 9.9, 9.0, 8.5], c=[10, 10.1, 9.3, 9.0])  # D+2 收盘 9.3 < 入场 10
    feats = {"000001": feat}
    market = {"mkt_ir": {d: 0.0 for d in dates}}
    ranks = {d: ["000001"] for d in dates}
    trt = P.simulate(feats, ranks, market, arm="treatment", N=1, topn=1,
                     entry_rule="limit_pc_0.0", cost_bps=0.0)
    assert trt.trades["hold_days"].iloc[0] == 1   # 破线,未续持


def test_ranks_topn_desc():
    """排名视图按 score 降序取 TopN。"""
    feats = {
        "A": {"dates": np.array(["2020-01-02"]), "mom": np.array([0.5])},
        "B": {"dates": np.array(["2020-01-02"]), "mom": np.array([0.9])},
        "C": {"dates": np.array(["2020-01-02"]), "mom": np.array([0.1])},
    }
    ranks = R.build_daily_ranks(feats, score_key="mom", cap=60, min_cross=1)
    assert ranks["2020-01-02"][:2] == ["B", "A"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
