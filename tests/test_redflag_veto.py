"""财报高危红旗接入选股逻辑(降权 / 否决)语义锁(WI-6 升级)。

锁死"红旗从仅展示 → 进排序/入选"的语义,防未来 prompt/代码重写时被无意删掉:
  1. council.redflag_penalty:dose 单调 + 封顶。
  2. council.redflag_adjust:降权=原分−罚分、否决=沉底标记、禁用=no-op;
     **符号正确性红线**——负分风险票被压更低,绝不被"抬高"(乘系数陷阱的反例)。
  3. web._rerank_scored:高危红旗票沉底;干净票相对序不变;无 council_score 仍沉底(不回归)。
  4. web._demote_flagged:高危票稳定沉底(dose 大者更靠后),干净票原序保留。
  5. 暴雷票入选红线:正 council_score 的"风险+高危红旗"票,排到更低分的干净票之下。
  6. 防未来函数:redflag_adjust 是入参的纯变换,不触任何记录/网络。
  7. 禁用(启用=False)→ 全链路 no-op,正常票排序不回归。

⚠️ 非投资建议:红旗为风险/警示层,降权/否决只改展示排序。
"""
from __future__ import annotations

import pytest

from tools.analysis import council            # 再导出源(council.redflag_* → config 单一真源)
from tools.config import strategy as strategy_cfg
from web import data_access as da


DOWN = {"启用": True, "模式": "降权", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}
VETO = {"启用": True, "模式": "否决", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}
VETO_DROP = {"启用": True, "模式": "否决", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": False}
OFF = {"启用": False, "模式": "降权", "每面罚分": 0.5, "罚分上限": 1.2, "否决沉底保留展示": True}


# ---------- 1. 罚分:dose 单调 + 封顶 ----------
def test_penalty_zero_when_no_flag():
    assert council.redflag_penalty(0, DOWN) == 0.0
    assert council.redflag_penalty(None, DOWN) == 0.0


def test_penalty_monotonic_and_capped():
    p1 = council.redflag_penalty(1, DOWN)
    p2 = council.redflag_penalty(2, DOWN)
    p3 = council.redflag_penalty(10, DOWN)
    assert p1 == 0.5 and p2 == 1.0
    assert p2 > p1                      # dose 单调:面越多罚越重
    assert p3 == 1.2                    # 封顶(0.5×10=5 → cap 1.2)


# ---------- 2. redflag_adjust:降权/否决/禁用 + 符号正确性 ----------
def test_adjust_downweight_subtracts():
    info = council.redflag_adjust(0.30, 1, DOWN)
    assert info["应用"] and info["模式"] == "降权"
    assert info["排序分"] == pytest.approx(0.30 - 0.5)   # 原分−罚分
    assert info["否决"] is False and info["剔除"] is False


def test_adjust_sign_correctness_negative_not_promoted():
    """符号红线:负分风险票必须被压更低(减罚分),绝不能像'乘<1系数'那样被抬高。"""
    base = -0.30
    info = council.redflag_adjust(base, 1, DOWN)
    assert info["排序分"] < base                       # -0.30-0.5=-0.80 < -0.30
    # 反例守卫:若误用乘系数 base*0.5=-0.15 会 > base(被抬高),此断言锁死不可发生
    assert info["排序分"] != base * 0.5


def test_adjust_veto_marks_sink_not_change_score():
    info = council.redflag_adjust(0.30, 2, VETO)
    assert info["否决"] is True and info["剔除"] is False
    assert info["排序分"] == 0.30                       # 否决不改分,靠 否决 标记沉底
    info_drop = council.redflag_adjust(0.30, 1, VETO_DROP)
    assert info_drop["剔除"] is True                    # 否决·不保留展示 → 剔除


def test_adjust_disabled_is_noop():
    info = council.redflag_adjust(0.30, 3, OFF)
    assert info["应用"] is False and info["罚分"] == 0.0
    assert info["排序分"] == 0.30 and info["否决"] is False


def test_adjust_no_flag_is_noop_even_enabled():
    info = council.redflag_adjust(0.30, 0, DOWN)
    assert info["应用"] is False and info["排序分"] == 0.30


def test_adjust_none_base_uses_negative_penalty():
    """无打分区块:base=None → 排序分=−罚分(clean=0、越多面越负),供稳定沉底。"""
    assert council.redflag_adjust(None, 0, DOWN)["排序分"] == 0.0
    assert council.redflag_adjust(None, 1, DOWN)["排序分"] == -0.5
    assert council.redflag_adjust(None, 2, DOWN)["排序分"] == -1.0


def test_adjust_is_pure_no_side_channel():
    """防未来函数:adjust 只吃 (base, count, cfg),不读任何记录/网络 → 同入参同出参。"""
    a = council.redflag_adjust(0.1, 1, DOWN)
    b = council.redflag_adjust(0.1, 1, DOWN)
    assert a == b


# ---------- 3/5. web._rerank_scored:高危沉底 + 暴雷票入选红线 ----------
def _row(code, score, high_flags):
    """构造展示行(risk 预挂,模拟 _attach_risk 产出;high_flags=高危红旗名列表或None)。"""
    risk = {"high_risk": True, "flags": list(high_flags)} if high_flags else None
    return {"code": code, "council_score": score, "risk": risk}


def test_rerank_downweight_sinks_high_risk(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: DOWN)
    rows = [
        _row("RISK1", 0.30, ["扣非为负"]),   # 高危但分高
        _row("CLEAN", 0.10, None),           # 干净、分较低
        _row("OK2", 0.05, None),
    ]
    out = da._rerank_scored(rows, {})
    codes = [r["code"] for r in out]
    # 暴雷票入选红线:RISK1 降权后 0.30-0.5=-0.20 < 0.10/0.05 → 排到干净票之下
    assert codes.index("CLEAN") < codes.index("RISK1")
    assert codes.index("OK2") < codes.index("RISK1")
    assert out[0]["code"] == "CLEAN"


def test_rerank_clean_order_and_none_sink(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: DOWN)
    rows = [
        _row("A", 0.20, None),
        _row("B", 0.10, None),
        _row("NOSCORE", None, None),         # 无 council_score → 沉底(不回归)
    ]
    out = [r["code"] for r in da._rerank_scored(rows, {})]
    assert out == ["A", "B", "NOSCORE"]      # 干净票按分降序;None 沉底


def test_rerank_disabled_no_regression(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: OFF)
    rows = [
        _row("RISK1", 0.30, ["扣非为负"]),
        _row("CLEAN", 0.10, None),
    ]
    out = [r["code"] for r in da._rerank_scored(rows, {})]
    assert out == ["RISK1", "CLEAN"]         # 禁用 → 原按分降序,不受红旗影响


def test_rerank_veto_drop_removes(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: VETO_DROP)
    rows = [_row("RISK1", 0.30, ["扣非为负"]), _row("CLEAN", 0.10, None)]
    out = [r["code"] for r in da._rerank_scored(rows, {})]
    assert out == ["CLEAN"]                  # 否决·不保留展示 → RISK1 被剔除


# ---------- 4. web._demote_flagged:无打分区块稳定沉底 ----------
def test_demote_flagged_stable_sink(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: DOWN)
    rows = [
        _row("R2", None, ["扣非为负", "非标审计意见"]),  # 2 面 → 沉最底
        _row("C1", None, None),
        _row("R1", None, ["扣非为负"]),                  # 1 面
        _row("C2", None, None),
    ]
    out = [r["code"] for r in da._demote_flagged(rows, {})]
    assert out[:2] == ["C1", "C2"]           # 干净票保持原并集顺序在前
    assert out[2] == "R1" and out[3] == "R2"  # 高危按 dose:1 面在 2 面之前(dose 大者更沉)


def test_demote_disabled_no_regression(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: OFF)
    rows = [_row("R1", None, ["扣非为负"]), _row("C1", None, None)]
    out = [r["code"] for r in da._demote_flagged(rows, {})]
    assert out == ["R1", "C1"]               # 禁用 → 并集原序不变


def test_demote_attaches_risk_adjust(monkeypatch):
    monkeypatch.setattr(strategy_cfg, "redflag_cfg", lambda: DOWN)
    rows = [_row("R1", None, ["扣非为负"])]
    da._demote_flagged(rows, {})
    assert rows[0]["risk_adjust"]["应用"] is True
    assert rows[0]["risk_adjust"]["高危数"] == 1
