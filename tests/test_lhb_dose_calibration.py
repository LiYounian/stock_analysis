"""龙虎榜风控轴「剂量标定」语义锁 + 标定/回放/记分卡脚本冒烟(WI-6 Phase 3)。

锁死本轮标定结论,防未来 prompt/代码重写把它改回工程占位:
  1. **触发罚分=0.5**(标定值,与财报红旗 0.5/面等量协调;非旧占位 0.6),且在综合分尺度上
     确能把命中票挤出前段:命中票原分处 top-N 边界档,减罚分后落到该档之下。
  2. **最小净买占比门槛=0.0**(标定保持:H5/H10 各档一致见光死,不抬门槛漏低档)。
  3. **模式=降权**(软沉底,非硬否决)。
  4. **不越合成封顶**:标定罚分 + 财报叠加 ≤ 风控汇聚.罚分上限。
  5. 标定罚分作用于真实分布的**方向正确性**:命中即降分(纯函数,防未来函数)。
  6. 门槛证据解析:_threshold_analysis 从 lab 分档正确判"仅高档负=H1 / 全档负=H5/H10"。
  7. 脚本冒烟:calibrate / replay / forward_scorecard 用注入的假数据能跑通、产结构正确的产物,
     不触网(loader/事件注入),防未来函数(list_date<as_of)。

⚠️ 非投资建议:标定只改排序展示尺度。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.config import strategy as sc


# ———————————— 1~4. 标定值语义锁(真源 config) ————————————
def _lhb_axis() -> dict:
    return (sc.risk_veto_cfg().get("龙虎榜", {}) or {})


def test_calibrated_penalty_is_half_not_placeholder():
    """触发罚分标定为 0.5(与红旗 0.5/面等量),非旧占位 0.6。"""
    assert _lhb_axis().get("触发罚分") == pytest.approx(0.5)


def test_calibrated_penalty_coordinates_with_redflag_face():
    """标定罚分 == 财报红旗单面罚分(协调锚:一次净买上榜 ≈ 一面红旗的风险事件量级)。"""
    rf_face = float(sc.redflag_cfg().get("每面罚分", 0.5))
    assert _lhb_axis().get("触发罚分") == pytest.approx(rf_face)


def test_calibrated_threshold_stays_zero():
    """最小净买占比门槛标定保持 0.0(H5/H10 各档一致见光死,抬门槛会漏低档)。"""
    assert float(_lhb_axis().get("最小净买占比", -1)) == 0.0


def test_calibrated_mode_is_downweight_not_veto():
    assert _lhb_axis().get("模式") == "降权"
    assert _lhb_axis().get("按条数加权") is False   # 单次固定剂量,分级罚分缺标定


def test_calibrated_penalty_plus_redflag_within_cap():
    """标定罚分 + 财报红旗上限 不越合成封顶(避免标定值把封顶顶穿)。"""
    agg = sc.risk_veto_cfg()
    cap = float(agg.get("罚分上限", 1.5))
    lhb_pen = float(_lhb_axis().get("触发罚分", 0.5))
    rf_cap = float(sc.redflag_cfg().get("罚分上限", 1.2))
    # 单轴罚分各自 ≤ 合成封顶;两轴叠加超顶时由 risk_veto_adjust 再封顶(此处只锁标定值不自破封顶)。
    assert lhb_pen <= cap
    assert rf_cap <= cap
    # 端到端:命中 + 3 面红旗(罚分溢出)→ 合成后被封顶,不超过 cap
    over = sc.risk_veto_adjust(0.3, 3, {"triggered": True, "reason": "净买上榜", "n_recent": 1}, agg)
    assert over["罚分"] <= cap + 1e-9


# ———————————— 5. 标定罚分在真实尺度上"命中即挤出" ————————————
def test_penalty_ejects_hit_from_top_boundary():
    """经验分布 top-N 边界≈0.55;命中票(边界分)减 0.5 后必落到边界之下(挤出前段)。"""
    axis = {"启用": True, "模式": "降权", "触发罚分": 0.5, "按条数加权": False,
            "条数上限": 3, "否决沉底保留展示": True, "最小净买占比": 0.0}
    agg = {"启用": True, "罚分上限": 1.5, "龙虎榜": axis}
    boundary = 0.55
    a = sc.risk_veto_adjust(boundary, 0, {"triggered": True, "reason": "净买上榜", "n_recent": 1}, agg)
    assert a["排序分"] == pytest.approx(boundary - 0.5)
    assert a["排序分"] < boundary                        # 方向正确:命中被压低,绝不抬高


# ———————————— 6. 门槛证据解析 ————————————
def test_threshold_analysis_reads_bucket_evidence():
    from tools.backtest import lhb_dose_lab as LAB
    lab = {"net_buy_ratio_buckets": {
        # H1:仅高档显著负(only_high)
        "H1": [{"ratio_range": [0, 1.8], "net_excess": 0.0007, "net_p": 0.6},
               {"ratio_range": [1.8, 4.4], "net_excess": -0.0008, "net_p": 0.55},
               {"ratio_range": [4.4, 9.2], "net_excess": -0.0022, "net_p": 0.08},
               {"ratio_range": [9.2, 89], "net_excess": -0.0076, "net_p": 0.0}],
        # H5:各档一致显著负(all_neg),门槛应保持 0.0
        "H5": [{"ratio_range": [0, 1.8], "net_excess": -0.0187, "net_p": 0.0},
               {"ratio_range": [1.8, 4.4], "net_excess": -0.0222, "net_p": 0.0},
               {"ratio_range": [4.4, 9.2], "net_excess": -0.0165, "net_p": 0.0},
               {"ratio_range": [9.2, 89], "net_excess": -0.0101, "net_p": 0.008}],
    }}
    thr = LAB._threshold_analysis(lab)
    assert thr["by_horizon"]["H1"]["only_high_sig_neg"] is True
    assert thr["by_horizon"]["H5"]["all_buckets_sig_neg"] is True
    assert thr["recommended_min_net_buy_ratio"] == 0.0   # H5 全档负 → 门槛 0.0


# ———————————— 7. 脚本冒烟(注入假数据,不触网,防未来函数) ————————————
def test_calibrate_smoke(monkeypatch, tmp_path):
    """calibrate 用注入的综合分分布 + lab 跑通,产结构正确报告,罚分建议在候选内。"""
    from tools.backtest import lhb_dose_lab as LAB
    rng = np.random.default_rng(0)
    # 造两日、每日 200 只票的综合分(top 紧凑~0.55,中位~0)
    per_date = {"2026-08-19": np.sort(np.concatenate([rng.normal(0, 0.15, 180),
                                                      np.linspace(0.45, 0.6, 20)])),
                "2026-08-20": np.sort(np.concatenate([rng.normal(0, 0.15, 180),
                                                      np.linspace(0.45, 0.6, 20)]))}
    monkeypatch.setattr(LAB, "collect_score_dist", lambda dates, ul: per_date)
    out = tmp_path / "calib.json"
    rep = LAB.calibrate(["2026-08-19", "2026-08-20"], 200, candidates=(0.3, 0.5, 0.6),
                        out=str(out), lab_path="data/analysis/backtest/lhb_veto_lab.json")
    assert out.exists()
    assert rep["score_dist"]["n"] == 400
    assert 0.3 <= rep["recommendation"]["触发罚分_后"] <= 0.6
    # 每个候选都应能把命中票挤出 top-N(top 紧凑)
    for s in rep["penalty_sweep"]:
        assert s["ejected_from_topN"] >= 0.9


def test_replay_verdict_no_future(monkeypatch):
    """回放命中裁决严格 list_date < as_of(防未来函数):上榜日=as_of 当天不触发。"""
    from tools.backtest import lhb_dose_replay as RP
    events = {"X": [{"list_date": "2026-08-20", "direction": 1, "net_buy_ratio": 5.0}]}
    axis = {"窗口天数": 7, "最小净买占比": 0.0}
    # as_of = 上榜日当天 → 盘后披露不可用 → 不触发
    assert RP._verdict(events, "X", "2026-08-20", axis) is None or \
        RP._verdict(events, "X", "2026-08-20", axis)["triggered"] is False
    # as_of = 次日 → 已公开 → 触发
    v = RP._verdict(events, "X", "2026-08-21", axis)
    assert v is not None and v["triggered"] is True


def test_forward_scorecard_incremental_idempotent(monkeypatch, tmp_path):
    """记分卡:注入命中行 + 假 K 线,回填前向收益;重跑同日幂等(不堆重复行)。"""
    from tools.backtest import lhb_forward_scorecard as FS

    def fake_record_day(date, ul):
        return pd.DataFrame([{
            "date": date, "code": "000001", "综合分": 0.5, "排序分": 0.0,
            "rank_before": 3, "rank_after": 80, "in_top20_before": True,
            "in_top20_after": False, "ejected": True, "net_buy_ratio": 6.0,
            "reason": "净买上榜"}], columns=FS._COLS)

    # 假 K 线:code 000001,as_of=2026-08-20 有下标,T+1 开盘、T+5 收盘齐(收益可算)
    dates = pd.date_range("2026-08-18", periods=10, freq="D")
    kdf = pd.DataFrame({"date": dates, "open": np.linspace(10, 10.9, 10),
                        "high": 11.0, "low": 9.0, "close": np.linspace(10, 9.5, 10)})
    monkeypatch.setattr(FS, "_record_day", fake_record_day)
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline_recent", lambda code: kdf)

    out = tmp_path / "sc.csv"
    df1 = FS.update("2026-08-20", out=str(out), universe_limit=50, horizons=(1, 5))
    df2 = FS.update("2026-08-20", out=str(out), universe_limit=50, horizons=(1, 5))
    assert len(df1) == 1 and len(df2) == 1               # 幂等:重跑不堆行
    assert "r_1" in df2.columns and "r_5" in df2.columns
    assert df2["r_5"].notna().all()                       # T+5 已到期 → 回填
    # close 下行 → 前向收益<0 → 见光死证实标记=1
    assert float(df2["underperf_5"].iloc[0]) == 1.0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
