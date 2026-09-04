"""扣非质量(#31)排序型回测 glue 单测。

不依赖真实数据(本地 financial_report raw 为空):用**注入的合成装载器**验证横截面面板构造 +
复用的 rank-IC 指标 + 达标裁决逻辑,锁住:
  · 综合分随因子单调(因子越好 → 横截面综合分排名越前)→ 因子能预测收益时 rank-IC 显著为正 → 达标
  · 空面板 → 阻塞(缺数据诚实降级,不硬造)
  · 有效交易日 < MIN_N → 观察;显著为负 → 淘汰
  · _pos_asof 取"最后一根 date ≤ as_of"(防未来函数:调仓日定位不看未来)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.backtest import backtest_deduct_quality as bt


# ————————————————————————————— 合成装载器 —————————————————————————————
def _make_universe(n=40, days=100):
    """n 只票,质量 q=i/(n-1);日收益 = 漂移(随 q)+ 小噪声 → 前瞻收益随 q 单调(但非完美,
    使每日 rank-IC 高而有方差,ICIR/t 有限)。"""
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2025-01-01", periods=days)]
    codes = [f"{i:06d}" for i in range(n)]
    qmap = {codes[i]: i / (n - 1) for i in range(n)}
    klines = {}
    for i, c in enumerate(codes):
        q = qmap[c]
        rng = np.random.default_rng(1000 + i)
        rets = 1.0 + q * 0.02 + rng.normal(0.0, 0.004, days)   # 漂移随 q + 小噪声
        close = 10.0 * np.cumprod(rets)
        klines[c] = pd.DataFrame({"date": dates, "close": close.tolist(),
                                  "volume": [1e6] * days})
    return codes, dates, qmap, klines


def _record_builder(qmap):
    def _b(code, as_of):
        q = qmap[code]
        derived = {"扣非净利增速": q * 100, "扣非占归母": q, "现金含量_CFO比净利": q * 2,
                   "毛利率": q * 60, "归母净利增速": q * 50}   # 单期领先度=q*50,单调
        return {"meta": {"code": code}, "financial": {"derived": derived},
                "扣非质量": {"质量领先度": None}}             # 兜底用单期
    return _b


def _kline_loader(klines):
    return lambda code: klines.get(code)


# ————————————————————————————— 面板 + IC —————————————————————————————
def test_panel_score_tracks_factor_and_ic_positive():
    codes, dates, qmap, klines = _make_universe(days=150)
    rebalance = dates[5:40]                            # 35 个调仓日(≥MIN_N;t+60<150)
    panel = bt.build_quality_panel(
        codes, rebalance, horizons=(5, 20, 60),
        record_builder=_record_builder(qmap), kline_loader=_kline_loader(klines))
    assert not panel.empty
    for col in ("date", "code", "score", "liq", "r_5", "r_20", "r_60"):
        assert col in panel.columns
    # 综合分随质量单调:同一天里 q 最高的票综合分应最高
    g = panel[panel["date"] == rebalance[0]]
    top_code = g.sort_values("score", ascending=False)["code"].iloc[0]
    assert top_code == codes[-1]                       # q=1 的票综合分第一
    # 因子完美预测收益 → 20/60 日 rank-IC 显著为正
    rep = bt.evaluate(panel, horizons=(5, 20, 60), topk=10)
    assert rep["present"] is True
    ic20 = rep["分维度"][20]["rank_IC"]
    assert ic20["IC均值"] is not None and ic20["IC均值"] > 0.9
    assert rep["分维度"][20]["TopN"]["Top超额%"] > 0
    assert rep["verdict"] == "达标"


def test_empty_universe_blocked():
    """缺数据(记录构造恒 None)→ 面板空 → 阻塞(不下结论)。"""
    codes, dates, qmap, klines = _make_universe(n=5)
    panel = bt.build_quality_panel(
        codes, dates[5:8], record_builder=lambda c, d: None,
        kline_loader=_kline_loader(klines))
    assert panel.empty
    rep = bt.evaluate(panel)
    assert rep["present"] is False and rep["verdict"] == "阻塞"


# ————————————————————————————— 裁决逻辑 —————————————————————————————
def _per_h(ic_mean, t, excess, n_days):
    return {20: {"rank_IC": {"IC均值": ic_mean, "t": t, "有效交易日": n_days},
                 "TopN": {"Top超额%": excess}, "分层": []},
            60: {"rank_IC": {"IC均值": ic_mean, "t": t, "有效交易日": n_days},
                 "TopN": {"Top超额%": excess}, "分层": []}}


def test_verdict_pass():
    v, _ = bt._verdict(_per_h(0.05, 2.0, 1.2, 50), (20, 60))
    assert v == "达标"


def test_verdict_insufficient_sample():
    v, r = bt._verdict(_per_h(0.05, 2.0, 1.2, 10), (20, 60))
    assert v == "观察" and "MIN_N" in r


def test_verdict_significant_negative():
    v, _ = bt._verdict(_per_h(-0.05, -2.0, -1.2, 50), (20, 60))
    assert v == "淘汰"


def test_verdict_observe_when_not_significant():
    v, _ = bt._verdict(_per_h(0.01, 0.8, 0.1, 50), (20, 60))
    assert v == "观察"


# ————————————————————————————— 防未来函数定位 —————————————————————————————
def test_pos_asof_last_le():
    dates = ["2025-01-01", "2025-01-05", "2025-01-10", "2025-01-20"]
    assert bt._pos_asof(dates, "2025-01-10") == 2      # 精确命中
    assert bt._pos_asof(dates, "2025-01-15") == 2      # 取最后一根 ≤(不看未来的 01-20)
    assert bt._pos_asof(dates, "2024-12-31") is None   # 全在未来 → None
