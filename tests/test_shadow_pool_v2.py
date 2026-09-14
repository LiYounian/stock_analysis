"""shadow_pool_v2 单测:锁住闸门语义/强度单调/防未来/接口同形/逐日确定性(计划 §6.3)。

用合成 fixture 精确锁语义(不依赖生产数据),另加真实数据烟测锁接口/确定性。
断言直接锁"为什么这么改"的语义,防未来 prompt/代码重写时无意删规则。
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from tools.analysis import shadow_pool, shadow_pool_v2 as v2

PROD = Path("/Users/yqg/Documents/projects/stock_analysis/data/analysis")


# ── 合成 fixture 构造器 ──────────────────────────────────────────────────────
def _council_row(code, 排序分, 行业="制造业", 方向="看多", 多样性=3, 专家=3):
    return {"code": code, "行业": 行业, "综合方向": 方向, "综合分": 排序分,
            "综合分_收缩": 排序分, "排序分": 排序分, "口径多样性": 多样性,
            "参与专家数": 专家, "覆盖口径": ["技术"], "财报风险": None}


def _record(mktcap=100.0, veto=False, unlock=None, profit=None, cb=None, ff5=None):
    return {
        "valuation": {"mktcap_yi": mktcap},
        "lhb_veto": {"triggered": veto, "reason": "test"},
        "financing": {"解禁": {"未来90日占流通_pct": unlock},
                      "可转债": {"潜在摊薄_pct": cb}},
        "chip": {"获利比例": profit},
        "fundflow": {"近5日主力合计": ff5},
    }


def _make_day(tmp_path, date, council_rows, records):
    d = tmp_path / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "策略0合议.json").write_text(
        json.dumps({"top": council_rows, "命中高危红旗": 0, "命中龙虎榜否决": 0,
                    "扫描数": len(council_rows), "top_n": len(council_rows)},
                   ensure_ascii=False), encoding="utf-8")
    for code, rec in records.items():
        (d / f"{code}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


# ── ① 闸门语义:龙虎否决 triggered → 硬剔 ────────────────────────────────────
def test_gate_lhb_veto_drops(tmp_path):
    date = "2026-01-05"
    rows = [_council_row("000001", 0.8), _council_row("000002", 0.7)]
    recs = {"000001": _record(veto=True), "000002": _record(veto=False)}
    dr = _make_day(tmp_path, date, rows, recs)
    pool, meta = v2.build_pool_v2(dr, date, cap=10, min_n=1)
    assert "000001" not in pool, "龙虎否决 triggered 的票必须被硬剔"
    assert "000002" in pool
    assert any("龙虎否决" in x["reason"] and x["code"] == "000001" for x in meta["dropped"])


# ── ① 闸门语义:低流动性(mktcap 低于当日分位/绝对地板)→ 硬剔 ─────────────────
def test_gate_liquidity_drops(tmp_path):
    date = "2026-01-06"
    # 一只远低于绝对地板(20亿)的小票,其余充分流动
    rows = [_council_row(f"0000{i:02d}", 0.9 - i * 0.01) for i in range(1, 11)]
    recs = {f"0000{i:02d}": _record(mktcap=(5.0 if i == 1 else 200.0)) for i in range(1, 11)}
    dr = _make_day(tmp_path, date, rows, recs)
    pool, meta = v2.build_pool_v2(dr, date, cap=20, min_n=1)
    assert "000001" not in pool, "市值低于绝对地板的票必须被流动性闸门剔除"
    assert any("流动性" in x["reason"] and x["code"] == "000001" for x in meta["dropped"])


# ── ① 软标签:融资摊薄/筹码高位/资金流转弱 打标但不剔 ────────────────────────
def test_soft_tags_not_dropped(tmp_path):
    date = "2026-01-07"
    rows = [_council_row("000001", 0.8)]
    recs = {"000001": _record(mktcap=100.0, unlock=5.0, profit=0.95, cb=8.0, ff5=-1e6)}
    dr = _make_day(tmp_path, date, rows, recs)
    pool, meta = v2.build_pool_v2(dr, date, cap=10, min_n=1)
    assert "000001" in pool, "软标签票不应被硬剔,只打标"
    tags = meta["soft_tags"].get("000001", [])
    assert any("解禁" in t for t in tags)
    assert any("获利盘" in t for t in tags)
    assert any("可转债" in t for t in tags)
    assert any("净流出" in t for t in tags)


# ── ① 看空/中性 不进强度骨架 ────────────────────────────────────────────────
def test_only_bullish_enters_base(tmp_path):
    date = "2026-01-08"
    rows = [_council_row("000001", 0.9, 方向="看多"),
            _council_row("000002", 0.95, 方向="看空"),
            _council_row("000003", 0.92, 方向="中性")]
    recs = {c: _record() for c in ("000001", "000002", "000003")}
    dr = _make_day(tmp_path, date, rows, recs)
    pool, _ = v2.build_pool_v2(dr, date, cap=10, min_n=1)
    assert pool == ["000001"], "只有看多进池(看空/中性即使分更高也不进)"


# ── ② 强度排序单调:final_score 随 code 严格降序,且强度主导 ──────────────────
def test_strength_ranking_monotonic(tmp_path):
    date = "2026-01-09"
    # 同行业不同强度(避开行业限流,给独立行业)
    rows = [_council_row(f"0000{i:02d}", 0.9 - i * 0.05, 行业=f"业{i}")
            for i in range(1, 6)]
    recs = {f"0000{i:02d}": _record() for i in range(1, 6)}
    dr = _make_day(tmp_path, date, rows, recs)
    _, meta = v2.build_pool_v2(dr, date, cap=10, min_n=1)
    scores = [x["final_score"] for x in meta["detail"]]
    assert scores == sorted(scores, reverse=True), "final_score 必须降序单调"
    # 强度更高者排更前(小权重交叉确认不该翻转显著强度差)
    codes = [x["code"] for x in meta["detail"]]
    assert codes == ["000001", "000002", "000003", "000004", "000005"]


# ── ③ 防未来:构池只读 <=date 目录,不触碰任何 >date 数据 ──────────────────────
def test_no_future_leak(tmp_path, monkeypatch):
    date = "2026-01-12"
    rows = [_council_row("000001", 0.8)]
    recs = {"000001": _record()}
    dr = _make_day(tmp_path, date, rows, recs)
    # 造一个未来日目录,监控是否被读取
    future = "2026-01-13"
    _make_day(tmp_path, future, [_council_row("000009", 0.99)], {"000009": _record()})
    read_paths = []
    orig = Path.read_text

    def spy(self, *a, **k):
        read_paths.append(str(self))
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    v2.build_pool_v2(dr, date)
    assert all(future not in p for p in read_paths), f"不得读取未来日 {future}:{read_paths}"
    assert any(date in p for p in read_paths)


# ── ④ 接口同形:与 v1 build_pool 返回形状一致((list[str], dict))──────────────
def test_interface_shape_matches_v1(tmp_path):
    date = "2026-01-15"
    rows = [_council_row(f"0003{i:02d}", 0.9 - i * 0.02, 行业=f"业{i}") for i in range(1, 6)]
    recs = {f"0003{i:02d}": _record() for i in range(1, 6)}
    dr = _make_day(tmp_path, date, rows, recs)
    pool, meta = v2.build_pool_v2(dr, date)
    assert isinstance(pool, list) and all(isinstance(c, str) for c in pool)
    assert isinstance(meta, dict) and "date" in meta and "pool_size" in meta
    # 与 v1 函数签名同形:(data_root, date, cap, min_n) -> (list, dict)
    import inspect
    sig_v1 = list(inspect.signature(shadow_pool.build_pool).parameters)
    sig_v2 = list(inspect.signature(v2.build_pool_v2).parameters)
    assert sig_v1 == sig_v2, f"签名须同形 v1={sig_v1} v2={sig_v2}"


# ── ⑤ 逐日确定性:同输入多次调用产出完全一致 ────────────────────────────────
def test_deterministic(tmp_path):
    date = "2026-01-16"
    rows = [_council_row(f"0004{i:02d}", 0.9 - i * 0.01, 行业=f"业{i % 3}") for i in range(1, 12)]
    recs = {f"0004{i:02d}": _record(mktcap=50.0 + i) for i in range(1, 12)}
    dr = _make_day(tmp_path, date, rows, recs)
    p1, _ = v2.build_pool_v2(dr, date)
    p2, _ = v2.build_pool_v2(dr, date)
    assert p1 == p2, "同输入必须同输出(逐日确定性)"


# ── ⑤ 真实数据烟测:接口跑通 + 池大小合理 + 确定性 ────────────────────────────
@pytest.mark.skipif(not PROD.exists(), reason="生产数据不可用")
def test_real_data_smoke():
    date = "2026-09-10"
    if not (PROD / date / "策略0合议.json").exists():
        pytest.skip("该日无 council")
    p1, meta = v2.build_pool_v2(str(PROD), date)
    p2, _ = v2.build_pool_v2(str(PROD), date)
    assert p1 == p2
    assert isinstance(p1, list) and all(isinstance(c, str) and len(c) == 6 for c in p1)
    assert meta["pool_size"] == len(p1)
    assert len(p1) <= v2.CAP
