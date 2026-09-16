"""午盘 Q · M2 · 选股 pipeline 契约测试。

覆盖:
    · 非交易日 / 无效 stage
    · 上游 gate/snapshot 缺失
    · 三段累加(1430 → 1450 → final)
    · gate.final 空仓 → 强制清空 final
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.analysis.midday_q import gate as G
from tools.config import midday_q_universe as UNIV
from tools.pipeline import midday_q_gate as PG
from tools.pipeline import midday_q_screen as PS


def _write_gate(tmp_path: Path, date: str, *,
                 stage1=None, stage2=None, final=None, flipped=False):
    """写一份合成 gate JSON 到 tmp_path/data/analysis/midday_q/gate_<date>.json。"""
    gate_dir = tmp_path / "data" / "analysis" / "midday_q"
    gate_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"date": date}
    if stage1: data["stage1_1430"] = stage1
    if stage2: data["stage2_1450"] = stage2
    if final:  data["final"] = final
    data["flipped"] = flipped
    (gate_dir / f"gate_{date}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_snapshot(tmp_path: Path, date: str, slot: str, codes: dict):
    """写一份合成 intraday snapshot。"""
    p = tmp_path / "data" / "intraday" / date
    p.mkdir(parents=True, exist_ok=True)
    (p / f"T{slot}.json").write_text(json.dumps({
        "date": date, "slot": slot, "codes": codes, "indices": {}, "errors": [],
    }, ensure_ascii=False), encoding="utf-8")


def _install_snapshot_reader(monkeypatch, tmp_path: Path):
    """intraday_snapshot.OUT_ROOT 是模块级静态求值,monkeypatch settings 不影响它。
    → 直接替换 _load_snapshot,读 tmp_path 下的相对路径。
    """
    def _load(date: str, slot: str):
        p = tmp_path / "data" / "intraday" / date / f"T{slot}.json"
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    monkeypatch.setattr(PS, "_load_snapshot", _load)


def _install_universe(monkeypatch, tmp_path: Path,
                       focus=("600001", "600002"),
                       full=None):
    """写一份 midday_q_universe.json 并接入。"""
    full = list(full) if full else list(focus) + ["600099"]
    p = tmp_path / "config" / "midday_q_universe.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": "1.0",
        "focus_codes": list(focus),
        "codes": [{"code": c, "name": f"票{c}", "sw3": "test",
                    "in_focus": c in focus} for c in full],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(UNIV, "_STORE", p)
    UNIV.reload()


def _gate_stage(*, state="强势", allowed=("Q1",), pos=1.0):
    return {
        "date": "2026-09-07", "as_of": "14:30", "state": state,
        "indicators": {}, "allowed_strategies": list(allowed),
        "position_pct": pos, "reasons": [],
    }


# ────────────────────────────── 基础契约 ──────────────────────────────

def test_stages():
    assert PS._STAGES == ("1430", "1450", "final")


def test_screen_path_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    p = PS._screen_path("2026-09-07")
    assert p.name == "screen_2026-09-07.json"
    assert p.parent.name == "midday_q"


# ────────────────────────────── 非交易日 / 无效 stage ──────────────────────────────

def test_non_trading_day_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: False)
    assert PS.run("1430", date="2026-09-06") == 0
    assert not PS._screen_path("2026-09-06").exists()


def test_invalid_stage_returns_1():
    assert PS.run("bogus") == 1   # type: ignore[arg-type]


# ────────────────────────────── 上游缺失 ──────────────────────────────

def test_missing_gate_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    assert PS.run("1430", date="2026-09-07") == 1
    assert not PS._screen_path("2026-09-07").exists()


def test_missing_snapshot_exits_1(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage())
    assert PS.run("1430", date="2026-09-07") == 1


# ────────────────────────────── gate 段不允许 ──────────────────────────────

def test_gate_disallowed_produces_empty(monkeypatch, tmp_path):
    """gate.stage1 allowed=[] → screen.stage1 空 selections。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="崩盘", allowed=(), pos=0.0))
    # 即便 snapshot 缺也不影响,因为 allowed 空直接短路
    assert PS.run("1430", date="2026-09-07") == 0
    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    assert data["stage1_1430"]["selections"] == {}
    assert data["stage1_1430"]["final_codes"] == []


# ────────────────────────────── 幂等 ──────────────────────────────

def test_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="崩盘", allowed=(), pos=0.0))
    PS.run("1430", date="2026-09-07")
    mtime1 = PS._screen_path("2026-09-07").stat().st_mtime
    PS.run("1430", date="2026-09-07")
    mtime2 = PS._screen_path("2026-09-07").stat().st_mtime
    assert mtime1 == mtime2


# ────────────────────────────── 三段完整流程 ──────────────────────────────

def test_full_pipeline_flipped_forces_empty_final(monkeypatch, tmp_path):
    """gate.final.allowed=[] + flipped=True → screen.final 强制空清单。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)

    _install_snapshot_reader(monkeypatch, tmp_path)
    _install_universe(monkeypatch, tmp_path,
                       focus=("600001",), full=("600001",))
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="震荡", allowed=("Q1", "Q3"), pos=0.5),
                stage2=_gate_stage(state="弱势", allowed=(), pos=0.0),
                final={"state": "弱势", "allowed_strategies": [], "position_pct": 0.0,
                        "note": "翻转 - 震荡 → 弱势,撤单"},
                flipped=True)
    # 放一只无法被任何策略入选的票(所有信号都缺 → screener 直接跳过)
    dummy_codes = {"600001": {"name": "A", "price": None, "open": None}}
    _write_snapshot(tmp_path, "2026-09-07", "1430", dummy_codes)
    _write_snapshot(tmp_path, "2026-09-07", "1450", dummy_codes)

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0
    assert PS.run("final", date="2026-09-07") == 0

    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    assert data["final"]["final_codes"] == []
    assert data["final"]["position_pct"] == 0.0
    assert data["flipped"] is True


def test_final_needs_gate_final_and_stage2(monkeypatch, tmp_path):
    """final 阶段缺前置 → exit 1。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage())
    assert PS.run("final", date="2026-09-07") == 1


def test_pre_screen_candidates_sort():
    """振幅×成交额 排序取前 top 名。"""
    quotes = {
        "A": {"amplitude": 5.0, "amount_wan": 20000.0},
        "B": {"amplitude": 8.0, "amount_wan": 50000.0},
        "C": {"amplitude": 2.0, "amount_wan": 10000.0},
        "D": {"amplitude": None, "amount_wan": 100000.0},
    }
    picked = PS._pre_screen_candidates(quotes, top=2)
    assert picked[:2] == ["B", "A"]


# ────────────────────────────── 票池过滤(focus/full) ──────────────────────────────

def test_universe_filter_focus_default(monkeypatch, tmp_path):
    """默认用 focus 池:snapshot 里 3 只、focus 里只有 2 只 → 过滤后剩 2 只。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _install_universe(monkeypatch, tmp_path,
                       focus=("300308", "300502"),
                       full=("300308", "300502", "600099"))

    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="强势", allowed=("Q1",), pos=1.0))
    _write_snapshot(tmp_path, "2026-09-07", "1430", {
        "300308": {"name": "在focus1", "price": None},
        "300502": {"name": "在focus2", "price": None},
        "600099": {"name": "在full不在focus", "price": None},
    })

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    # 三只票没一只符合 Q1 信号,但 pipeline 应该跑成功(state=0)
    assert data["stage1_1430"]["selections"].get("Q1") == []


def test_universe_filter_full(monkeypatch, tmp_path):
    """full_universe=True → 主池 3 只全过。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _install_universe(monkeypatch, tmp_path,
                       focus=("300308",),
                       full=("300308", "300502", "600099"))

    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="强势", allowed=("Q1",), pos=1.0))
    _write_snapshot(tmp_path, "2026-09-07", "1430", {
        "300308": {"name": "A", "price": None},
        "300502": {"name": "B", "price": None},
        "600099": {"name": "C", "price": None},
    })

    assert PS.run("1430", date="2026-09-07",
                   skip_fundflow=True, full_universe=True) == 0
    # 只判定 pipeline 跑通即可(具体 selections 是空,因为 quote 没数据)


def test_universe_filter_all_out_yields_empty(monkeypatch, tmp_path):
    """snapshot 里的票全都不在票池 → 落空清单 exit 0(不 error 1,让下游看到"跑过了但空")。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _install_universe(monkeypatch, tmp_path,
                       focus=("300308",),
                       full=("300308",))

    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="强势", allowed=("Q1",), pos=1.0))
    _write_snapshot(tmp_path, "2026-09-07", "1430", {
        "999997": {"name": "无关", "price": None},
        "999998": {"name": "无关", "price": None},
    })

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    data = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))
    assert data["stage1_1430"]["final_codes"] == []
    assert "票池" in data["stage1_1430"]["note"]


def test_universe_missing_file_exits_1(monkeypatch, tmp_path):
    """票池 JSON 缺失 → exit 1(不静默继续)。"""
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    # 显式指到不存在的路径
    monkeypatch.setattr(UNIV, "_STORE", tmp_path / "nx.json")
    UNIV.reload()

    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(state="强势", allowed=("Q1",), pos=1.0))
    _write_snapshot(tmp_path, "2026-09-07", "1430",
                     {"300308": {"name": "A", "price": None}})

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 1


# ──────────────── 数据覆盖率闸门 / confirm_from 降级(2026-09-16 加) ────────────────
#
# 背景:原实现里 1450 无条件继承首判 selections 作 confirm_from。若首判因数据故障
# (快照没覆盖到票池)选出 0 只,该空集会永久锁死复核 —— 哪怕 1450 数据补全也选不出票。
# 2026-09-16 实测:首判覆盖率 4.8% → 全天 0 只。这里锁住修复语义。

def _setup_coverage_case(monkeypatch, tmp_path, focus):
    monkeypatch.setattr(PS.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(G.settings, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(PS.cal, "is_trading_day", lambda d: True)
    _install_snapshot_reader(monkeypatch, tmp_path)
    _install_universe(monkeypatch, tmp_path, focus=focus, full=focus)


def test_coverage_recorded_on_every_stage(monkeypatch, tmp_path):
    """每段落盘都带 universe_coverage / quotes_n / universe_n(可观测性)。"""
    focus = tuple(f"60000{i}" for i in range(1, 6))       # 5 只
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage(allowed=("Q1",)))
    # 5 只里给 4 只 → 覆盖率 80% ≥ 60% 门限
    _write_snapshot(tmp_path, "2026-09-07", "1430",
                     {c: {"name": f"票{c}", "price": None} for c in focus[:4]})

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    s1 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage1_1430"]
    assert s1["universe_n"] == 5
    assert s1["quotes_n"] == 4
    assert s1["universe_coverage"] == pytest.approx(0.8)
    assert s1["data_insufficient"] is False


def test_low_coverage_flags_data_insufficient(monkeypatch, tmp_path):
    """覆盖率低于门限 → 标 data_insufficient(区分'没看全'与'真无信号')。"""
    focus = tuple(f"60000{i}" for i in range(1, 6))       # 5 只
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07", stage1=_gate_stage(allowed=("Q1",)))
    # 只给 1 只 → 覆盖率 20% < 60%
    _write_snapshot(tmp_path, "2026-09-07", "1430",
                     {focus[0]: {"name": "仅一只", "price": None}})

    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    s1 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage1_1430"]
    assert s1["data_insufficient"] is True
    assert s1["universe_coverage"] == pytest.approx(0.2)


def test_1450_does_not_inherit_insufficient_stage1(monkeypatch, tmp_path):
    """核心回归:首判数据不足时,1450 **不**继承其空集,退回全票池重筛。

    这是 2026-09-16 全天 0 只的根因 —— 原实现会让 1450 候选恒为空集。
    """
    focus = tuple(f"60000{i}" for i in range(1, 6))
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(allowed=("Q1",)), stage2=_gate_stage(allowed=("Q1",)))
    # 首判:只 1 只 → data_insufficient,选出 0 只
    _write_snapshot(tmp_path, "2026-09-07", "1430",
                     {focus[0]: {"name": "仅一只", "price": None}})
    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    # 复核:数据补全到 5 只
    _write_snapshot(tmp_path, "2026-09-07", "1450",
                     {c: {"name": f"票{c}", "price": None} for c in focus})
    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0

    s2 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage2_1450"]
    # 关键:候选范围是全票池(5 只),不是首判的空集
    assert s2["quotes_n"] == 5
    assert s2["data_insufficient"] is False
    # 且必须诚实标注:这批票没经过首判确认
    assert s2["confirm_degraded"] is True
    assert "首判数据不足" in s2["confirm_degraded_reason"]
    assert "单点命中" in s2["note"]


def test_1450_does_inherit_sufficient_empty_stage1(monkeypatch, tmp_path):
    """反向锁:首判数据**充分**但真选出 0 只 → 1450 仍须继承空集(尊重'无信号')。

    否则会把"今天真没票"误判成"数据故障"而放开全池,破坏方案"两次都命中才买"。
    """
    focus = tuple(f"60000{i}" for i in range(1, 6))
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(allowed=("Q1",)), stage2=_gate_stage(allowed=("Q1",)))
    # 首判数据充分(5/5),但这些票不满足任何 Q1 信号 → 空是真实结论
    snap = {c: {"name": f"票{c}", "price": None} for c in focus}
    _write_snapshot(tmp_path, "2026-09-07", "1430", snap)
    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    s1 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage1_1430"]
    assert s1["data_insufficient"] is False
    assert s1["selections"]["Q1"] == []

    _write_snapshot(tmp_path, "2026-09-07", "1450", snap)
    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0
    s2 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage2_1450"]
    # 继承了空集 → 不标降级(这不是故障)
    assert s2.get("confirm_degraded") is not True
    assert s2["final_codes"] == []


def test_degraded_flag_propagates_to_final(monkeypatch, tmp_path):
    """单点命中标记必须透传到 final —— 否则下游分不清是否经过双点确认。"""
    focus = tuple(f"60000{i}" for i in range(1, 6))
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07",
                stage1=_gate_stage(allowed=("Q1",)),
                stage2=_gate_stage(allowed=("Q1",)),
                final=_gate_stage(allowed=("Q1",)))
    _write_snapshot(tmp_path, "2026-09-07", "1430",
                     {focus[0]: {"name": "仅一只", "price": None}})
    assert PS.run("1430", date="2026-09-07", skip_fundflow=True) == 0
    _write_snapshot(tmp_path, "2026-09-07", "1450",
                     {c: {"name": f"票{c}", "price": None} for c in focus})
    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0
    assert PS.run("final", date="2026-09-07", skip_fundflow=True) == 0

    final = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["final"]
    assert final["confirm_degraded"] is True
    assert "单点命中" in final["note"]


def test_1450_degraded_when_stage1_absent(monkeypatch, tmp_path):
    """首判段整体缺失(如 14:30 任务没跑)→ 同样标降级,不静默当双点确认。"""
    focus = tuple(f"60000{i}" for i in range(1, 6))
    _setup_coverage_case(monkeypatch, tmp_path, focus)
    _write_gate(tmp_path, "2026-09-07", stage2=_gate_stage(allowed=("Q1",)))
    _write_snapshot(tmp_path, "2026-09-07", "1450",
                     {c: {"name": f"票{c}", "price": None} for c in focus})

    assert PS.run("1450", date="2026-09-07", skip_fundflow=True) == 0
    s2 = json.loads(PS._screen_path("2026-09-07").read_text(encoding="utf-8"))["stage2_1450"]
    assert s2["confirm_degraded"] is True
    assert "首判段缺失" in s2["confirm_degraded_reason"]


def test_coverage_threshold_constant_sane():
    """门限须 ∈ (0,1];0 等于关闭闸门,>1 永远不达标。"""
    assert 0 < PS.MIN_UNIVERSE_COVERAGE <= 1.0
