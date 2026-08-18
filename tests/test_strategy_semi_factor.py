"""策略E_自选池半导体多因子 单测(移植自聚宽「半导体板块多因子策略」)。"""
from __future__ import annotations

import math

import pytest

from tools.strategy import registry as reg
from tools.strategy import semi_factor as sf


def _rec(rd_pct, rev_yoy_pct, 营收, mktcap_yi, pct_chg=0.5):
    """最小可用中心记录:填 3 因子所需字段 + snapshot 过业务过滤。"""
    return {
        "financial": {
            "derived": {"研发费用率": rd_pct, "营收增速": rev_yoy_pct},
            "利润表摘要": {"营业总收入": 营收},
        },
        "valuation": {"mktcap_yi": mktcap_yi},
        "snapshot": {"close": 10.0, "pct_chg": pct_chg},
    }


def test_registered():
    meta = reg.get("策略E_自选池半导体多因子")
    assert meta.kind == "选股"
    assert callable(meta.fn)


def test_score_weights_favor_rd_rev():
    """rd/rev 权重 0.6 最大:高研发投入的票综合分最高。"""
    recs = {
        "HIGH_RD": _rec(rd_pct=15.0, rev_yoy_pct=45.0, 营收=5e9, mktcap_yi=569.0),
        "MID_RD":  _rec(rd_pct=3.3,  rev_yoy_pct=190.0, 营收=1.9e10, mktcap_yi=10113.29),
        "LOW_RD":  _rec(rd_pct=1.4,  rev_yoy_pct=105.0, 营收=1e10, mktcap_yi=5571.0),
    }
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=3)
    assert out["codes"][0] == "HIGH_RD"                          # 高研发排第一
    assert out["codes"][-1] == "LOW_RD"                          # 低研发垫底


def test_filter_missing_data():
    """任一因子缺失(研发/营收/市值/营收增速)→ 剔除。"""
    recs = {
        "OK": _rec(rd_pct=5.0, rev_yoy_pct=30.0, 营收=1e10, mktcap_yi=500.0),
        "NO_RD": {"financial": {"derived": {"营收增速": 30.0},
                                "利润表摘要": {"营业总收入": 1e10}},
                  "valuation": {"mktcap_yi": 500.0},
                  "snapshot": {"pct_chg": 0.5}},
        "NO_MKTCAP": _rec(5.0, 30.0, 1e10, None),
        "NO_REV": {"financial": {"derived": {"研发费用率": 5.0, "营收增速": 30.0}},
                   "valuation": {"mktcap_yi": 500.0},
                   "snapshot": {"pct_chg": 0.5}},
        "NEG_RD": _rec(rd_pct=-1.0, rev_yoy_pct=30.0, 营收=1e10, mktcap_yi=500.0),
        "OK2": _rec(rd_pct=8.0, rev_yoy_pct=50.0, 营收=1e10, mktcap_yi=500.0),
    }
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=5)
    # 5 只被剔,只留 OK + OK2
    assert set(out["codes"]) == {"OK", "OK2"}


def test_filter_limit_up_and_paused():
    """涨跌停(|pct_chg|≥9.7)/ 停牌(snapshot 缺失)剔除。"""
    recs = {
        "LIMIT_UP":  _rec(5.0, 30.0, 1e10, 500.0, pct_chg=9.9),
        "LIMIT_DN":  _rec(5.0, 30.0, 1e10, 500.0, pct_chg=-9.8),
        "PAUSED":    {"financial": {"derived": {"研发费用率": 5.0, "营收增速": 30.0},
                                    "利润表摘要": {"营业总收入": 1e10}},
                      "valuation": {"mktcap_yi": 500.0},
                      "snapshot": None},
        "PASS_A":    _rec(6.0, 30.0, 1e10, 500.0),
        "PASS_B":    _rec(4.0, 30.0, 1e10, 500.0),
    }
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=5)
    assert set(out["codes"]) == {"PASS_A", "PASS_B"}


def test_zscore_and_winsor_helpers():
    """winsor 剪掉离群大值 + zscore 均值 0 方差 1(充分样本)。"""
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 100.0]                       # 100 是离群
    w = sf._winsorize_med(vals)
    assert w[-1] < 100.0                                          # 被剪
    z = sf._zscore([1.0, 2.0, 3.0, 4.0, 5.0])
    assert abs(sum(z) / len(z)) < 1e-9                            # 均值 ≈ 0
    assert abs((sum(v * v for v in z) / len(z)) ** 0.5 - 1) < 1e-9  # 方差 ≈ 1


def test_zscore_all_same_returns_zeros():
    """全同值 → std=0 → 返回全 0(避免除零)。"""
    z = sf._zscore([5.0, 5.0, 5.0])
    assert z == [0.0, 0.0, 0.0]


def test_winsor_all_same_returns_same():
    """全同值 → mad=0 → 原样返回。"""
    w = sf._winsorize_med([5.0, 5.0, 5.0])
    assert w == [5.0, 5.0, 5.0]


def test_top_k_and_score_ordering():
    """top_k 生效 + 综合分降序。"""
    recs = {f"S{i:02d}": _rec(rd_pct=float(i), rev_yoy_pct=float(i * 10),
                              营收=1e10, mktcap_yi=500.0)
            for i in range(1, 8)}
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=3)
    scores = [d["综合分"] for d in out["因子明细"]]
    assert scores == sorted(scores, reverse=True)                 # 综合分降序
    assert out["codes"] == [d["code"] for d in out["因子明细"][:3]]
    assert len(out["codes"]) == 3


def test_less_than_2_samples_returns_empty():
    """样本 <2 无法做横截面标准化 → 空结果 + note。"""
    recs = {"A": _rec(5.0, 30.0, 1e10, 500.0)}
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=5)
    assert out["codes"] == []
    assert "note" in out


def test_empty_records():
    """空 records / None → 空结果不炸。"""
    for rec in ({}, None):
        out = reg.run("策略E_自选池半导体多因子", rec, top_k=3)
        assert out["codes"] == []


def test_details_include_raw_and_zscored():
    """因子明细同时暴露原始值与标准化后值(供 web 展示)。"""
    recs = {
        "A": _rec(rd_pct=5.0, rev_yoy_pct=30.0, 营收=1e10, mktcap_yi=500.0),
        "B": _rec(rd_pct=8.0, rev_yoy_pct=50.0, 营收=1e10, mktcap_yi=500.0),
    }
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=2)
    d0 = out["因子明细"][0]
    for k in ("code", "综合分", "rd_rev", "rd_mcap", "rev_yoy",
              "rd_rev_z", "rd_mcap_z", "rev_yoy_z"):
        assert k in d0


def test_weights_match_original_paper():
    """权重与原脚本一致:rd/rev=0.6, rd/mcap=0.2, 营收增速=0.2。"""
    recs = {
        "A": _rec(5.0, 30.0, 1e10, 500.0),
        "B": _rec(8.0, 50.0, 1e10, 500.0),
    }
    out = reg.run("策略E_自选池半导体多因子", recs, top_k=2)
    w = out["权重"]
    assert w["rd_rev"] == 0.6 and w["rd_mcap"] == 0.2 and w["rev_yoy"] == 0.2
    # 综合分 = 加权和,验一次:
    d = out["因子明细"][0]
    assert math.isclose(
        d["综合分"],
        d["rd_rev_z"] * 0.6 + d["rd_mcap_z"] * 0.2 + d["rev_yoy_z"] * 0.2,
        abs_tol=1e-4,
    )
