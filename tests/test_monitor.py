"""盯盘监控 MVP 测试(tools/monitor)。

锁住"为什么这么设计"的语义:
  · schema JSON 往返不丢字段 / 向后兼容忽略未知键
  · 触发求值:<=,>=,cross_down,cross_up,time 各分支
  · 去抖:once 命中一次后不复弹 + jsonl 回放恢复 fired
  · md 解析:日内D-0表 抽 entry/stop/take;隔夜卡表 抽挂单/止损/不追高
  · merge 去重合并触发
  · notifier 非macOS降级不炸
  · 交易时段判定
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tools.monitor import converter, engine
from tools.monitor.notify import DesktopNotifier, LogNotifier, get_notifier
from tools.monitor.schema import CST, Alert, Trigger, WatchItem, Watchlist


# ── schema 往返 ─────────────────────────────────────────────
def test_watchlist_roundtrip():
    wl = Watchlist(date="2026-09-17", items=[
        WatchItem(code="601061", name="测试票", role="买入", ref_price=11.14, triggers=[
            Trigger(id="entry", kind="entry_limit", op="<=", value=11.2, action="进场"),
            Trigger(id="stop", kind="stop_loss", op="<=", value=10.67, action="止损"),
        ], gates=[Trigger(id="eod", kind="time", at="14:57", action="了结")]),
    ], source={"type": "selection_md"})
    d = wl.to_dict()
    wl2 = Watchlist.from_dict(d)
    assert wl2.date == "2026-09-17"
    assert wl2.items[0].code == "601061"
    assert wl2.items[0].triggers[0].value == 11.2
    assert wl2.items[0].gates[0].at == "14:57"


def test_trigger_from_dict_ignores_unknown_keys():
    t = Trigger.from_dict({"id": "x", "kind": "stop_loss", "op": "<=", "value": 1.0,
                           "future_field": "ignore_me"})
    assert t.id == "x" and t.value == 1.0


def test_watchlist_save_load(tmp_path):
    wl = Watchlist(date="2026-09-17", items=[WatchItem(code="000001", name="平安")])
    wl.save(root=tmp_path)
    loaded = Watchlist.load("2026-09-17", root=tmp_path)
    assert loaded is not None and loaded.items[0].code == "000001"
    assert Watchlist.load("2020-01-01", root=tmp_path) is None


# ── 触发求值 ─────────────────────────────────────────────
def test_evaluate_stop_and_take():
    it = WatchItem(code="c", name="n", triggers=[
        Trigger(id="stop", kind="stop_loss", op="<=", value=10.0, action="止损"),
        Trigger(id="take", kind="take_profit", op=">=", value=12.0, action="止盈"),
    ])
    hit_stop = engine.evaluate(it, {"price": 9.9})
    assert [a.trigger_id for a in hit_stop] == ["stop"]
    hit_take = engine.evaluate(it, {"price": 12.5})
    assert [a.trigger_id for a in hit_take] == ["take"]
    assert engine.evaluate(it, {"price": 11.0}) == []


def test_evaluate_cross_down_needs_prev():
    it = WatchItem(code="c", name="n", triggers=[
        Trigger(id="x", kind="stop_loss", op="cross_down", value=10.0, action="破位"),
    ])
    assert engine.evaluate(it, {"price": 9.9}, prev=None) == []          # 无上轮不判穿越
    assert engine.evaluate(it, {"price": 9.9}, prev={"price": 10.5})     # 上轮在阈上→穿越
    assert engine.evaluate(it, {"price": 9.9}, prev={"price": 9.8}) == []  # 上轮已在阈下→不重触


def test_evaluate_missing_field_no_alert():
    it = WatchItem(code="c", name="n", triggers=[
        Trigger(id="s", kind="stop_loss", op="<=", value=10.0)])
    assert engine.evaluate(it, {}) == []                                 # 停牌/缺价 不误触发


def test_evaluate_time_gate():
    it = WatchItem(code="c", name="n", gates=[
        Trigger(id="eod", kind="time", at="14:57", action="了结")])
    before = datetime(2026, 9, 17, 14, 0, tzinfo=CST)
    after = datetime(2026, 9, 17, 14, 58, tzinfo=CST)
    assert engine.evaluate(it, {"price": 1}, now=before) == []
    assert [a.trigger_id for a in engine.evaluate(it, {"price": 1}, now=after)] == ["eod"]


# ── 去抖 / 回放 ─────────────────────────────────────────────
def test_once_dedup_and_replay(tmp_path):
    wl = Watchlist(date="2026-09-17", items=[WatchItem(code="000001", name="平安", triggers=[
        Trigger(id="stop", kind="stop_loss", op="<=", value=10.0, action="止损", once=True)])])

    calls = []

    class _Rec(LogNotifier):
        def notify(self, alert):
            calls.append(alert.trigger_id)

    # 强制价格恒在止损下方,跑两轮:once 应只弹一次。
    import tools.monitor.engine as eng
    orig = eng.gtimg_quote.fetch_quotes
    eng.gtimg_quote.fetch_quotes = lambda codes: {"000001": {"price": 9.5}}
    try:
        eng.run(wl, _Rec(), interval=0, root=tmp_path, max_rounds=3, respect_session=False)
    finally:
        eng.gtimg_quote.fetch_quotes = orig
    assert calls == ["stop"]                                              # 三轮只弹一次

    # jsonl 落盘 + 回放:重启后 fired 恢复,不再弹。
    apath = tmp_path / "2026-09-17_alerts.jsonl"
    assert apath.exists()
    fired = eng._load_fired("2026-09-17", tmp_path)
    assert ("000001", "stop") in fired


# ── md 解析 ─────────────────────────────────────────────
_INTRADAY_MD = """# 全A午盘选股 · 日内_2026-09-17

## 今日可买入 · D-0 交易计划

| 序 | 代码 | 名称 | 现价(11:30) | 进场触发 | 止损位 | 止盈位 | 波动锚 | 了结纪律 |
|---|---|---|---|---|---|---|---|---|
| 1 | 601061 | 测试甲 | 11.14 | 尾盘买入,现价≤11.2不追高;已跌破止损 10.67 则放弃 | 10.67(−4.26%) | 11.85(+6.39%) | ATR14 4.26% | 当日了结 |
| 2 | 300502 | 新易盛 | 426.64 | 现价≤428.77不追高 | 404.01(−5.3%) | 460.59(+7.96%) | ATR14 5.30% | 当日了结 |
"""

_OVERNIGHT_MD = """# 隔夜选股 2026-09-16
<!-- PICKS: 002811,600000 -->

| 序 | 代码 | 名称 | 挂单价 | 止损价 | 不追高上限 | 入场方式 |
|---|---|---|---|---|---|---|
| 1 | 002811 | 郑中设计 | 51.2 | 49.5 | 52.0 | 回踩MA5 |
"""


def test_parse_intraday_md(tmp_path):
    p = tmp_path / "2026-09-17.md"
    p.write_text(_INTRADAY_MD, encoding="utf-8")
    wl = converter.from_selection_md(p)
    assert wl.date == "2026-09-17"
    assert {it.code for it in wl.items} == {"601061", "300502"}
    a = wl.get("601061")
    tby = {t.id: t.value for t in a.triggers}
    assert tby["entry"] == 11.2 and tby["stop"] == 10.67 and tby["take"] == 11.85
    assert a.ref_price == 11.14
    assert any(g.kind == "time" for g in a.gates)                        # 日内→收盘了结闸门


def test_parse_overnight_md(tmp_path):
    p = tmp_path / "2026-09-16.md"
    p.write_text(_OVERNIGHT_MD, encoding="utf-8")
    wl = converter.from_selection_md(p)
    a = wl.get("002811")
    tby = {t.id: t.value for t in a.triggers}
    assert tby["entry"] == 51.2 and tby["stop"] == 49.5 and tby["cap"] == 52.0


# ── merge ─────────────────────────────────────────────
def test_merge_dedup():
    a = Watchlist(date="d", items=[WatchItem(code="1", name="A", role="买入",
                  triggers=[Trigger(id="stop", kind="stop_loss", op="<=", value=10.0)])])
    b = Watchlist(date="d", items=[
        WatchItem(code="1", name="A", role="自选",
                  triggers=[Trigger(id="take", kind="take_profit", op=">=", value=12.0)]),
        WatchItem(code="2", name="B", role="自选"),
    ])
    m = converter.merge(a, b)
    assert {it.code for it in m.items} == {"1", "2"}
    one = m.get("1")
    assert {t.id for t in one.triggers} == {"stop", "take"}              # 触发合并
    assert one.role == "买入"                                           # 非自选优先


# ── notifier ─────────────────────────────────────────────
def test_desktop_notifier_degrades(monkeypatch):
    import tools.monitor.notify as nt
    monkeypatch.setattr(nt.platform, "system", lambda: "Linux")
    n = DesktopNotifier()
    assert n._enabled is False
    n.notify(Alert(code="c", name="n", trigger_id="t", kind="stop_loss",
                   action="止损", price=1.0, value=2.0))                # 不抛异常即通过


def test_get_notifier_fallback():
    assert get_notifier("nonexistent").__class__.__name__ == "DesktopNotifier"
    assert get_notifier("log").name == "log"


# ── 交易时段 ─────────────────────────────────────────────
def test_in_trading_session(monkeypatch):
    import tools.monitor.engine as eng
    monkeypatch.setattr(eng.cal, "is_trading_day", lambda d: True)
    assert eng.in_trading_session(datetime(2026, 9, 17, 10, 0, tzinfo=CST))
    assert eng.in_trading_session(datetime(2026, 9, 17, 14, 0, tzinfo=CST))
    assert not eng.in_trading_session(datetime(2026, 9, 17, 12, 0, tzinfo=CST))   # 午休
    assert not eng.in_trading_session(datetime(2026, 9, 17, 8, 0, tzinfo=CST))    # 盘前
    monkeypatch.setattr(eng.cal, "is_trading_day", lambda d: False)
    assert not eng.in_trading_session(datetime(2026, 9, 17, 10, 0, tzinfo=CST))   # 非交易日
