"""统一风控 veto 汇聚器语义锁(WI-6 Phase 3)。

锁死「财报红旗(质量轴)+ 龙虎榜否决(微结构轴)→ 同一否决/降权出口(正交 OR 合成)」的语义,
防未来 prompt/代码重写时被无意改坏:
  1. OR 合成:任一轴触发即否决/降权;两轴降权罚分求和后封顶。
  2. 单轴触发:仅红旗触发 / 仅龙虎榜触发 各自正确。
  3. 两轴叠加:剂量累加、封顶;否决 OR、剔除 OR。
  4. 向后兼容红线:无龙虎榜数据(verdict=None/未触发)→ 排序结果与 redflag_adjust 完全一致。
  5. 总开关:风控汇聚.启用=False → 龙虎榜轴关停、财报轴不受影响(红旗现状不回归)。
  6. 符号正确性:负分风险票被压更低,绝不被抬高。
  7. 防未来函数:risk_veto_adjust 是入参纯变换,同入参同出参,不触数据/网络。
  8. web 消费侧:_rerank_scored / _demote_flagged 读 record['lhb_veto'] 生效(龙虎榜票沉底)。

⚠️ 非投资建议:风控层只改展示排序/入选。
"""
from __future__ import annotations

import pytest

from tools.config import strategy as strategy_cfg
from web import data_access as da


# —— 各轴 config(测试固定,免受默认漂移影响)——
RF_DOWN = {"启用": True, "模式": "降权", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}
RF_VETO = {"启用": True, "模式": "否决", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}
RF_OFF = {"启用": False, "模式": "降权", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}

AGG_ON = {"启用": True, "罚分上限": 1.5,
          "龙虎榜": {"启用": True, "模式": "降权", "触发罚分": 0.6, "按条数加权": False,
                    "条数上限": 3, "否决沉底保留展示": True}}
AGG_VETO = {"启用": True, "罚分上限": 1.5,
            "龙虎榜": {"启用": True, "模式": "否决", "触发罚分": 0.6, "按条数加权": False,
                      "条数上限": 3, "否决沉底保留展示": True}}
AGG_VETO_DROP = {"启用": True, "罚分上限": 1.5,
                 "龙虎榜": {"启用": True, "模式": "否决", "触发罚分": 0.6, "否决沉底保留展示": False}}
AGG_WEIGHTED = {"启用": True, "罚分上限": 3.0,   # 抬高合成封顶,单测「按条数加权」由条数上限主导
                "龙虎榜": {"启用": True, "模式": "降权", "触发罚分": 0.6, "按条数加权": True,
                          "条数上限": 3, "否决沉底保留展示": True}}
AGG_OFF = {"启用": False, "罚分上限": 1.5,
           "龙虎榜": {"启用": True, "模式": "降权", "触发罚分": 0.6}}


def _trig(reason="净买上榜", n=1):
    return {"triggered": True, "reason": reason, "n_recent": n}


NOTRIG = {"triggered": False, "reason": "近7日无净买上榜", "n_recent": 0}


@pytest.fixture(autouse=True)
def _fix_redflag(monkeypatch):
    """默认把财报轴口径固定为 RF_DOWN,单测按需覆盖。"""
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)


# ———————————— 1. OR 合成 ————————————
def test_or_either_axis_triggers_downweight():
    # 仅龙虎榜(无红旗)
    a = strategy_cfg.risk_veto_adjust(0.30, 0, _trig(), AGG_ON)
    assert a["应用"] and a["排序分"] == pytest.approx(0.30 - 0.6)
    # 仅红旗(无龙虎榜)
    b = strategy_cfg.risk_veto_adjust(0.30, 1, None, AGG_ON)
    assert b["应用"] and b["排序分"] == pytest.approx(0.30 - 0.5)


def test_or_both_axes_penalty_sums():
    a = strategy_cfg.risk_veto_adjust(0.30, 1, _trig(), AGG_ON)
    # 红旗 0.5 + 龙虎榜 0.6 = 1.1(<封顶 1.5)
    assert a["罚分"] == pytest.approx(1.1)
    assert a["排序分"] == pytest.approx(0.30 - 1.1)
    assert set(("财报", "龙虎榜")).issubset(a["各轴"].keys())
    assert a["各轴"]["财报"]["应用"] and a["各轴"]["龙虎榜"]["应用"]


def test_penalty_capped_by_agg_ceiling():
    # 红旗 3 面(0.5×3=1.5 但红旗自身封顶 1.2)+ 龙虎榜 0.6 = 1.8 → 合成封顶 1.5
    a = strategy_cfg.risk_veto_adjust(0.30, 3, _trig(), AGG_ON)
    assert a["罚分"] == pytest.approx(1.5)
    assert a["排序分"] == pytest.approx(0.30 - 1.5)


# ———————————— 2/3. 否决 OR / 剔除 OR ————————————
def test_veto_or_from_lhb_axis(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)  # 红旗降权(不否决)
    a = strategy_cfg.risk_veto_adjust(0.30, 1, _trig(), AGG_VETO)      # 龙虎榜否决
    assert a["否决"] is True and a["模式"] == "否决"
    # 否决轴不减分,只降权轴(红旗 0.5)减;龙虎榜否决靠标记沉底
    assert a["排序分"] == pytest.approx(0.30 - 0.5)


def test_veto_or_from_redflag_axis(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_VETO)  # 红旗否决
    a = strategy_cfg.risk_veto_adjust(0.30, 1, _trig(), AGG_ON)        # 龙虎榜降权
    assert a["否决"] is True
    assert a["排序分"] == pytest.approx(0.30 - 0.6)                    # 只龙虎榜降权减分


def test_drop_or_from_lhb(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)
    a = strategy_cfg.risk_veto_adjust(0.30, 0, _trig(), AGG_VETO_DROP)
    assert a["剔除"] is True and a["否决"] is True


# ———————————— 按条数加权 ————————————
def test_lhb_weighted_by_n_recent():
    a = strategy_cfg.risk_veto_adjust(0.30, 0, _trig(n=2), AGG_WEIGHTED)
    assert a["罚分"] == pytest.approx(0.6 * 2)          # 0.6 × min(2,3)
    b = strategy_cfg.risk_veto_adjust(0.30, 0, _trig(n=9), AGG_WEIGHTED)
    assert b["罚分"] == pytest.approx(0.6 * 3)          # 条数上限 3


# ———————————— 4. 向后兼容:无龙虎榜=红旗现状 ————————————
@pytest.mark.parametrize("base,dose", [(0.30, 0), (0.30, 1), (0.30, 3),
                                       (-0.30, 1), (None, 0), (None, 2)])
def test_no_lhb_equals_redflag(base, dose):
    """lhb_verdict=None 且财报轴同口径 → 排序分/应用/否决/剔除 与 redflag_adjust 完全一致(不回归)。"""
    agg = strategy_cfg.risk_veto_adjust(base, dose, None, AGG_ON)
    rf = strategy_cfg.redflag_adjust(base, dose, RF_DOWN)
    assert agg["排序分"] == pytest.approx(rf["排序分"]) if rf["排序分"] is not None else True
    assert agg["应用"] == rf["应用"]
    assert agg["否决"] == rf["否决"]
    assert agg["剔除"] == rf["剔除"]


def test_not_triggered_verdict_equals_none():
    a = strategy_cfg.risk_veto_adjust(0.30, 1, NOTRIG, AGG_ON)
    b = strategy_cfg.risk_veto_adjust(0.30, 1, None, AGG_ON)
    assert a["排序分"] == b["排序分"] and a["应用"] == b["应用"]


# ———————————— 5. 总开关:关停龙虎榜轴,财报轴不回归 ————————————
def test_master_switch_off_disables_lhb_only():
    a = strategy_cfg.risk_veto_adjust(0.30, 1, _trig(), AGG_OFF)
    # 龙虎榜轴关停 → 只红旗降权 0.5
    assert a["排序分"] == pytest.approx(0.30 - 0.5)
    assert a["各轴"]["龙虎榜"]["应用"] is False
    assert a["各轴"]["财报"]["应用"] is True


def test_lhb_axis_off_but_redflag_on():
    agg = {"启用": True, "罚分上限": 1.5, "龙虎榜": {"启用": False, "模式": "降权", "触发罚分": 0.6}}
    a = strategy_cfg.risk_veto_adjust(0.30, 1, _trig(), agg)
    assert a["排序分"] == pytest.approx(0.30 - 0.5)   # 只红旗


# ———————————— 6. 符号正确性 ————————————
def test_sign_correctness_negative_not_promoted():
    base = -0.30
    a = strategy_cfg.risk_veto_adjust(base, 0, _trig(), AGG_ON)
    assert a["排序分"] < base                          # -0.30-0.6=-0.90 < -0.30
    assert a["排序分"] != base * 0.5                   # 反例守卫:乘系数会抬高


def test_none_base_uses_negative_penalty():
    a = strategy_cfg.risk_veto_adjust(None, 0, _trig(), AGG_ON)
    assert a["排序分"] == pytest.approx(-0.6)          # 无打分区块:−合成罚分
    b = strategy_cfg.risk_veto_adjust(None, 1, _trig(), AGG_ON)
    assert b["排序分"] == pytest.approx(-1.1)          # 红旗 0.5 + 龙虎榜 0.6


# ———————————— 7. 纯函数(防未来函数)————————————
def test_pure_same_input_same_output():
    a = strategy_cfg.risk_veto_adjust(0.1, 1, _trig(n=2), AGG_ON)
    b = strategy_cfg.risk_veto_adjust(0.1, 1, _trig(n=2), AGG_ON)
    assert a == b


# ———————————— 8. web 消费侧:record['lhb_veto'] 生效 ————————————
def _row(code, score, high_flags=None):
    risk = {"high_risk": True, "flags": list(high_flags)} if high_flags else None
    return {"code": code, "council_score": score, "risk": risk}


def test_web_rerank_lhb_sinks_row(monkeypatch):
    """龙虎榜净买上榜票(record.lhb_veto.triggered)在自选/策略0 排序里降权沉底。"""
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)
    monkeypatch.setattr(strategy_cfg, "risk_veto_cfg", lambda: AGG_ON)
    rows = [_row("LHB1", 0.30), _row("CLEAN", 0.10), _row("OK2", 0.05)]
    recs = {"LHB1": {"lhb_veto": _trig()}, "CLEAN": {}, "OK2": {}}
    out = [r["code"] for r in da._rerank_scored(rows, recs)]
    # LHB1 降权后 0.30-0.6=-0.30 < 0.10/0.05 → 沉到干净票之下
    assert out[0] == "CLEAN"
    assert out.index("LHB1") == len(out) - 1


def test_web_rerank_no_lhb_data_no_regression(monkeypatch):
    """无 lhb_veto 块(旧记录)→ 与红旗现状一致(按分降序)。"""
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)
    monkeypatch.setattr(strategy_cfg, "risk_veto_cfg", lambda: AGG_ON)
    rows = [_row("A", 0.20), _row("B", 0.10)]
    out = [r["code"] for r in da._rerank_scored(rows, {"A": {}, "B": {}})]
    assert out == ["A", "B"]


def test_web_demote_lhb_sinks_in_unscored(monkeypatch):
    """无打分区块:龙虎榜触发票稳定沉底,干净票原序保留。"""
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)
    monkeypatch.setattr(strategy_cfg, "risk_veto_cfg", lambda: AGG_ON)
    rows = [_row("LHB1", None), _row("C1", None), _row("C2", None)]
    recs = {"LHB1": {"lhb_veto": _trig()}, "C1": {}, "C2": {}}
    out = [r["code"] for r in da._demote_flagged(rows, recs)]
    assert out[:2] == ["C1", "C2"] and out[2] == "LHB1"


def test_web_veto_drop_removes_lhb(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: RF_DOWN)
    monkeypatch.setattr(strategy_cfg, "risk_veto_cfg", lambda: AGG_VETO_DROP)
    rows = [_row("LHB1", 0.30), _row("CLEAN", 0.10)]
    recs = {"LHB1": {"lhb_veto": _trig()}, "CLEAN": {}}
    out = [r["code"] for r in da._rerank_scored(rows, recs)]
    assert out == ["CLEAN"]                            # 否决·不保留展示 → LHB1 剔除


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
