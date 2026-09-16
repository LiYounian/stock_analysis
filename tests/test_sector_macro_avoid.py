"""C sector_macro_avoid 单测:锁住宏观/板块级消息规避 forward-shadow 的硬语义（约法6），
防未来 prompt/代码重写删规则。

锁的"为什么改"：
  ① non-gating 纯记录 —— advisory 恒带 non_gating/非validated=True，schema 稳定。
  ② 三主题命中口径 —— 只命中 美联储加息/利率·汇率·关税出口管制 三主题的消息才计入。
  ③ 板块级净方向聚合 —— industries → 申万一级 rollup，利好+/利空−×强度 累加，净<0 才打规避。
  ④ 防未来/防偷看 —— 交易日守卫（非交易日不落盘）+ 幂等（同日重跑不覆盖，除非 --force）。
  ⑤ thermometer 优雅降级 —— 确认层缺数据时不报错，规避标记仍产出。

全程合成 sentiment_policy + monkeypatch thermometer（不触网、不依赖生产数据）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.research import sector_macro_avoid as C

DATE = "2026-09-15"


def _write_policy(root: Path, date: str, msgs: list[dict]) -> None:
    d = root / "analysis" / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "sentiment_policy.json").write_text(
        json.dumps(msgs, ensure_ascii=False), encoding="utf-8")


def _msg(title, direction, strength, industries):
    return {"date": DATE, "title": title, "summary": "", "keyword": "",
            "影响方向": direction, "影响强度": strength, "industries": industries,
            "region": "国外"}


# ── ② 三主题命中口径 ─────────────────────────────────────────────────────────
def test_match_themes():
    assert "美联储加息/利率" in C.match_themes(_msg("美联储加息预期升温", "利空", 4, ["半导体"]))
    assert "关税/出口管制" in C.match_themes(_msg("美方升级半导体出口管制", "利空", 5, ["半导体"]))
    assert "汇率(人民币)" in C.match_themes(_msg("人民币汇率中间价走弱", "利空", 3, ["银行"]))
    # 不命中任一主题 → 空
    assert C.match_themes(_msg("某公司发布新品", "利好", 3, ["消费电子"])) == []


# ── ③ 板块级净方向聚合（利好+ / 利空− ×强度）──────────────────────────────────
def test_aggregate_net_direction():
    msgs = [
        _msg("美联储加息，利率敏感板块承压", "利空", 4, ["半导体"]),   # 半导体→电子 −4
        _msg("加息预期下高估值成长回调", "利空", 2, ["AI算力"]),        # AI算力→计算机 −2
        _msg("算力政策强共振", "利好", 5, ["AI算力"]),                  # 关税/加息不含→不计
    ]
    agg = C.aggregate_macro(msgs)
    # 电子净利空 −4；计算机只累计命中主题的两条（−2）
    assert agg["电子"].净强度 == -4
    assert agg["电子"].净方向 == "利空"
    assert "计算机" in agg
    assert agg["计算机"].净强度 == -2   # 第三条不含三主题词，不计入


def test_net_positive_not_avoided():
    """利好压过利空 → 净正 → 不打规避（faithful to 净方向 触发口径）。"""
    msgs = [
        _msg("关税落地，出口板块短空", "利空", 2, ["半导体"]),
        _msg("出口管制豁免，半导体大利好", "利好", 5, ["半导体"]),
    ]
    agg = C.aggregate_macro(msgs)
    assert agg["电子"].净强度 == 3      # -2 +5
    assert agg["电子"].净方向 == "利好"


# ── ① non-gating schema + ③ 规避触发 ─────────────────────────────────────────
def test_run_daily_shadow_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "thermometer_confirm", lambda *a, **k: {})  # 降级确认
    root = tmp_path / "data"
    _write_policy(root, DATE, [
        _msg("美联储加息，利率敏感承压", "利空", 4, ["半导体"]),
        _msg("人民币贬值预期，进口成本升", "利空", 3, ["公用事业"]),
    ])
    out = tmp_path / "out"
    adv = C.run_daily_shadow(DATE, str(out), data_root=str(root))
    assert adv["non_gating"] is True and adv["非validated"] is True
    assert (out / f"{DATE}.json").exists()
    inds = {b["industry"] for b in adv["板块规避"]}
    assert "电子" in inds                       # 半导体→电子 净利空
    for b in adv["板块规避"]:
        assert set(["industry", "命中主题", "净方向", "强度",
                    "thermometer确认", "规避置信", "置信依据"]).issubset(b)
        assert b["净方向"] == "利空"            # 只有净利空板块进规避列表


# ── ④ 交易日守卫 + 幂等 ──────────────────────────────────────────────────────
def test_trading_day_guard(tmp_path, monkeypatch):
    from tools.collectors import calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    out = tmp_path / "out"
    rc = C.main(["--date", DATE, "--out-dir", str(out)])
    assert rc == 0
    assert not (out / f"{DATE}.json").exists()   # 非交易日不落盘


def test_idempotent(tmp_path, monkeypatch):
    from tools.collectors import calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(C, "thermometer_confirm", lambda *a, **k: {})
    root = tmp_path / "data"
    _write_policy(root, DATE, [_msg("美联储加息承压", "利空", 4, ["半导体"])])
    out = tmp_path / "out"
    rc1 = C.main(["--date", DATE, "--out-dir", str(out), "--data-root", str(root)])
    assert rc1 == 0
    p = out / f"{DATE}.json"
    mtime1 = p.stat().st_mtime_ns
    # 同日重跑：幂等跳过（不重写）
    rc2 = C.main(["--date", DATE, "--out-dir", str(out), "--data-root", str(root)])
    assert rc2 == 0 and p.stat().st_mtime_ns == mtime1
