"""IET 行业面板聚合 单测(纯逻辑,无 IO)。

锁定:
  - 中位/均值口径;PE/PB 剔除 ≤0;成分股 < min_members → 行业弃权(返回 None)。
  - 无有效样本的聚合量置 None(不硬填 → 温度打分时该维弃权)。
  - aggregate_date 按行业分组、弃权行业不入表。
  - members_snapshot 归一逻辑(to_sw 归不动的 code 不出现)。
"""
import numpy as np
import pytest

from tools.analysis.industry_temp import panel as P


def test_aggregate_median_and_min_members():
    codes = ["A", "B", "C"]
    pe = {"A": 10.0, "B": 20.0, "C": 30.0}
    pb = {"A": 1.0, "B": 2.0, "C": 3.0}
    turn = {"A": 1.0, "B": 2.0, "C": 3.0}
    row = P.aggregate_industry("电子", "2026-01-05", codes, pe, pb, turn, min_members=3)
    assert row["n_members"] == 3
    assert row["pe_median"] == 20.0
    assert row["pb_median"] == 2.0
    assert row["turnover_mean"] == pytest.approx(2.0)
    # 成分不足 → 弃权
    assert P.aggregate_industry("电子", "d", ["A", "B"], pe, pb, turn, min_members=3) is None


def test_aggregate_drop_nonpositive_and_nan():
    codes = ["A", "B", "C", "D"]
    # PE 含负值和 NaN,应剔除后中位;PB 全无效 → None
    pe = {"A": -5.0, "B": np.nan, "C": 12.0, "D": 18.0}
    pb = {"A": -1.0, "B": 0.0, "C": None, "D": np.nan}
    turn = {"A": 1.0, "B": 3.0, "C": None, "D": 5.0}
    row = P.aggregate_industry("医药生物", "d", codes, pe, pb, turn, min_members=1)
    assert row["pe_median"] == 15.0            # median(12,18)
    assert row["pb_median"] is None            # 全部 ≤0/NaN
    assert row["turnover_mean"] == pytest.approx(3.0)  # mean(1,3,5)


def test_aggregate_date_groups_and_drops():
    members = {"A": "电子", "B": "电子", "C": "银行", "D": "电子"}
    pe = {k: 10.0 for k in members}
    pb = {k: 1.0 for k in members}
    turn = {k: 2.0 for k in members}
    rows = P.aggregate_date("d", members, pe, pb, turn, min_members=2)
    inds = {r["industry"] for r in rows}
    assert inds == {"电子"}          # 银行只有1只 < 2 弃权
    df = P.panel_to_frame(rows)
    assert list(df.columns) == P.PANEL_COLS
    assert (df["industry"] == "电子").all()


def test_build_panel_orchestration():
    # stub 注入:两日、两行业成分、固定读数
    universe = ["A", "B", "C"]
    members = {"A": "电子", "B": "电子", "C": "银行"}
    prov = lambda uni, date: {c: members[c] for c in uni}
    pe = lambda c, d: {"A": 10.0, "B": 20.0, "C": 30.0}[c]
    pb = lambda c, d: 1.5
    turn = lambda c, d: 2.0
    df = P.build_panel(["2024-01-02", "2024-01-03"], universe, prov, pe, pb, turn, min_members=2)
    # 银行单只<2弃权;电子每日一行,共2日 → 2行
    assert set(df["industry"]) == {"电子"}
    assert len(df) == 2
    row = df[df["date"] == "2024-01-02"].iloc[0]
    assert row["pe_median"] == 15.0 and row["n_members"] == 2


def test_members_snapshot_normalize():
    snap = {"000001": "银行", "600519": "食品饮料", "999999": "不存在行业xyz"}
    out = P.members_snapshot(["000001", "600519", "999999", "000002"], "d", snap)
    # 银行/食品饮料 能 to_sw 归一;xyz 归不动 → 不出现;000002 无源 → 不出现
    assert out.get("000001") == "银行"
    assert out.get("600519") == "食品饮料"
    assert "999999" not in out
    assert "000002" not in out
