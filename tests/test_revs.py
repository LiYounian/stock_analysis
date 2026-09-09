"""REVS 四因子(阶段1 E/V/S)后端单测。

断言锁住"为什么这么设计"的语义,防未来重写误删规则:
  · S 情绪纯因子:动量=N日收益(原始,方向合成时施加)、有界回看=防未来函数、换手近端 NaN 兜底、波动率
  · E 盈利 PIT:只纳入 disclosure_date<=as_of 的报告期(未披露不可见);as_of=None 用全部
  · V 估值:PE/PB≤0(亏损/负净资产)剔为 None;三项全缺→None
  · 横截面:多维合成方向(E 高好、V 低好、S 反转/低换手/低波)、缺维**重归一**不塌缩、min_dims 门
  · 市值分位按当日横截面秩算并入 V;业务过滤(代码头/ST/停牌/涨跌停/低流动性)分类计数
  · 样本 <2 降级、空 records 不炸、include_dims 子集、top_k 生效
"""
from __future__ import annotations

import math

import pytest

from tools.strategy import registry as reg
from tools.strategy import revs

NAN = float("nan")


# ————————————————————————————— S 情绪纯因子 —————————————————————————————
def test_momentum_raw_sign_and_value():
    """动量返回**原始**收益率(方向在合成施加):涨→正、跌→负。"""
    up = revs.momentum_factor([10, 10, 10, 10, 10, 12], n=5)
    down = revs.momentum_factor([10, 10, 10, 10, 10, 8], n=5)
    assert math.isclose(up, 0.2, abs_tol=1e-9)
    assert math.isclose(down, -0.2, abs_tol=1e-9)


def test_momentum_insufficient_or_illegal():
    assert revs.momentum_factor([1, 2, 3], n=5) is None
    assert revs.momentum_factor([0, 1, 2, 3, 4, 5], n=5) is None   # 基准价=0
    assert revs.momentum_factor([1, 2, 3, 4, 5, NAN], n=5) is None


def test_momentum_bounded_lookback_anti_future():
    """防未来函数:动量只读最后 N+1 根,窗口外历史改变不影响。"""
    base = [1, 2, 3, 100, 100, 100, 100, 100, 105]
    v1 = revs.momentum_factor(base, n=5)
    v2 = revs.momentum_factor([999, -50, 7] + base[3:], n=5)
    assert v1 == v2


def test_turnover_mean_and_nan_tail():
    """换手=近N日有效均值(原始正数);近端 NaN 按有效值兜底。"""
    assert math.isclose(revs.turnover_mean([2.0] * 20, n=20), 2.0, abs_tol=1e-9)
    assert math.isclose(revs.turnover_mean([2.0] * 15 + [NAN] * 5, n=20), 2.0, abs_tol=1e-9)


def test_turnover_mean_too_few_valid():
    """有效点 < 半窗 → None(不静默造假)。"""
    assert revs.turnover_mean([2.0] * 5 + [NAN] * 15, n=20) is None


def test_volatility_positive_and_zero():
    """波动率=近N日收益 std:恒定收益→0,波动大→更大。"""
    flat = revs.volatility_factor([100 * (1.01 ** i) for i in range(21)], n=20)  # 恒定 1% 涨
    assert math.isclose(flat, 0.0, abs_tol=1e-9)
    jumpy = revs.volatility_factor([100, 110, 99, 115, 95, 120, 90, 125, 88, 130,
                                    85, 135, 80, 140, 78, 145, 75, 150, 70, 155, 68], n=20)
    assert jumpy > 0.05


# ————————————————————————————— E 盈利 PIT —————————————————————————————
def _period(rev, np_, eq):
    """构造单期最小三大表(营收/归母净利/归母净资产)。"""
    return {"利润表": {"营业总收入": rev, "归母净利润": np_},
            "资产负债表": {"归母股东权益": eq}}


def _periods_with_disc():
    """两年 × 两期,带披露日;供 PIT 选期 + 增速/ROE 计算。"""
    p = {
        "2023-12-31": {**_period(800, 80, 500), "disclosure_date": "2024-03-31", "report_date": "2023-12-31"},
        "2024-06-30": {**_period(500, 50, 520), "disclosure_date": "2024-08-30", "report_date": "2024-06-30"},
        "2024-12-31": {**_period(1000, 120, 560), "disclosure_date": "2025-03-31", "report_date": "2024-12-31"},
        "2025-06-30": {**_period(650, 70, 600), "disclosure_date": "2025-08-30", "report_date": "2025-06-30"},
    }
    return p


def test_earnings_pit_picks_latest_disclosed():
    """PIT:as_of 只能看到 disclosure_date<=as_of 的最新期。"""
    p = _periods_with_disc()
    # 2025-04-01 可见:2024-12-31(披露 2025-03-31);不可见 2025-06-30(披露 2025-08-30)
    e = revs.earnings_subfactors(p, as_of="2025-04-01")
    assert e["_报告期"] == "2024-12-31"
    # 归母净利增速 = (120-80)/|80|*100 = 50%
    assert math.isclose(e["归母净利增速"], 50.0, abs_tol=1e-6)
    # ROE = 120/560*100
    assert math.isclose(e["ROE"], round(120 / 560 * 100, 4), abs_tol=1e-3)


def test_earnings_pit_advances_after_new_disclosure():
    """新报告期披露后(as_of 越过其披露日)才切到新期。"""
    p = _periods_with_disc()
    e = revs.earnings_subfactors(p, as_of="2025-09-01")   # 2025-06-30 已披露
    assert e["_报告期"] == "2025-06-30"


def test_earnings_none_before_any_disclosure():
    """as_of 早于所有披露日 → 无可见期 → None。"""
    assert revs.earnings_subfactors(_periods_with_disc(), as_of="2023-01-01") is None


def test_earnings_asof_none_uses_all():
    """as_of=None(实盘取最新)→ 用全部期,取最新报告期。"""
    e = revs.earnings_subfactors(_periods_with_disc(), as_of=None)
    assert e["_报告期"] == "2025-06-30"


def test_earnings_empty():
    assert revs.earnings_subfactors({}, as_of="2025-01-01") is None


# ————————————————————————————— V 估值 —————————————————————————————
def test_valuation_drops_nonpositive_pe_pb():
    """PE/PB≤0(亏损/负净资产)不是'便宜'而是无意义 → None。"""
    v = revs.valuation_subfactors({"PE_TTM": -30, "PB": 2.0, "总市值": 1e10})
    assert v["PE_TTM"] is None
    assert v["PB"] == 2.0
    assert v["总市值"] == 1e10


def test_valuation_all_missing():
    assert revs.valuation_subfactors({"PE_TTM": None, "PB": None, "总市值": None}) is None
    assert revs.valuation_subfactors({}) is None


# ————————————————————————————— 横截面选股 —————————————————————————————
def _rec(e=None, v=None, s=None, amt=9999.0, mv=5e9, pct=0.5, st=False):
    """最小中心记录:各维原始子因子 + snapshot 过业务过滤。mv 也放进 V.总市值。"""
    if v is not None and "总市值" not in v:
        v = {**v, "总市值": mv}
    return {"REVS": {"E": e, "V": v, "S": s},
            "snapshot": {"amount_wan": amt, "pct_chg": pct, "close": 10.0, "is_st": st}}


def _E(g, rev, roe):
    return {"归母净利增速": g, "营收增速": rev, "ROE": roe}


def _V(pe, pb, mv):
    return {"PE_TTM": pe, "PB": pb, "总市值": mv}


def _S(mom, turn, vol):
    return {"动量": mom, "换手": turn, "波动率": vol}


def test_registered():
    meta = reg.get("REVS四因子")
    assert meta.kind == "选股"
    assert callable(meta.fn)


def test_composite_ranking_direction():
    """三维皆好(高成长/低估值小市值/反转低换手低波)→ 综合分最高;皆差→最低。"""
    recs = {
        "600001": _rec(_E(80, 40, 22), _V(10, 1.0, 4e9), _S(-0.08, 1.2, 0.015)),   # BEST
        "600002": _rec(_E(10, 12, 11), _V(25, 2.5, 1e10), _S(0.02, 4.0, 0.03)),    # MID
        "600003": _rec(_E(-30, 3, 6), _V(90, 7.0, 5e10), _S(0.20, 9.0, 0.06)),     # WORST
    }
    out = reg.run("REVS四因子", recs, top_k=3)
    assert out["codes"][0] == "600001"
    assert out["codes"][-1] == "600003"


def test_valuation_direction_cheaper_higher():
    """V 维:仅 PE 不同,便宜的票综合分更高(方向 -1)。"""
    recs = {
        "600001": _rec(v=_V(10, 1.5, 5e9), e=_E(10, 10, 10), s=_S(0.0, 3.0, 0.03)),
        "600002": _rec(v=_V(60, 1.5, 5e9), e=_E(10, 10, 10), s=_S(0.0, 3.0, 0.03)),
    }
    out = reg.run("REVS四因子", recs, top_k=2)
    assert out["codes"][0] == "600001"


def test_reweight_on_missing_dim_not_collapse():
    """缺维重归一:缺 V 的票靠 E+S(≥min_dims)仍参与,不因缺维塌成 0 被埋。"""
    recs = {
        "600001": _rec(_E(90, 45, 24), None, _S(-0.10, 1.0, 0.012)),   # 缺 V,但 E+S 极好
        "600002": _rec(_E(5, 5, 8), _V(30, 3.0, 2e10), _S(0.10, 6.0, 0.05)),
        "600003": _rec(_E(0, 0, 7), _V(40, 4.0, 3e10), _S(0.15, 8.0, 0.06)),
    }
    out = reg.run("REVS四因子", recs, top_k=3)
    assert out["codes"][0] == "600001"      # 缺 V 不妨碍它凭 E+S 夺魁
    d0 = out["因子明细"][0]
    assert d0["维度分"]["V估值"] is None      # V 缺失如实记 None
    assert set(d0["present维度"]) == {"E盈利", "S情绪"}


def test_min_dims_filter():
    """present 维度 < min_dims → 跳过(缺维重归一但太少不选)。"""
    recs = {
        "ONLY_S": _rec(None, None, _S(-0.1, 1.0, 0.01)),   # 仅 1 维
        "600002": _rec(_E(10, 10, 10), _V(20, 2.0, 5e9), _S(0.0, 3.0, 0.03)),
        "600003": _rec(_E(5, 5, 8), _V(30, 3.0, 8e9), _S(0.05, 4.0, 0.04)),
    }
    out = reg.run("REVS四因子", recs, top_k=5, min_dims=2)
    assert "ONLY_S" not in out["codes"]
    assert out["跳过"].get("有效维度<2") == 1


def test_market_cap_percentile_small_higher():
    """市值分位按当日横截面秩算并入 V(方向 -1):小市值票 V 更高。"""
    recs = {
        "SMALL": _rec(v={"PE_TTM": 20, "PB": 2.0}, mv=2e9, e=_E(10, 10, 10), s=_S(0.0, 3.0, 0.03)),
        "BIG": _rec(v={"PE_TTM": 20, "PB": 2.0}, mv=5e11, e=_E(10, 10, 10), s=_S(0.0, 3.0, 0.03)),
    }
    out = reg.run("REVS四因子", recs, top_k=2)
    assert out["codes"][0] == "SMALL"


def test_exclude_heads_keeps_chinext_drops_star():
    """默认剔科创68/北交8·4,保留创业板30(与'剔ST/科创/北交'字面一致)。"""
    recs = {
        "600001": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03)),
        "300001": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03)),   # 创业:保留
        "688001": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03)),   # 科创:剔
        "830001": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03)),   # 北交:剔
    }
    out = reg.run("REVS四因子", recs, top_k=5)
    assert "300001" in out["codes"]
    assert "688001" not in out["codes"] and "830001" not in out["codes"]
    assert out["跳过"].get("剔除代码头") == 2


def test_filter_st_limit_liquidity():
    """ST / 涨跌停 / 低流动性(成交额)分类剔除计数。"""
    recs = {
        "ST1": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03), st=True),
        "UP": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03), pct=9.9),
        "ILLIQ": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03), amt=100.0),
        "OK1": _rec(_E(12, 11, 12), _V(18, 1.8, 4e9), _S(-0.02, 2, 0.02)),
        "OK2": _rec(_E(8, 9, 9), _V(25, 2.5, 6e9), _S(0.03, 4, 0.04)),
    }
    out = reg.run("REVS四因子", recs, top_k=5)
    assert set(out["codes"]) == {"OK1", "OK2"}
    assert out["跳过"].get("ST") == 1
    assert out["跳过"].get("涨跌停") == 1
    assert out["跳过"].get("低流动性(成交额)") == 1


def test_include_dims_subset():
    """include_dims 只用维度子集(回测 1a:E+S,不含 V)。"""
    recs = {
        "600001": _rec(_E(80, 40, 22), _V(90, 9, 5e11), _S(-0.08, 1.2, 0.015)),  # V 很差但不参与
        "600002": _rec(_E(-20, 2, 6), _V(8, 0.8, 2e9), _S(0.15, 8, 0.06)),
    }
    out = reg.run("REVS四因子", recs, top_k=2, include_dims=["E盈利", "S情绪"])
    assert out["参与维度"] == ["E盈利", "S情绪"]
    assert out["codes"][0] == "600001"          # 只看 E+S,600001 好
    # V 未参与:维度分里不含 V估值
    assert "V估值" not in out["因子明细"][0]["维度分"]


def test_less_than_2_samples_and_empty():
    """样本<2 → 空+note;空/None records 不炸。"""
    one = reg.run("REVS四因子", {"600001": _rec(_E(10, 10, 10), _V(20, 2, 5e9), _S(0, 3, 0.03))}, top_k=5)
    assert one["codes"] == [] and "note" in one
    for r in ({}, None):
        assert reg.run("REVS四因子", r, top_k=3)["codes"] == []


def test_valuation_asof_pit_and_dropneg():
    """1b 回测 V 取数器:as_of 只取≤当日最新行(防未来);PE/PB≤0 剔为 None;早于序列→None。"""
    import numpy as np

    from tools.backtest import backtest_revs as B
    tl = (["2024-01-05", "2024-03-20", "2024-06-10"],
          np.array([20.0, -5.0, 15.0]),      # 2024-03-20 那行 PE 为负(亏损)
          np.array([2.0, 1.5, 1.2]),
          np.array([100.0, 110.0, 120.0]))
    assert B._valuation_asof(tl, "2024-02-01") == (20.0, 2.0, 100.0)   # 取 01-05 行
    r = B._valuation_asof(tl, "2024-04-01")                            # 取 03-20 行,PE<0→None
    assert r[0] is None and r[1] == 1.5 and r[2] == 110.0
    assert B._valuation_asof(tl, "2023-12-01") is None                 # 早于序列→无可见
    assert B._valuation_asof(None, "2024-05-01") is None


def test_top_k_and_ordering():
    """top_k 生效 + 综合分降序。"""
    recs = {f"6000{i:02d}": _rec(_E(i * 10, i * 5, i * 2), _V(100 - i * 5, 5 - i * 0.3, (10 - i) * 1e9),
                                 _S(-i * 0.01, 10 - i, 0.06 - i * 0.003)) for i in range(1, 8)}
    out = reg.run("REVS四因子", recs, top_k=3)
    scores = [d["综合分"] for d in out["因子明细"]]
    assert scores == sorted(scores, reverse=True)
    assert out["codes"] == [d["code"] for d in out["因子明细"][:3]]
    assert len(out["codes"]) == 3
