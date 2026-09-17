"""消息驱动板块选股 M3 forward 记分器单测:锁死 non-gating / 防未来 / 龙头跟涨分开 /
分层 / append-only / 样本门槛(约法6:锁"为什么改",防未来 prompt/代码重写删规则)。

锁的硬语义(对齐任务书 §4 与 接口设计 §4):
  ① non-gating —— 只写自己的 out_dir/<date>.json,**绝不改 sector_focus.json / live 选股产物**;
     advisory 恒带 non_gating/forward_only/非validated=True。
  ② as-of 守卫 —— 只消费 as_of ≤ 信号日 D 的块;as_of 晚于 D(未来信号)→ 整段拒(返回 None)。
  ③ 龙头 / 跟涨严格分开 —— 龙头票 只来自 龙头候选、跟涨票 只来自 跟涨候选,不混;跟涨挂 board_已动。
  ④ 回踩限价入场 + 绝对收益口径 —— 限价=D 收盘,D+1 low≤限价才成交(price=min(限价,open),不追高开);
     r_dN = 成交价→D+N 收盘 %;高开未回踩 → not_entered、无收益。
  ⑤ 分层 —— summarize 按 强弱(强/中/弱) + 已动(龙头)/board_已动(跟涨) 拆,龙头跟涨分列。
  ⑥ append-only 回填 —— 到期后 backfill 只填 None cell、绝不改写已成交/已结算;幂等;样本 <120 只报 N。

全程合成块 + monkeypatch K 线/基准(不触网、不依赖生产数据)。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.research import sector_news_forward as M

DATE = "2026-09-11"
CAL = [DATE, "2026-09-12", "2026-09-15"]      # D, D+1, D+2(合成交易日)


# ── fixture:写 sector_focus.json 的「消息驱动」块 + monkeypatch K线/基准 ──────
def _write_block(root: Path, date: str, block: dict, *, extra_focus: dict | None = None):
    d = root / "analysis" / date
    d.mkdir(parents=True, exist_ok=True)
    focus = {"版本": "test", "消息驱动": block}
    if extra_focus:
        focus.update(extra_focus)
    p = d / "sector_focus.json"
    p.write_text(json.dumps(focus, ensure_ascii=False), encoding="utf-8")
    return p


def _block(利好板块: list[dict], as_of: str = DATE) -> dict:
    return {"as_of": as_of, "version": "test", "利好板块": 利好板块}


def _mock_kline(monkeypatch, rows_by_code: dict):
    """monkeypatch _kline:注入 {code: [(date,open,low,close)...]},d2i 用各票自身日期。"""
    def fake(code, cache):
        rows = rows_by_code.get(code)
        if rows is None:
            return (None, {})
        return (rows, {r[0]: i for i, r in enumerate(rows)})
    monkeypatch.setattr(M, "_kline", fake)


def _mock_ew(monkeypatch, ew: dict):
    monkeypatch.setattr(M, "_load_ew", lambda data_root: dict(ew))


def _bootstrap_dataroot(monkeypatch):
    """ensure_data_root 在测试里不需要(直传 data_root),打成 no-op 防触真主档。"""
    import tools.analysis.market_forecast.dataroot as DR
    monkeypatch.setattr(DR, "ensure_data_root", lambda *a, **k: None)


# ── ① non-gating:只写自己的盘,不碰 sector_focus ─────────────────────────────
def test_non_gating_writes_only_own_dir(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "强",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": True}],
                   "跟涨候选": [{"code": "600002", "name": "乙", "联动依据": "补涨"}]}])
    focus_p = _write_block(root, DATE, blk)
    before = focus_p.read_text(encoding="utf-8")
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10)], "600002": [(DATE, 5, 5, 5)]})
    _mock_ew(monkeypatch, {})
    out = tmp_path / "out"
    adv = M.run_daily(DATE, str(out), data_root=str(root))
    assert adv["non_gating"] is True and adv["forward_only"] is True and adv["非validated"] is True
    # sector_focus 原样未被改(non-gating 铁律)
    assert focus_p.read_text(encoding="utf-8") == before
    # 只落自己的 advisory,别的都没动
    assert (out / f"{DATE}.json").exists()
    assert list(p.name for p in out.iterdir()) == [f"{DATE}.json"]


# ── ② as-of 守卫:未来信号整段拒 ─────────────────────────────────────────────
def test_asof_guard_rejects_future(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "中",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": False}],
                   "跟涨候选": []}], as_of="2026-09-12")   # as_of 晚于信号日 D
    _write_block(root, DATE, blk)
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10)]})
    _mock_ew(monkeypatch, {})
    assert M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root)) is None
    assert not (tmp_path / "out" / f"{DATE}.json").exists()   # 不落坏盘
    # as_of == D → 放行
    assert M.asof_ok({"as_of": DATE}, DATE) is True
    assert M.asof_ok({"as_of": "2026-09-10"}, DATE) is True
    assert M.asof_ok({"as_of": None}, DATE) is False          # 缺 as_of 保守拒


# ── ③ 龙头 / 跟涨严格分开 ────────────────────────────────────────────────────
def test_leader_follower_separated(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "强",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": True}],
                   "跟涨候选": [{"code": "600002", "name": "乙", "联动依据": "龙头已动·接力"}]}])
    _write_block(root, DATE, blk)
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10)], "600002": [(DATE, 5, 5, 5)]})
    _mock_ew(monkeypatch, {})
    adv = M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    assert [r["code"] for r in adv["龙头票"]] == ["600001"]
    assert [r["code"] for r in adv["跟涨票"]] == ["600002"]
    assert all(r["stream"] == M.MAIN_STREAM for r in adv["龙头票"])
    assert all(r["stream"] == M.FOLLOW_STREAM for r in adv["跟涨票"])
    # 龙头带 已动;跟涨带 board_已动(该板块龙头已动=True)+联动依据、且**不带主档"已动"**
    assert adv["龙头票"][0]["已动"] is True
    assert adv["跟涨票"][0]["board_已动"] is True and "已动" not in adv["跟涨票"][0]


# ── ④ 回踩限价成交 + 绝对收益口径;高开未回踩 → not_entered ────────────────────
def test_entry_limit_fill_and_returns(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "强",
                   "龙头候选": [{"code": "FILL", "name": "回踩成交", "已动": False},
                              {"code": "GAP", "name": "高开未回踩", "已动": False}],
                   "跟涨候选": []}])
    _write_block(root, DATE, blk)
    # FILL:D 收=10→限价10;D+1 low=9.5≤10 成交,price=min(10,9.8)=9.8;close1=11,close2=12
    # GAP :D 收=10→限价10;D+1 low=10.2>10 高开未回踩 → 未成交
    _mock_kline(monkeypatch, {
        "FILL": [(DATE, 10, 10, 10.0), (CAL[1], 9.8, 9.5, 11.0), (CAL[2], 11.5, 11.0, 12.0)],
        "GAP":  [(DATE, 10, 10, 10.0), (CAL[1], 10.5, 10.2, 11.0), (CAL[2], 11.5, 11.0, 12.0)],
    })
    _mock_ew(monkeypatch, {DATE: 1.0, CAL[1]: 1.02, CAL[2]: 1.05})
    adv = M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    by = {r["code"]: r for r in adv["龙头票"]}
    fill = by["FILL"]
    assert fill["entry"]["filled"] is True and fill["entry"]["price"] == pytest.approx(9.8)
    assert fill["labels"]["r_d1"] == pytest.approx((11.0 / 9.8 - 1) * 100)
    assert fill["labels"]["r_d2"] == pytest.approx((12.0 / 9.8 - 1) * 100)
    assert fill["labels"]["bench_d1"] == pytest.approx(2.0)      # 全A等权 1.0→1.02
    assert fill["labels"]["excess_d1"] == pytest.approx((11.0 / 9.8 - 1) * 100 - 2.0)
    assert fill["status"] == "settled"
    gap = by["GAP"]
    assert gap["entry"]["filled"] is False and gap["status"] == "not_entered"
    assert gap["labels"]["r_d1"] is None and gap["labels"]["r_d2"] is None


# ── ⑤ 分层:强弱 × 已动/board_已动,龙头跟涨分列 ─────────────────────────────
def test_summarize_layering(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([
        {"board": "电子", "tag": "利好", "强弱": "强",
         "龙头候选": [{"code": "L强动", "name": "a", "已动": True}], "跟涨候选": [
             {"code": "F强", "name": "f", "联动依据": "x"}]},
        {"board": "军工", "tag": "利好", "强弱": "弱",
         "龙头候选": [{"code": "L弱静", "name": "b", "已动": False}], "跟涨候选": []},
    ])
    _write_block(root, DATE, blk)
    _mock_kline(monkeypatch, {
        "L强动": [(DATE, 10, 10, 10.0), (CAL[1], 10, 9, 11.0), (CAL[2], 11, 11, 12.0)],
        "L弱静": [(DATE, 20, 20, 20.0), (CAL[1], 20, 19, 22.0), (CAL[2], 22, 22, 24.0)],
        "F强":   [(DATE, 5, 5, 5.0), (CAL[1], 5, 4.5, 5.5), (CAL[2], 5.5, 5.5, 6.0)],
    })
    _mock_ew(monkeypatch, {})
    M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    s = M.summarize(str(tmp_path / "out"))
    # 龙头/跟涨分列存在
    assert s["龙头"]["n_候选"] == 2 and s["跟涨_联动观察"]["n_候选"] == 1
    # 强弱分层
    assert s["龙头"]["按强弱"]["强"]["n_候选"] == 1
    assert s["龙头"]["按强弱"]["弱"]["n_候选"] == 1
    # 已动分层(龙头)
    assert s["龙头"]["按已动"]["True"]["n_候选"] == 1
    assert s["龙头"]["按已动"]["False"]["n_候选"] == 1
    # 跟涨按 board_已动
    assert "按board_已动" in s["跟涨_联动观察"]


# ── ⑥ 样本 <120 只报 N、不下结论 ───────────────────────────────────────────
def test_sample_threshold_reports_N(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "强",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": True}], "跟涨候选": []}])
    _write_block(root, DATE, blk)
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10.0), (CAL[1], 10, 9, 11.0),
                                         (CAL[2], 11, 11, 12.0)]})
    _mock_ew(monkeypatch, {})
    M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    s = M.summarize(str(tmp_path / "out"))
    assert s["min_sample"] == 120
    assert s["样本充足_龙头"] is False
    assert "只报 N" in s["结论"]


# ── ⑥ append-only 回填:只填 None、不改写已结算;幂等 ────────────────────────
def test_backfill_append_only(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "强",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": True}], "跟涨候选": []}])
    _write_block(root, DATE, blk)
    _mock_ew(monkeypatch, {})
    out = str(tmp_path / "out")
    # 首跑:只有 D 行(D+1 未到期)→ pending、entry.limit 已锁、无收益
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10.0)]})
    adv = M.run_daily(DATE, out, data_root=str(root))
    rec = adv["龙头票"][0]
    assert rec["status"] == "pending" and rec["entry"]["limit"] == 10.0
    assert rec["labels"]["r_d1"] is None and adv["label_status"] == "pending"
    # 到期后:K 线补出 D+1/D+2 → backfill 填 None cell、结算
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10.0), (CAL[1], 9.8, 9.5, 11.0),
                                         (CAL[2], 11.5, 11.0, 12.0)]})
    r1 = M.backfill_labels(out, data_root=str(root))
    assert r1["n_filled"] == 2 and r1["days_touched"] == 1
    adv2 = json.loads((tmp_path / "out" / f"{DATE}.json").read_text(encoding="utf-8"))
    rec2 = adv2["龙头票"][0]
    assert rec2["entry"]["filled"] is True and rec2["entry"]["price"] == pytest.approx(9.8)
    assert rec2["labels"]["r_d1"] == pytest.approx((11.0 / 9.8 - 1) * 100)
    assert rec2["status"] == "settled" and adv2["label_status"] == "settled"
    # 限价(D 收盘)回填前后不变(append-only、不改写既有 cell)
    assert rec2["entry"]["limit"] == 10.0
    # 幂等:再 backfill 不再动
    r2 = M.backfill_labels(out, data_root=str(root))
    assert r2["n_filled"] == 0 and r2["days_touched"] == 0


# ── 缺块/幂等守卫 ────────────────────────────────────────────────────────────
def test_missing_block_and_idempotent(tmp_path, monkeypatch):
    _bootstrap_dataroot(monkeypatch)
    root = tmp_path / "data"
    (root / "analysis" / DATE).mkdir(parents=True)
    _mock_kline(monkeypatch, {})
    _mock_ew(monkeypatch, {})
    # 无 sector_focus / 无 catalyst → 块缺失 → None、按常规
    monkeypatch.setattr(M, "_block_from_catalyst", lambda date, dr: None)
    assert M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root)) is None
    # 有块:首跑落盘,再跑幂等跳过(不因二次跑改盘)
    blk = _block([{"board": "电子", "tag": "利好", "强弱": "中",
                   "龙头候选": [{"code": "600001", "name": "甲", "已动": False}], "跟涨候选": []}])
    _write_block(root, DATE, blk)
    _mock_kline(monkeypatch, {"600001": [(DATE, 10, 10, 10.0)]})
    a1 = M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    p = tmp_path / "out" / f"{DATE}.json"
    mtime1 = p.stat().st_mtime_ns
    a2 = M.run_daily(DATE, str(tmp_path / "out"), data_root=str(root))
    assert a1 is not None and a2 is not None
    assert p.stat().st_mtime_ns == mtime1        # 幂等:未 --force 不重写
