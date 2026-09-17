"""H1-F1 收盘闭环「有声降级」单测:骨架步一崩,闭环不中止 + 高可见告警落盘,绝不静默。

锁的语义(为什么这么写,防未来重写误删):
  · 09-14~17 收盘 run_screen_all 的 enrich_candidates 及其后 7 步(collect_ticks/serialize/events/
    factor/council/panel/screen)**全部裸调**,单步一崩(launchd fd 软限 256 → EMFILE)整条闭环
    中止且**无任何显式信号**,per-stock 记录退化为午盘版。F1 给这 8 步套 _safe_gap。
  · 断言三件事:① 某骨架步抛 OSError(Errno 24)→ 闭环**继续**(后续骨架步 + 收尾 run_multi_gate 仍被调);
    ② data/analysis/<D>/_gap_alarm.json **生成**且含失败步名/errno/时间;③ 降级**不静默**(有落盘留痕)。
"""
import json

import pytest

from tools import run
from tools.pipeline import (screen_conditional_rank, screen_council, screen_deduct_quality,
                            screen_max_range, screen_momentum, screen_reversal_turnover,
                            screen_s02, screen_semi_factor, screen_strong, screen_volume)


@pytest.fixture(autouse=True)
def _isolate(analysis_tmpdir):
    """analysis 落盘根切到 tmp(_gap_alarm.json 落这里,可回读);见 conftest.analysis_tmpdir。"""


def _stub_all_screeners(monkeypatch):
    """9 条在产 screener 全桩成「选出 1 只」的小假 view(避免真跑/触网/落盘)。"""
    def mk(view_key, codes):
        def _f(codes_all, as_of=None, fetch=True, **k):
            return {view_key: [{"code": c} for c in codes]}
        return _f
    monkeypatch.setattr(screen_council, "run_council_screen", mk("top", ["C1"]))
    for mod, fname in ((screen_s02, "run_s02_screen"),
                       (screen_momentum, "run_momentum_screen"),
                       (screen_semi_factor, "run_semi_factor_screen"),
                       (screen_max_range, "run_max_range_screen"),
                       (screen_volume, "run_volume_screen"),
                       (screen_strong, "run_strong_screen"),
                       (screen_reversal_turnover, "run_reversal_turnover_screen"),
                       (screen_conditional_rank, "run_conditional_rank_screen"),
                       (screen_deduct_quality, "run_deduct_quality_screen")):
        monkeypatch.setattr(mod, fname, mk("入选清单", []))


def _stub_stage2(monkeypatch, calls, raise_step=None, exc=None):
    """阶段② 骨架步 + 收尾步全桩成计数桩;raise_step 指定的那步抛 exc(测有声降级)。"""
    monkeypatch.setattr(run.master_sync, "sync_master", lambda codes, as_of=None: {"ok": len(codes)})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: [])

    def rec(name):
        def _f(*a, **k):
            calls.append(name)
            if name == raise_step:
                raise exc
        return _f

    def _enrich(codes, as_of, no_llm=False):
        calls.append("enrich_candidates")
        if raise_step == "enrich_candidates":
            raise exc
        return {"candidates": len(codes), "summary": "stub"}

    for fn in ("collect_values_missing", "collect_market_context", "collect_ticks",
               "run_serialize", "run_events", "run_factor", "run_council",
               "run_panel", "run_screen", "collect_lhb", "_update_lhb_scorecard",
               "run_backtest", "run_multi_gate", "_push_incremental"):
        monkeypatch.setattr(run, fn, rec(fn))
    monkeypatch.setattr(run, "enrich_candidates", _enrich)


def test_skeleton_step_failure_degrades_audibly_and_continues(monkeypatch, analysis_tmpdir):
    """run_serialize 抛 EMFILE → 闭环继续(后续骨架步 + run_multi_gate 仍跑)+ _gap_alarm.json 生成。"""
    calls: list[str] = []
    boom = OSError(24, "Too many open files")
    _stub_all_screeners(monkeypatch)
    _stub_stage2(monkeypatch, calls, raise_step="run_serialize", exc=boom)

    out = run.run_screen_all(["A"], "2026-09-18")

    # ① 闭环未中止:崩点(serialize)之后的骨架步 + 收尾步仍被调用
    assert "run_serialize" in calls          # 崩点自身进入过
    assert "run_events" in calls             # 崩点之后的骨架步照跑
    assert "run_panel" in calls
    assert "run_screen" in calls
    assert "run_multi_gate" in calls         # 收尾闸门照跑
    assert out["as_of"] == "2026-09-18"

    # ② 告警落盘且不静默:_gap_alarm.json 生成,记录失败步名 + errno
    marker = analysis_tmpdir / "2026-09-18" / "_gap_alarm.json"
    assert marker.exists(), "降级必须落 _gap_alarm.json,绝不静默"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    steps = payload["steps"]
    assert any(st["step"] == "record 序列化" for st in steps)
    rec0 = next(st for st in steps if st["step"] == "record 序列化")
    assert rec0["errno"] == 24
    assert rec0["error_type"] == "OSError"


def test_no_failure_no_alarm(monkeypatch, analysis_tmpdir):
    """全步正常 → 不生成 _gap_alarm.json(无假阳性告警)。"""
    calls: list[str] = []
    _stub_all_screeners(monkeypatch)
    _stub_stage2(monkeypatch, calls, raise_step=None, exc=None)

    run.run_screen_all(["A"], "2026-09-18")

    assert not (analysis_tmpdir / "2026-09-18" / "_gap_alarm.json").exists()


def test_multiple_step_failures_accumulate(monkeypatch, analysis_tmpdir):
    """多步降级累积进同一 _gap_alarm.json 的 steps 数组,不互相覆盖。"""
    calls: list[str] = []
    boom = OSError(24, "Too many open files")
    _stub_all_screeners(monkeypatch)
    # 让 factor + panel 两步都崩
    monkeypatch.setattr(run.master_sync, "sync_master", lambda codes, as_of=None: {"ok": len(codes)})
    monkeypatch.setattr(run.stock_pool, "get_codes", lambda: [])

    def rec(name):
        def _f(*a, **k):
            calls.append(name)
            if name in ("run_factor", "run_panel"):
                raise boom
        return _f

    for fn in ("collect_values_missing", "collect_market_context", "collect_ticks",
               "run_serialize", "run_events", "run_factor", "run_council",
               "run_panel", "run_screen", "collect_lhb", "_update_lhb_scorecard",
               "run_backtest", "run_multi_gate", "_push_incremental"):
        monkeypatch.setattr(run, fn, rec(fn))
    monkeypatch.setattr(run, "enrich_candidates",
                        lambda codes, as_of, no_llm=False: {"candidates": 0, "summary": ""})

    run.run_screen_all(["A"], "2026-09-18")

    payload = json.loads((analysis_tmpdir / "2026-09-18" / "_gap_alarm.json").read_text(encoding="utf-8"))
    names = {st["step"] for st in payload["steps"]}
    assert {"多因子预计算", "panel 组装"} <= names
    assert len(payload["steps"]) == 2
