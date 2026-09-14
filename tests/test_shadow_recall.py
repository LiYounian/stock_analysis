"""shadow_recall 单测:锁住召回语义/闸门/排序契约/cap契约/防未来/确定性/看空守卫(设计 §7.2)。

合成 fixture 精确锁语义(不依赖生产数据),另加真实数据烟测锁接口/确定性。
断言直接锁"为什么这么改"——防未来 prompt/代码重写时无意删规则。
"""
from __future__ import annotations
import json
from pathlib import Path

import pytest

from tools.analysis import shadow_pool_v2 as v2
from tools.analysis import shadow_recall as sr

PROD = Path("/Users/yqg/Documents/projects/stock_analysis/data/analysis")


# ── 合成 fixture 构造器 ──────────────────────────────────────────────────────
def _council_row(code, 排序分, 行业="制造业", 方向="看多"):
    return {"code": code, "行业": 行业, "综合方向": 方向, "综合分": 排序分,
            "综合分_收缩": 排序分, "排序分": 排序分, "口径多样性": 3,
            "参与专家数": 3, "覆盖口径": ["技术"], "财报风险": None}


def _record(mktcap=100.0, veto=False, unlock=None, profit=None, cb=None, ff5=None,
            industry="制造业"):
    return {
        "meta": {"industry": industry, "industry_asof": industry},
        "valuation": {"mktcap_yi": mktcap},
        "lhb_veto": {"triggered": veto, "reason": "test"},
        "financing": {"解禁": {"未来90日占流通_pct": unlock},
                      "可转债": {"潜在摊薄_pct": cb}},
        "chip": {"获利比例": profit},
        "fundflow": {"近5日主力合计": ff5},
    }


def _make_day(tmp_path, date, council_rows, records, screens):
    """screens: {策略名: [code,...]}(会写成 {入选清单:[{code:..},..]} 策略 view)。"""
    d = tmp_path / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "策略0合议.json").write_text(
        json.dumps({"top": council_rows, "命中高危红旗": 0, "命中龙虎榜否决": 0,
                    "扫描数": len(council_rows), "top_n": len(council_rows)},
                   ensure_ascii=False), encoding="utf-8")
    for code, rec in records.items():
        (d / f"{code}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    for name, codes in screens.items():
        (d / f"{name}.json").write_text(
            json.dumps({"入选清单": [{"code": c} for c in codes]}, ensure_ascii=False),
            encoding="utf-8")
    return str(tmp_path)


def _base_scene(profit_recall_extra=None):
    """1 只 council 看多强度票(000001)+ 若干池外候选,可控命中策略数。"""
    council = [_council_row("000001", 0.9, 行业="强度业")]
    records = {
        "000001": _record(industry="强度业"),   # 强度主池
        "000010": _record(industry="召回业A"),   # 将命中 2 策略 → 召回
        "000011": _record(industry="召回业B"),   # 将命中 3 策略 → 召回(更前)
        "000012": _record(industry="召回业C"),   # 仅命中 1 策略 → 不召回
    }
    if profit_recall_extra:
        records.update(profit_recall_extra)
    screens = {
        "动量组合": ["000010", "000011", "000012"],
        "量价放量": ["000010", "000011"],
        "最强选股": ["000011"],
    }
    return council, records, screens


# ── ① 召回语义:screen≥2 ∧ 池外 → 召回;仅 1 策略 → 不召回 ─────────────────────
def test_recall_requires_two_screens(tmp_path):
    date = "2026-02-02"
    council, records, screens = _base_scene()
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, meta = sr.build_pool_v2r(dr, date)
    assert "000001" in pool, "council 看多强度票应在强度主池"
    assert "000011" in pool and "000010" in pool, "命中≥2策略的池外票必须被召回"
    assert "000012" not in pool, "仅命中 1 策略的票不得召回(screen<MIN_HITS)"
    codes = [x["code"] for x in meta["recalled"]]
    # hit_count 降序:000011(3命中) 排在 000010(2命中) 前
    assert codes.index("000011") < codes.index("000010")


# ── ② 闸门:召回票过同一套硬闸门(龙虎否决 / 流动性)────────────────────────────
def test_recall_hard_gates(tmp_path):
    date = "2026-02-03"
    council, records, screens = _base_scene()
    records["000010"] = _record(veto=True, industry="召回业A")     # 龙虎否决 → 剔
    records["000011"] = _record(mktcap=5.0, industry="召回业B")     # 低流动性 → 剔
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, meta = sr.build_pool_v2r(dr, date)
    assert "000010" not in pool, "龙虎否决的召回候选必须被硬闸门剔除"
    assert "000011" not in pool, "低流动性的召回候选必须被硬闸门剔除"


# ── ③ council 看空守卫:screen≥2 但 council 看空 → 不召回 ─────────────────────
def test_recall_bearish_guard(tmp_path):
    date = "2026-02-04"
    council, records, screens = _base_scene()
    # 让 000011 在 council 里被标看空(即便多策略命中也不召回)
    council.append(_council_row("000011", 0.95, 行业="召回业B", 方向="看空"))
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, _ = sr.build_pool_v2r(dr, date)
    assert "000011" not in pool, "council 明确看空的票不得召回(第一性原理守卫)"
    assert "000010" in pool, "非看空的多策略命中票仍应召回"


# ── ④ 排序契约:召回恒排在强度主池之后 ───────────────────────────────────────
def test_recall_ranked_after_strength(tmp_path):
    date = "2026-02-05"
    council, records, screens = _base_scene()
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, meta = sr.build_pool_v2r(dr, date)
    base_n = meta["base_pool_size"]
    recalled = set(x["code"] for x in meta["recalled"])
    # 强度主池在前 base_n 位,召回票只出现在其后
    for i, c in enumerate(pool):
        if c in recalled:
            assert i >= base_n, "召回票必须排在强度主池之后"


# ── ⑤ cap 契约:强度主池不被挤出;召回≤RECALL_CAP;总池≤CAP_R ──────────────────
def test_cap_contracts(tmp_path):
    date = "2026-02-06"
    # 强度主池填到 v2.CAP;再放大量池外多策略命中票
    council = [_council_row(f"0001{i:02d}", 0.99 - i * 0.001, 行业=f"业{i}")
               for i in range(0, v2.CAP)]
    records = {f"0001{i:02d}": _record(industry=f"业{i}") for i in range(0, v2.CAP)}
    recall_codes = [f"0002{i:02d}" for i in range(0, 20)]
    for i, c in enumerate(recall_codes):
        records[c] = _record(industry=f"召回业{i}")
    screens = {"动量组合": recall_codes, "量价放量": recall_codes, "最强选股": recall_codes}
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, meta = sr.build_pool_v2r(dr, date)
    base_codes = [f"0001{i:02d}" for i in range(0, v2.CAP)]
    for c in base_codes:
        assert c in pool, "强度主池任何一只都不得被召回挤出"
    assert meta["recall_added"] <= sr.RECALL_CAP, "召回数不得超过 RECALL_CAP"
    assert len(pool) <= sr.CAP_R, "总池不得超过 CAP_R"


# ── ⑤ 行业限流对合并后总池统一施加 ───────────────────────────────────────────
def test_industry_cap_on_recall(tmp_path):
    date = "2026-02-07"
    council = [_council_row("000001", 0.9, 行业="热门业")]
    records = {"000001": _record(industry="热门业")}
    # 4 只召回票全同一行业,IND_CAP 限制后不能全进
    recall_codes = ["000020", "000021", "000022", "000023"]
    for c in recall_codes:
        records[c] = _record(industry="热门业")
    screens = {"动量组合": recall_codes, "量价放量": recall_codes}
    dr = _make_day(tmp_path, date, council, records, screens)
    pool, meta = sr.build_pool_v2r(dr, date)
    same_ind = [c for c in pool if c == "000001" or c in recall_codes]
    assert len(same_ind) <= sr.IND_CAP, f"同行业入池数不得超过 IND_CAP={sr.IND_CAP}"


# ── ⑥ 防未来:构池只读 <=date 目录 ───────────────────────────────────────────
def test_no_future_leak(tmp_path, monkeypatch):
    date = "2026-02-10"
    council, records, screens = _base_scene()
    dr = _make_day(tmp_path, date, council, records, screens)
    future = "2026-02-11"
    _make_day(tmp_path, future, [_council_row("000099", 0.99)],
              {"000099": _record()}, {"动量组合": ["000099"]})
    read_paths = []
    orig = Path.read_text

    def spy(self, *a, **k):
        read_paths.append(str(self))
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy)
    sr.build_pool_v2r(dr, date)
    assert all(future not in p for p in read_paths), f"不得读取未来日 {future}"
    assert any(date in p for p in read_paths)


# ── ⑦ 确定性 + 接口同形 ──────────────────────────────────────────────────────
def test_deterministic_and_shape(tmp_path):
    date = "2026-02-12"
    council, records, screens = _base_scene()
    dr = _make_day(tmp_path, date, council, records, screens)
    p1, meta = sr.build_pool_v2r(dr, date)
    p2, _ = sr.build_pool_v2r(dr, date)
    assert p1 == p2, "同输入必须同输出(确定性)"
    assert isinstance(p1, list) and all(isinstance(c, str) for c in p1)
    assert isinstance(meta, dict) and meta["version"] == "v2r"
    assert meta["pool_size"] == len(p1)


# ── ⑧ 真实数据烟测:接口跑通 + 召回确实扩池 + 确定性 ──────────────────────────
@pytest.mark.skipif(not PROD.exists(), reason="生产数据不可用")
def test_real_data_smoke():
    date = "2026-09-10"
    if not (PROD / date / "策略0合议.json").exists():
        pytest.skip("该日无 council")
    p1, meta = sr.build_pool_v2r(str(PROD), date)
    p2, _ = sr.build_pool_v2r(str(PROD), date)
    assert p1 == p2
    assert all(isinstance(c, str) and len(c) == 6 for c in p1)
    assert meta["pool_size"] == len(p1)
    assert len(p1) <= sr.CAP_R
    # 召回确实在强度主池基础上扩了池(v2r >= v2)
    base_pool, _ = v2.build_pool_v2(str(PROD), date)
    assert set(base_pool).issubset(set(p1)), "v2r 必须包含全部 v2 强度主池"
    assert meta["recall_added"] == len(p1) - meta["base_pool_size"]
