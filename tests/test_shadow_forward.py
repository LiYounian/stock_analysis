"""shadow_forward 单测:锁住 forward 影子跑的硬语义(约法6),防未来 prompt/代码重写删规则。

锁的"为什么改":
  ① dry-run 非 live —— 只写证据目录,绝不写 live 选股产物 / data_root。
  ② 证据可追加 + 幂等 —— 同日重跑不重复研判、_index.jsonl 一日一行。
  ③ 防未来 —— 未到期的前向标签留 null,决策时点不看未来。
  ④ 前向收益口径 —— r_N == close[idx+N]/close[idx]-1(canonical,同 forward_scorecard)。
  ⑤ 买入侧口径 —— buy_side == stance ∈ {买入,可参与}。
  ⑥ 回填自愈 + α 复用 shadow_score —— 到期后就地回填、样本外 α 可算且诚实标 N。

全程 mock DeepSeek(不真调、不烧 token)+ mock K线(注入可控价),不触网、不依赖生产数据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from tools.analysis import deep_analysis as da
from tools.analysis import shadow_forward as sf

SIG = "2026-03-02"   # 信号日
FUT = ["2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"]  # T+1..T+5


# ── fixture:构造一个 v2r 可建池的当日分析目录(复用 shadow_recall 的合成口径)──────
def _council_row(code, 排序分, 行业="制造业", 方向="看多"):
    return {"code": code, "行业": 行业, "综合方向": 方向, "综合分": 排序分,
            "综合分_收缩": 排序分, "排序分": 排序分, "口径多样性": 3,
            "参与专家数": 3, "覆盖口径": ["技术"], "财报风险": None}


def _record(mktcap=100.0, industry="制造业"):
    return {
        "meta": {"industry": industry, "industry_asof": industry},
        "valuation": {"mktcap_yi": mktcap},
        "lhb_veto": {"triggered": False, "reason": "test"},
        "financing": {"解禁": {"未来90日占流通_pct": None},
                      "可转债": {"潜在摊薄_pct": None}},
        "chip": {"获利比例": None},
        "fundflow": {"近5日主力合计": None},
    }


def _make_day(tmp_path, date, council_rows, records, screens):
    d = tmp_path / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "策略0合议.json").write_text(
        json.dumps({"top": council_rows, "扫描数": len(council_rows),
                    "top_n": len(council_rows)}, ensure_ascii=False), encoding="utf-8")
    for code, rec in records.items():
        (d / f"{code}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    for name, codes in screens.items():
        (d / f"{name}.json").write_text(
            json.dumps({"入选清单": [{"code": c} for c in codes]}, ensure_ascii=False),
            encoding="utf-8")
    return str(tmp_path)


def _scene(tmp_path):
    """强度票 000001(council看多)+ 召回票 000010/000011(命中≥2策略)。"""
    council = [_council_row("000001", 0.9, 行业="强度业")]
    records = {
        "000001": _record(industry="强度业"),
        "000010": _record(industry="召回业A"),
        "000011": _record(industry="召回业B"),
    }
    screens = {
        "动量组合": ["000010", "000011"],
        "量价放量": ["000010", "000011"],
    }
    return _make_day(tmp_path, SIG, council, records, screens)


# ── mock DeepSeek:按 stance 表返回真 JudgeResult(units_of 正常过滤)────────────
def _fake_generate(stance_map, error_codes=()):
    def gen(date, codes, **kw):
        out = []
        for c in codes:
            if c in error_codes:
                out.append(da.JudgeResult(code=c, unit={}, error="LLM 研判失败:mock"))
                continue
            unit = {"code": c, "stance": stance_map.get(c, "观望"),
                    "dir_1d": "偏多" if stance_map.get(c) in ("买入", "可参与") else "中性",
                    "dir_5d": "中性", "key_reason": "mock", "key_risk": "mock"}
            out.append(da.JudgeResult(code=c, unit=unit))
        return out
    return gen


# ── mock K线:{code: [(date, close), ...]} → market.load_kline 返回 DataFrame ──────
def _fake_kline(klines):
    def load(code):
        rows = klines.get(code)
        if rows is None:
            raise FileNotFoundError(code)
        return pd.DataFrame(rows, columns=["date", "close"])
    return load


def _patch(monkeypatch, stance_map, klines, error_codes=()):
    monkeypatch.setattr(sf.da, "generate", _fake_generate(stance_map, error_codes))
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline", _fake_kline(klines))


# ── ① dry-run 非 live:只写证据目录,绝不写 live 产物 / data_root ─────────────────
def test_dry_run_not_live(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与", "000011": "观望"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")})
    sf.run_forward_day(SIG, dr, str(ev))
    # live 铁律:data_root 下不得新增任何 live 选股产物
    assert not list(Path(dr).rglob("每日选股.json")), "绝不得写 live 选股产物"
    assert not list(Path(dr).rglob("picks*.json")), "绝不得写 live picks 产物"
    # 只写证据目录
    assert sf.evidence_path(str(ev), SIG).exists()
    assert sf.index_path(str(ev)).exists()


# ── ② 证据可追加 + 幂等:同日重跑跳过研判、索引一日一行 ──────────────────────────
def test_append_and_idempotent(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    calls = {"n": 0}

    def counting_gen(date, codes, **kw):
        calls["n"] += 1
        return _fake_generate({"000001": "买入"})(date, codes, **kw)

    monkeypatch.setattr(sf.da, "generate", counting_gen)
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline",
                        _fake_kline({c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")}))

    s1 = sf.run_forward_day(SIG, dr, str(ev))
    s2 = sf.run_forward_day(SIG, dr, str(ev))   # 重跑
    assert calls["n"] == 1, "同日重跑不得再调 DeepSeek(幂等,不烧 token)"
    assert s2.get("skipped") is True
    lines = [ln for ln in sf.index_path(str(ev)).read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, "_index.jsonl 同一日只能一行(upsert 幂等)"
    # force 重跑会再调
    sf.run_forward_day(SIG, dr, str(ev), force=True)
    assert calls["n"] == 2


# ── ③ 买入侧口径:buy_side == stance ∈ {买入,可参与} ─────────────────────────────
def test_buy_side_semantics(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与", "000011": "观望"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")})
    sf.run_forward_day(SIG, dr, str(ev))
    rec = json.loads(sf.evidence_path(str(ev), SIG).read_text())
    assert set(rec["buy_side"]) == {"000001", "000010"}, "买入侧=买入∪可参与,观望不计"
    assert "000011" not in rec["buy_side"]


# ── ③b 研判出错的票不进 units,但仍进池/仍建标签 ────────────────────────────────
def test_error_units_dropped_but_labeled(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")},
           error_codes=("000011",))
    sf.run_forward_day(SIG, dr, str(ev))
    rec = json.loads(sf.evidence_path(str(ev), SIG).read_text())
    unit_codes = {u["code"] for u in rec["units"]}
    assert "000011" not in unit_codes, "研判出错的票不得进 units"
    assert "000011" in rec["labels"], "池内票仍建前向标签(供收益追踪)"
    assert any(e["code"] == "000011" for e in rec["errors"])


# ── ④ 防未来:决策日无 T+N 数据 → 标签留 null(不看未来)────────────────────────
def test_no_future_labels_at_decision(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    # K线只到信号日(无任何未来行)
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")})
    sf.run_forward_day(SIG, dr, str(ev))
    rec = json.loads(sf.evidence_path(str(ev), SIG).read_text())
    for c, lab in rec["labels"].items():
        assert lab["close_0"] == 10.0, "close_0=信号日收盘(当日已披露,可写)"
        assert lab["r_1"] is None and lab["r_5"] is None, "未到期前向标签必须留 null"
    assert rec["label_status"] == "pending"


# ── ④b 前向收益口径:r_N == close[idx+N]/close[idx]-1(canonical)────────────────
def test_forward_return_formula(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    # 000001 有到 T+5 的完整未来:close 10→11(T+1)... →13(T+5)
    closes = [10.0, 11.0, 10.5, 12.0, 9.0, 13.0]      # SIG + FUT(5 根)
    rows_full = list(zip([SIG] + FUT, closes))
    _patch(monkeypatch, {"000001": "买入"},
           {"000001": rows_full,
            "000010": [(SIG, 20.0)], "000011": [(SIG, 30.0)]})
    sf.run_forward_day(SIG, dr, str(ev))
    lab = json.loads(sf.evidence_path(str(ev), SIG).read_text())["labels"]["000001"]
    assert lab["r_1"] == pytest.approx((11.0 / 10.0 - 1) * 100)
    assert lab["r_5"] == pytest.approx((13.0 / 10.0 - 1) * 100)
    # 只到信号日的票:仍留 null
    assert json.loads(sf.evidence_path(str(ev), SIG).read_text())["labels"]["000010"]["r_1"] is None


# ── ⑤ 回填自愈:到期后 backfill 就地补 null,幂等,防未来仍守 ─────────────────────
def test_backfill_fills_matured_idempotent(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    # 决策时:无未来 → 全 null
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")})
    sf.run_forward_day(SIG, dr, str(ev))
    assert json.loads(sf.evidence_path(str(ev), SIG).read_text())["label_status"] == "pending"

    # 未来数据到位(只到 T+1,T+5 仍缺)→ 回填应补 r_1、留 r_5=null(partial)
    from tools.collectors import market
    monkeypatch.setattr(market, "load_kline", _fake_kline({
        "000001": [(SIG, 10.0), (FUT[0], 12.0)],
        "000010": [(SIG, 10.0), (FUT[0], 9.0)],
        "000011": [(SIG, 10.0), (FUT[0], 10.0)],
    }))
    bf1 = sf.backfill_labels(str(ev))
    assert bf1["n_filled"] == 3, "3 票各补上 r_1"
    rec = json.loads(sf.evidence_path(str(ev), SIG).read_text())
    assert rec["labels"]["000001"]["r_1"] == pytest.approx(20.0)
    assert rec["labels"]["000001"]["r_5"] is None, "T+5 仍缺 → 留 null(防未来)"
    assert rec["label_status"] == "partial"
    # 幂等:再回填(同数据)不再改动
    bf2 = sf.backfill_labels(str(ev))
    assert bf2["n_filled"] == 0 and bf2["days_touched"] == 0


# ── ⑥ α 基准=全A等权(唯一真源):样本外 α 可算、诚实标基准来源 ─────────────────────
def _make_breadth(tmp_path, mp_by_date):
    """构造 data/breadth/<date>.json({date, mean_pct})→ equal_weight_index 读它当全A等权基准。"""
    bd = tmp_path / "breadth"
    bd.mkdir(parents=True, exist_ok=True)
    for date, mp in mp_by_date.items():
        (bd / f"{date}.json").write_text(
            json.dumps({"date": date, "mean_pct": mp}, ensure_ascii=False), encoding="utf-8")
    return str(bd)


def test_forward_alpha_equal_weight_benchmark(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    # 买入 000001(r_1=+10)、可参与 000010(r_1=-5);000011 观望不计
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与", "000011": "观望"},
           {"000001": [(SIG, 10.0), (FUT[0], 11.0)],
            "000010": [(SIG, 10.0), (FUT[0], 9.5)],
            "000011": [(SIG, 10.0), (FUT[0], 10.0)]})
    sf.run_forward_day(SIG, dr, str(ev))
    sf.backfill_labels(str(ev))
    # 全A等权 r_1 = mean_pct[FUT0] = +1.0%(锚 SIG=0)
    bdir = _make_breadth(tmp_path, {SIG: 0.0, FUT[0]: 1.0})
    a = sf.forward_alpha(str(ev), breadth_dir=bdir, scorecard_path=None)
    assert a["n_days"] == 1 and a["total_buys"] == 2
    b1 = a["buy_agg"]["r_1"]
    assert b1["pooled_n_buys"] == 2
    assert b1["pooled_buy_mean_r"] == pytest.approx(2.5), "买入侧均值=(10-5)/2"
    # α = 买入侧均值 − 全A等权基准 = 2.5 − 1.0 = +1.5
    assert a["per_day"][0]["alpha_r1"] == pytest.approx(2.5 - 1.0)
    assert a["per_day"][0]["bench_src_r1"] == "全A等权", "基准应取全A等权唯一真源"
    assert "全A等权" in a["benchmark_note"] and "非投资建议" in a["benchmark_note"]


# ── ⑥b 基准回退:全A等权窗口不全 → 回退 forward_scorecard 全样本代理,诚实标来源 ──────
def test_forward_alpha_fallback_scorecard(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    _patch(monkeypatch, {"000001": "买入", "000010": "可参与", "000011": "观望"},
           {"000001": [(SIG, 10.0), (FUT[0], 11.0)],
            "000010": [(SIG, 10.0), (FUT[0], 9.5)],
            "000011": [(SIG, 10.0), (FUT[0], 10.0)]})
    sf.run_forward_day(SIG, dr, str(ev))
    sf.backfill_labels(str(ev))
    # forward_scorecard 全样本(含 SIG 日 3 票 r_1=10/-5/0,均值 5/3)当回退基准
    card = tmp_path / "sc.csv"
    rows = "date,code,r_1,r_5\n" + \
        f"{SIG},000001,10.0,\n{SIG},000010,-5.0,\n{SIG},000011,0.0,\n"
    card.write_text(rows, encoding="utf-8")
    empty_breadth = str(tmp_path / "no_breadth")   # 不存在 → 全A等权空 → 回退
    a = sf.forward_alpha(str(ev), breadth_dir=empty_breadth, scorecard_path=str(card))
    assert a["per_day"][0]["bench_src_r1"] == "scorecard代理", "全A等权缺 → 回退 scorecard 代理"
    assert a["per_day"][0]["alpha_r1"] == pytest.approx(2.5 - (10.0 - 5.0 + 0.0) / 3)


# ── ⑥c benchmark 口径写进每条 evidence(诚实标基准边界)──────────────────────────
def test_benchmark_note_in_evidence(tmp_path, monkeypatch):
    dr = _scene(tmp_path)
    ev = tmp_path / "evidence"
    _patch(monkeypatch, {"000001": "买入"},
           {c: [(SIG, 10.0)] for c in ("000001", "000010", "000011")})
    sf.run_forward_day(SIG, dr, str(ev))
    rec = json.loads(sf.evidence_path(str(ev), SIG).read_text())
    assert "benchmark_note" in rec
    assert "全A等权" in rec["benchmark_note"] and "非投资建议" in rec["benchmark_note"]
