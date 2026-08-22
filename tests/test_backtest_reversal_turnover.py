"""反转低换手 前瞻回测器 单测(防未来函数 / 横截面标准化 / 真实成本净额 / 组合换手率)。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.backtest import backtest_reversal_turnover as bt
from tools.strategy.reversal_turnover import reversal_factor


def _mk_df(closes, turnovers=None, vols=None):
    n = len(closes)
    base = pd.Timestamp("2020-01-01")
    return pd.DataFrame({
        "date": [base + pd.Timedelta(days=i) for i in range(n)],
        "open": closes, "high": closes, "low": closes, "close": closes,
        "volume": vols if vols is not None else [1e6] * n,
        "turnover": turnovers if turnovers is not None else [1.0] * n,
    })


def test_panel_no_future_function(monkeypatch):
    """panel 里 (code,t) 的 rev 只由 close[:t+1] 决定;改 t 之后的价不改变该 rev。"""
    closes = list(np.linspace(10, 20, 60)) + [15, 16, 14, 13, 12]     # 65 根
    df = _mk_df(closes)
    monkeypatch.setattr(bt.market, "load_kline", lambda code: df)
    panel = bt.build_rt_panel(["X"], rev_n=5, turn_n=20, horizons=(5,), step=1, warmup=25)
    # 取某个 t 的行,手算 reversal_factor(close[:t+1]) 应完全一致
    row = panel.iloc[len(panel) // 2]
    # 反推 t:date 对应 df 下标
    t = df.index[df["date"] == pd.Timestamp(row["date"])][0]
    assert np.isclose(row["rev"], reversal_factor(np.array(closes[: t + 1], float), n=5))


def test_forward_return_uses_future_as_label():
    """前瞻收益 r_N = close[t+N]/close[t]-1(用未来价,但仅作被预测标签,不进 score)。"""
    closes = [10.0] * 30 + [11.0] * 30                              # t=29 后一段跳涨
    df = _mk_df(closes)
    import tools.backtest.backtest_reversal_turnover as m
    orig = m.market.load_kline
    m.market.load_kline = lambda code: df
    try:
        panel = m.build_rt_panel(["X"], rev_n=5, turn_n=20, horizons=(5,), step=1, warmup=20)
    finally:
        m.market.load_kline = orig
    assert "r_5" in panel.columns and len(panel) > 0


def test_add_scores_cross_sectional_zscore():
    """add_scores 逐日横截面 z-score:同日 z_rev 均值≈0;composite=w·zr+w·zt。"""
    panel = pd.DataFrame({
        "date": ["d1"] * 5,
        "code": list("ABCDE"),
        "rev": [0.1, -0.2, 0.3, 0.0, -0.1],
        "turn": [-1.0, -2.0, -0.5, -3.0, -1.5],
        "liq": [1e8] * 5,
        "r_5": [1, 2, 3, 4, 5],
    })
    out = bt.add_scores(panel, w_rev=0.5, w_turn=0.5)
    assert abs(out["z_rev"].mean()) < 1e-9
    assert abs(out["z_turn"].mean()) < 1e-9
    r0 = out.iloc[0]
    assert np.isclose(r0["score_composite"], 0.5 * r0["z_rev"] + 0.5 * r0["z_turn"])
    # 单因子列保留原始值(排序秩等价)
    assert list(out["score_rev"]) == list(panel["rev"])


def test_topk_net_cost_and_turnover():
    """net = gross − 组合换手率×往返成本;组合换手率 ∈ [0,1];成本使 net < gross。"""
    # 两个非重叠调仓日(N=5,dates 每 5 个取一个):d0=第0个,d1=第5个
    dates = [f"2020-01-{i:02d}" for i in range(1, 12)]              # 11 个交易日
    rows = []
    # 构造:每天 12 只票,score 决定 TopK;让 d0 与 d5 的 TopK 完全不同(换手=1)
    for di, d in enumerate(dates):
        for j in range(12):
            score = (j if di == 0 else 11 - j)                     # d5 时排序反转 → TopK 全换
            rows.append({"date": d, "code": f"C{j:02d}", "score_x": float(score),
                         "r_5": float(j)})
    panel = pd.DataFrame(rows)
    tk = bt.topk_net_metrics(panel, "score_x", N=5, k=3, roundtrip_bps=20.0)
    assert tk["调仓次数"] >= 2
    assert 0.0 <= tk["组合换手率"] <= 1.0
    assert tk["net每轮超额%"] < tk["gross每轮超额%"]                 # 成本拉低 net
    # 换手=1 时净成本每轮 = 1×20bps = 0.2%
    assert np.isclose(tk["净成本每轮%"], tk["组合换手率"] * 0.2, atol=1e-9)


def test_topk_low_turnover_cheaper():
    """组合换手率越低 → 净成本越低(核心看点:低换手腿省成本)。"""
    dates = [f"2020-02-{i:02d}" for i in range(1, 12)]
    # 稳定组合:每天 score 恒定 → TopK 不变 → 换手≈0
    rows = []
    for d in dates:
        for j in range(12):
            rows.append({"date": d, "code": f"S{j:02d}", "score_x": float(j), "r_5": float(j)})
    panel = pd.DataFrame(rows)
    tk = bt.topk_net_metrics(panel, "score_x", N=5, k=3, roundtrip_bps=20.0)
    assert tk["组合换手率"] < 0.01                                  # 几乎不换
    assert np.isclose(tk["净成本每轮%"], 0.0, atol=1e-6)
    assert np.isclose(tk["net每轮超额%"], tk["gross每轮超额%"], atol=1e-6)
