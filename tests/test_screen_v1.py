"""V1 模块二选股单测(F2.3 护栏 / F2.4 硬规则AND / F2.6 达标占比)。

锁语义:硬规则 AND——任一条件不满足即不达标;护栏三条各自可剔除;达标占比 = 达标/有效。
"""
import pandas as pd
import pytest

from tools.analysis import screen_v1 as sv


def _breakout_df():
    """箱体放量突破序列(pattern 会判达标 + 量能配合)。"""
    base = [100 + (2 if i % 2 else -2) for i in range(20)]
    closes = base + [108]
    vols = [1000.0] * 20 + [2500.0]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes, "volume": vols,
    })


# ---------- 护栏 F2.3 ----------
def test_guardrail_pe_extreme_rejected():
    ok, reasons = sv.guardrail(pe_percentile=0.95, net_profit_growth=10, ann_titles=[])
    assert ok is False and any("PE分位" in r for r in reasons)


def test_guardrail_negative_growth_rejected():
    ok, reasons = sv.guardrail(pe_percentile=0.5, net_profit_growth=-3.0, ann_titles=[])
    assert ok is False and any("净利增速" in r for r in reasons)


def test_guardrail_compliance_keyword_rejected():
    ok, reasons = sv.guardrail(0.5, 10, ["公司收到证监会监管函"])
    assert ok is False and any("合规风险" in r for r in reasons)


def test_guardrail_clean_pass():
    ok, reasons = sv.guardrail(0.5, 12.0, ["关于回购公司股份的公告"])
    assert ok is True and reasons == []


def test_guardrail_missing_data_not_rejected():
    """缺 PE/增速(None)不主动剔除,避免误杀。"""
    assert sv.guardrail(None, None, [])[0] is True


# ---------- 硬规则 AND F2.4 ----------
def test_qualified_all_pass():
    r = sv.is_qualified(_breakout_df(), rs_stock_vs_board=5.0, rs_board_vs_hs300=3.0,
                        pe_percentile=0.5, net_profit_growth=10.0, ann_titles=[])
    assert r["达标"] is True and all(r["各项"].values())


def test_and_fails_if_rs_weak():
    r = sv.is_qualified(_breakout_df(), rs_stock_vs_board=-1.0, rs_board_vs_hs300=3.0,
                        pe_percentile=0.5, net_profit_growth=10.0)
    assert r["达标"] is False and "RS不达标" in r["剔除原因"]


def test_and_fails_if_guardrail_hit():
    r = sv.is_qualified(_breakout_df(), 5.0, 3.0, pe_percentile=0.99, net_profit_growth=10.0)
    assert r["达标"] is False and any("PE分位" in x for x in r["剔除原因"])


def test_and_fails_if_no_pattern():
    flat = [100 + (0.5 if i % 2 else -0.5) for i in range(21)]
    df = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=21, freq="D"),
                       "open": flat, "high": flat, "low": flat, "close": flat,
                       "volume": [1000.0] * 21})
    r = sv.is_qualified(df, 5.0, 3.0, 0.5, 10.0)
    assert r["达标"] is False and "无形态命中" in r["剔除原因"]


# ---------- 达标占比 F2.6 ----------
def test_market_breadth():
    results = {
        "A": {"达标": True}, "B": {"达标": False},
        "C": {"达标": True}, "D": {"达标": False},
    }
    b = sv.market_breadth(results)
    assert b["有效样本"] == 4 and b["达标数"] == 2
    assert b["达标占比"] == 0.5 and b["达标清单"] == ["A", "C"]


def test_market_breadth_empty():
    assert sv.market_breadth({})["达标占比"] == 0.0
