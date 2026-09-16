"""B veto_track 单测:锁住基本面 veto 误杀 forward-shadow 的硬语义（约法6），
防未来 prompt/代码重写删规则。

锁的"为什么改"：
  ① non-gating 纯记录 —— advisory 恒带 non_gating/非validated=True，不动 live veto。
  ② 交集口径 —— veto票 = 价量强(量价放量/最强选股) ∩ 被 veto/超买规避(多策略命中闸门)。
  ③ veto 原因抽取 —— veto 应用/罚分 + 过热闸 触发/沉底/剔除（超买共振轴）都进 veto原因。
  ④ 前向绝对收益口径 —— r_N == close[idx+N]/close[idx]-1（canonical），未到期留 null。
  ⑤ 回填自愈 —— 到期后 --backfill 就地填 None cell，幂等；summary 诚实报 N（<120 不下结论）。
  ⑥ 交易日守卫 + 幂等 —— 非交易日不落盘；同日重跑不覆盖（除非 --force）。

全程合成视图/闸门 + monkeypatch K线（不触网、不依赖生产数据）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.research import veto_track as B

DATE = "2026-09-11"
FUT = ["2026-09-12", "2026-09-15", "2026-09-16"]   # D+1..D+3（合成交易日）


def _write_views(root: Path, date: str, 量价: list[str], 最强: list[str], 闸门票: list[dict]):
    d = root / "analysis" / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "量价放量.json").write_text(json.dumps(
        {"入选清单": [{"code": c, "组合": ["单日放量"]} for c in 量价]}, ensure_ascii=False),
        encoding="utf-8")
    (d / "最强选股.json").write_text(json.dumps(
        {"入选清单": [{"code": c} for c in 最强]}, ensure_ascii=False), encoding="utf-8")
    (d / "多策略命中闸门.json").write_text(json.dumps(
        {"启用": True, "票": 闸门票}, ensure_ascii=False), encoding="utf-8")


def _ticket(code, *, veto应用=False, 罚分=0.0, 归因=None, 过热触发=False, 超买=False):
    return {
        "code": code,
        "veto": {"应用": veto应用, "剔除": False, "否决": False,
                 "归因": 归因 or [], "罚分": 罚分},
        "过热闸": {"触发": 过热触发, "沉底": False, "剔除": False, "原因": [],
                 "轴": {"超买共振": 超买, "基本面空心": False, "涨幅透支": False,
                       "换手极端": False, "事件博弈": False}},
    }


def _mock_kline(monkeypatch, prices: dict):
    """monkeypatch _kline_index：注入 {code: [close...]}，date2idx 用合成交易日历。"""
    cal = [DATE] + FUT
    d2i = {d: i for i, d in enumerate(cal)}

    def fake(code, cache):
        return (prices.get(code), dict(d2i))
    monkeypatch.setattr(B, "_kline_index", fake)


# ── ② 交集口径 + ③ veto 原因 ─────────────────────────────────────────────────
def test_cross_and_reasons(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _write_views(root, DATE,
                 量价=["A", "B", "C"], 最强=["D"],
                 闸门票=[
                     _ticket("A", veto应用=True, 罚分=1.0, 归因=["财报高危红旗×2"]),  # 价量强∩veto
                     _ticket("B", 过热触发=True, 超买=True),                          # 价量强∩超买
                     _ticket("C"),                                                     # 无veto→不计
                     _ticket("X", veto应用=True, 罚分=1.0),                            # veto但非价量强→不计
                     _ticket("D", veto应用=True, 罚分=0.5, 归因=["业绩预告-54%"]),     # 最强选股∩veto
                 ])
    _mock_kline(monkeypatch, {"A": [10, 11, 12, 9], "B": [20, 19, 18, 21], "D": [5, 5.5, 6, 6]})
    adv = B.run_daily_shadow(DATE, str(tmp_path / "out"), data_root=str(root))
    assert adv["non_gating"] is True and adv["非validated"] is True
    codes = {v["code"] for v in adv["veto票"]}
    assert codes == {"A", "B", "D"}             # C(无veto)/X(非价量强) 排除
    by = {v["code"]: v for v in adv["veto票"]}
    assert "财报高危红旗×2" in by["A"]["veto原因"][0]
    assert "超买共振" in by["B"]["veto原因"][0]   # 过热闸轴
    assert by["A"]["价量命中"] == ["量价放量"]
    assert by["D"]["价量命中"] == ["最强选股"]


# ── ④ 前向绝对收益 canonical ─────────────────────────────────────────────────
def test_forward_return_canonical(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _write_views(root, DATE, 量价=["A"], 最强=[],
                 闸门票=[_ticket("A", veto应用=True, 罚分=1.0, 归因=["红旗"])])
    _mock_kline(monkeypatch, {"A": [10.0, 11.0, 13.0, 9.0]})   # D=10
    adv = B.run_daily_shadow(DATE, str(tmp_path / "out"), data_root=str(root))
    lab = adv["veto票"][0]["labels"]
    assert lab["close_0"] == 10.0
    assert abs(lab["r_1"] - 10.0) < 1e-6         # 11/10-1 = +10%
    assert abs(lab["r_2"] - 30.0) < 1e-6         # 13/10-1 = +30%


def test_pending_when_未到期(tmp_path, monkeypatch):
    root = tmp_path / "data"
    _write_views(root, DATE, 量价=["A"], 最强=[],
                 闸门票=[_ticket("A", veto应用=True, 罚分=1.0)])
    _mock_kline(monkeypatch, {"A": [10.0]})       # 只有 D，无 D+1/D+2 → 留 null
    adv = B.run_daily_shadow(DATE, str(tmp_path / "out"), data_root=str(root))
    lab = adv["veto票"][0]["labels"]
    assert lab["r_1"] is None and lab["r_2"] is None
    assert adv["label_status"] == "pending"


# ── ⑤ 回填自愈 + summary 报 N ────────────────────────────────────────────────
def test_backfill_and_summary(tmp_path, monkeypatch):
    root = tmp_path / "data"
    out = tmp_path / "out"
    _write_views(root, DATE, 量价=["A"], 最强=[],
                 闸门票=[_ticket("A", veto应用=True, 罚分=1.0)])
    _mock_kline(monkeypatch, {"A": [10.0]})       # 落盘时未到期
    B.run_daily_shadow(DATE, str(out), data_root=str(root))
    assert B._load_json(str(out / f"{DATE}.json"))["label_status"] == "pending"
    # 到期后回填
    monkeypatch.setattr("tools.analysis.market_forecast.dataroot.ensure_data_root",
                        lambda *a, **k: None, raising=False)
    _mock_kline(monkeypatch, {"A": [10.0, 12.0, 11.0]})
    r = B.backfill_labels(str(out))
    assert r["n_filled"] == 2
    adv = B._load_json(str(out / f"{DATE}.json"))
    assert adv["label_status"] == "settled"
    assert abs(adv["veto票"][0]["labels"]["r_1"] - 20.0) < 1e-6
    summ = B.summarize(str(out))
    assert summ["样本充足"] is False              # <120 诚实报 N
    assert summ["汇总"]["r_1"]["n_settled"] == 1


# ── ⑥ 交易日守卫 + 幂等 ──────────────────────────────────────────────────────
def test_trading_guard_and_idempotent(tmp_path, monkeypatch):
    from tools.collectors import calendar as cal
    root = tmp_path / "data"
    out = tmp_path / "out"
    _write_views(root, DATE, 量价=["A"], 最强=[],
                 闸门票=[_ticket("A", veto应用=True, 罚分=1.0)])
    _mock_kline(monkeypatch, {"A": [10.0, 11.0, 12.0]})
    # 非交易日不落盘
    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    assert B.main(["--date", DATE, "--out-dir", str(out), "--data-root", str(root)]) == 0
    assert not (out / f"{DATE}.json").exists()
    # 交易日：落盘后幂等
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    assert B.main(["--date", DATE, "--out-dir", str(out), "--data-root", str(root)]) == 0
    p = out / f"{DATE}.json"
    m1 = p.stat().st_mtime_ns
    assert B.main(["--date", DATE, "--out-dir", str(out), "--data-root", str(root)]) == 0
    assert p.stat().st_mtime_ns == m1
