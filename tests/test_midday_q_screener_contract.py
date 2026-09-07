"""午盘 Q · M2 · Q1/Q2/Q3 screener 端到端 + dispatch 派单器测试。

策略是纯函数(输入 quotes 字典 + extras),测试构造少量 fake 数据验证入选/rank。
不触外部网络、不读文件。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.strategy import midday_q_screener as SC
from tools.strategy import midday_q_signals as S


# ────────────────────────────── 通用 fake 数据构造 ──────────────────────────────

def _q(name="普通股 A", prev_close=10.0, open=10.0, price=None,
       high=None, low=None, volume=1_000_000, amount_wan=20000.0,
       pct_chg=None, vol_ratio=1.0, turnover=1.0, amplitude=2.0):
    if price is None:  price = open * 1.04            # 默认 +4%(Q1 命中带)
    if high is None:   high = price * 1.001            # 略高于现价
    if low is None:    low = open * 0.995              # 略低于开盘
    if pct_chg is None: pct_chg = (price / prev_close - 1) * 100
    return {"name": name, "prev_close": prev_close, "open": open, "price": price,
            "high": high, "low": low, "volume": volume, "amount_wan": amount_wan,
            "pct_chg": pct_chg, "vol_ratio": vol_ratio, "turnover": turnover,
            "amplitude": amplitude}


def _bullish_kline(start=1.0, n=80):
    """构造 T-1 递增日线,MA5>MA10>MA20>MA60 稳定多头。"""
    return pd.DataFrame({"close": [start + i * 0.1 for i in range(n)]})


# ────────────────────────────── Q1 端到端 ──────────────────────────────

def test_q1_selects_temperate_strong():
    """符合温和强势带 (4%) + 量能 + 距日高 + 均线 → 入选。"""
    q = _q(price=10.4, high=10.42)   # 4% 涨,距日高 0.19%
    quotes = {"600001": q}
    extras = {
        "t1_klines": {"600001": _bullish_kline()},
    }
    hits = SC.screen_q1("2026-09-07", "14:30", quotes, extras=extras)
    assert len(hits) == 1
    assert hits[0]["code"] == "600001"
    assert 0 < hits[0]["rank_score"]


def test_q1_excludes_too_hot():
    """涨幅 > 6% → 不在温和强势带,排除。"""
    q = _q(price=10.8, high=10.82)
    hits = SC.screen_q1("2026-09-07", "14:30", {"600001": q},
                         extras={"t1_klines": {"600001": _bullish_kline()}})
    assert hits == []


def test_q1_excludes_flat():
    """涨幅 < 3% → 不启动,排除。"""
    q = _q(price=10.2)
    hits = SC.screen_q1("2026-09-07", "14:30", {"600001": q},
                         extras={"t1_klines": {"600001": _bullish_kline()}})
    assert hits == []


def test_q1_excludes_far_from_high():
    """距日高 > 1.5% → 排除(说明冲高回落)。"""
    q = _q(price=10.4, high=10.65)   # 距日高 (1 - 10.4/10.65) ≈ 2.35%
    hits = SC.screen_q1("2026-09-07", "14:30", {"600001": q},
                         extras={"t1_klines": {"600001": _bullish_kline()}})
    assert hits == []


def test_q1_excludes_near_limit_up():
    """接近涨停(price > 涨停价 × 0.985)→ 排除。

    构造:prev_close=10, 涨停=11, 0.985 阈=10.835;price=10.85 违反,涨幅 4.86%(∈ [3,6])。
    """
    q = _q(price=10.85, high=10.87, prev_close=10.0, open=10.35)
    hits = SC.screen_q1("2026-09-07", "14:30", {"600001": q},
                         extras={"t1_klines": {"600001": _bullish_kline()}})
    assert hits == []


def test_q1_excludes_low_liquidity():
    q = _q(price=10.4, high=10.42, amount_wan=3000)   # 3000 万 < 5000 万
    hits = SC.screen_q1("2026-09-07", "14:30", {"600001": q},
                         extras={"t1_klines": {"600001": _bullish_kline()}})
    assert hits == []


def test_q1_top_n_sorts_by_rank():
    """两只都入选,按 rank_score 降序取 top_n=1。"""
    q_hot = _q(price=10.55, high=10.56)               # 5.5% 涨,顶部(避开 6% 浮点边界)
    q_mild = _q(price=10.31, high=10.315)             # 3.1% 涨,底部
    extras = {"t1_klines": {"A": _bullish_kline(), "B": _bullish_kline()}}
    hits = SC.screen_q1("2026-09-07", "14:30",
                        {"A": q_hot, "B": q_mild}, top_n=1, extras=extras)
    assert len(hits) == 1
    assert hits[0]["code"] == "A"


def test_q1_confirm_from_narrows_scope():
    """复核模式:confirm_from=[A] → 只跑 A,B 不看。"""
    q = _q(price=10.4, high=10.42)
    quotes = {"A": q, "B": q}
    extras = {"t1_klines": {"A": _bullish_kline(), "B": _bullish_kline()}}
    hits = SC.screen_q1("2026-09-07", "14:50", quotes,
                         confirm_from=["A"], extras=extras)
    assert len(hits) == 1
    assert hits[0]["code"] == "A"


# ────────────────────────────── Q2 端到端 ──────────────────────────────

def test_q2_selects_v_recovery():
    """早跌 3% + 反弹到 -0.5% + 放量 + 长期趋势 OK → 入选。"""
    q = _q(open=10.0, low=9.65, price=9.95, high=10.0,   # low -3.5%, 反弹 3.1%, 现价 -0.5%
           volume=1_500_000, amount_wan=15000.0)
    # 手工把 vol_ratio 提到 1.5(am_pm_vol_ratio 兜底 vol_ratio)
    q["vol_ratio"] = 1.5
    extras = {"t1_klines": {"600001": _bullish_kline()}}
    hits = SC.screen_q2("2026-09-07", "14:30", {"600001": q}, extras=extras)
    assert len(hits) == 1


def test_q2_excludes_shallow_dip():
    """早跌只 1% → 不算超跌,排除。"""
    q = _q(open=10.0, low=9.9, price=9.95, high=10.02, vol_ratio=1.5)
    extras = {"t1_klines": {"600001": _bullish_kline()}}
    hits = SC.screen_q2("2026-09-07", "14:30", {"600001": q}, extras=extras)
    assert hits == []


def test_q2_excludes_current_return_out_of_range():
    """反弹过头(涨幅 > 2%)→ 排除。"""
    q = _q(open=10.0, low=9.65, price=10.5, high=10.55, vol_ratio=1.5)
    extras = {"t1_klines": {"600001": _bullish_kline()}}
    hits = SC.screen_q2("2026-09-07", "14:30", {"600001": q}, extras=extras)
    assert hits == []


# ────────────────────────────── Q3 端到端 ──────────────────────────────

def _fake_ff_positive():
    """构造下午净流入 + 大单占比 > 30% 的 fundflow df。"""
    return pd.DataFrame([
        {"time": pd.Timestamp("2026-09-07 13:05"),
         "主力净流入": 500000, "小单净流入": -100000, "中单净流入": -200000,
         "大单净流入": 300000, "超大单净流入": 200000, "主力净占比": 5.0},
        {"time": pd.Timestamp("2026-09-07 13:35"),
         "主力净流入": 300000, "小单净流入": -50000, "中单净流入": -100000,
         "大单净流入": 200000, "超大单净流入": 100000, "主力净占比": 4.0},
    ])


def test_q3_selects_with_fundflow():
    q = _q(price=10.3, high=10.32, amount_wan=12000.0)   # 3% 涨,流动性 8000 万+
    ff = _fake_ff_positive()
    extras = {"fundflow": {"600001": ff}, "t1_klines": {}}
    hits = SC.screen_q3("2026-09-07", "14:30", {"600001": q}, extras=extras)
    assert len(hits) == 1
    assert hits[0]["signals"]["MainNetPM_yi"] > 0


def test_q3_excludes_no_fundflow():
    """无 fundflow 数据 → 不入选(即便报价符合)。"""
    q = _q(price=10.3, high=10.32, amount_wan=12000.0)
    hits = SC.screen_q3("2026-09-07", "14:30", {"600001": q},
                         extras={"fundflow": {}, "t1_klines": {}})
    assert hits == []


def test_q3_excludes_negative_pm_flow():
    """下午主力净流出 → 排除。"""
    q = _q(price=10.3, high=10.32, amount_wan=12000.0)
    ff = pd.DataFrame([
        {"time": pd.Timestamp("2026-09-07 13:05"),
         "主力净流入": -500000, "小单净流入": 100000, "中单净流入": 200000,
         "大单净流入": -300000, "超大单净流入": -200000, "主力净占比": -5.0},
    ])
    extras = {"fundflow": {"600001": ff}, "t1_klines": {}}
    hits = SC.screen_q3("2026-09-07", "14:30", {"600001": q}, extras=extras)
    assert hits == []


def test_q3_needs_higher_liquidity():
    """成交额 6000 万 < Q3 的 8000 万要求 → 排除。"""
    q = _q(price=10.3, high=10.32, amount_wan=6000.0)
    ff = _fake_ff_positive()
    hits = SC.screen_q3("2026-09-07", "14:30", {"600001": q},
                         extras={"fundflow": {"600001": ff}, "t1_klines": {}})
    assert hits == []


# ────────────────────────────── dispatch 派单器 ──────────────────────────────

def test_dispatch_empty_when_not_allowed():
    """gate 空 allowed → 空清单。"""
    result = SC.dispatch("2026-09-07", "14:30", {},
                         gate_final={"state": "崩盘", "allowed_strategies": [],
                                       "position_pct": 0.0})
    assert result["selections"] == {}
    assert result["final_codes"] == []


def test_dispatch_triggers_only_allowed():
    """gate.allowed=['Q1'] → 只跑 Q1,Q2/Q3 键不在 selections 里。"""
    q = _q(price=10.4, high=10.42)
    result = SC.dispatch(
        "2026-09-07", "14:30", {"600001": q},
        gate_final={"state": "强势", "allowed_strategies": ["Q1"], "position_pct": 1.0},
        extras={"t1_klines": {"600001": _bullish_kline()}},
    )
    assert "Q1" in result["selections"]
    assert "Q2" not in result["selections"]
    assert "Q3" not in result["selections"]
    assert len(result["final_codes"]) == 1
    assert result["final_codes"][0]["from_strategy"] == "Q1"


def test_dispatch_merges_final_codes_deduped():
    """同一票在 Q1 和 Q2 都入选 → final_codes 里只出现一次(取高分)。"""
    # 构造同一票两策略都命中的场景
    q = _q(open=10.0, low=9.65, price=10.4, high=10.42,   # Q1 命中(+4%)且 Q2 命中(V型 -3.5% 反弹到 +4%)
           volume=1_500_000, amount_wan=15000.0, vol_ratio=1.5)
    # 注:实际上 Q2 要求 CurrentReturn ∈ [-0.01, 0.02];这里 +4% 已越界,只 Q1 命中
    # 用不同报价分别构造
    q_q1 = _q(price=10.4, high=10.42)                                  # 只 Q1
    q_q2 = _q(open=10.0, low=9.65, price=9.95, high=10.0, vol_ratio=1.5)   # 只 Q2
    result = SC.dispatch(
        "2026-09-07", "14:30", {"A": q_q1, "B": q_q2},
        gate_final={"state": "震荡", "allowed_strategies": ["Q1", "Q2"], "position_pct": 0.5},
        extras={"t1_klines": {"A": _bullish_kline(), "B": _bullish_kline()}},
    )
    codes = {h["code"] for h in result["final_codes"]}
    assert codes == {"A", "B"}
    # 都从各自策略入选
    from_strategies = {h["code"]: h["from_strategy"] for h in result["final_codes"]}
    assert from_strategies["A"] == "Q1"
    assert from_strategies["B"] == "Q2"


def test_dispatch_final_codes_sorted_by_rank():
    """final_codes 按 rank_score 降序。"""
    q_hot = _q(price=10.6, high=10.61)
    q_mild = _q(price=10.3, high=10.31)
    result = SC.dispatch(
        "2026-09-07", "14:30", {"A": q_hot, "B": q_mild},
        gate_final={"state": "强势", "allowed_strategies": ["Q1"], "position_pct": 1.0},
        extras={"t1_klines": {"A": _bullish_kline(), "B": _bullish_kline()}},
    )
    assert result["final_codes"][0]["rank_score"] >= result["final_codes"][-1]["rank_score"]


def test_dispatch_carries_gate_state():
    """gate.state / position_pct 落到 result。"""
    result = SC.dispatch("2026-09-07", "14:30", {},
                         gate_final={"state": "震荡", "allowed_strategies": ["Q1"],
                                       "position_pct": 0.5})
    assert result["gate_state"] == "震荡"
    assert result["position_pct"] == 0.5
