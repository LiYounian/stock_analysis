"""日内实时观测循环单测。

正确性红线(改坏即挂):
  · 规则命中口径:涨幅突破→买入倾向 / 跌幅预警→卖出倾向 / 相对午盘参考价急拉急跌 / 量比放量;
  · ref_price 缺 → 不判急拉急跌(不假造参考);price 缺 → 无事件(缺失不猜);
  · **防未来/静态值**:quote_time 未推进 → 本轮不触发(非交易时段冻结值不误触发);
  · **迟滞去重**:同 (code,规则,方向) 未回落只触发一次;回落到阈值内后允许再触发;
  · 循环:注入 quote_fn/sleep,按轮落 jsonl,累计条数正确。

hermetic:注入报价与 sleep,monkeypatch 产出根到 tmp,不触网、不写真实 data/。
"""
import json

from tools.pipeline import intraday_watch as w


def _q(price, pct=None, vr=None, qt="20260907143000"):
    return {"price": price, "pct_chg": pct, "vol_ratio": vr, "quote_time": qt}


# ── evaluate_quote ──

def test_up_and_vol_rules():
    cfg = w.WatchConfig()
    hits = w.evaluate_quote("600519", _q(110, pct=8.0, vr=3.0), ref_price=None, cfg=cfg)
    rules = {(h["rule"], h["direction"]) for h in hits}
    assert ("涨幅突破", w.BUY) in rules
    assert ("放量", w.WATCH) in rules


def test_down_rule():
    hits = w.evaluate_quote("000001", _q(9, pct=-6.0), ref_price=None, cfg=w.WatchConfig())
    assert ("跌幅预警", w.SELL) in {(h["rule"], h["direction"]) for h in hits}


def test_surge_plunge_need_ref():
    cfg = w.WatchConfig()
    # 相对午盘参考价 100 → 现价 104 = +4% ≥ surge 3% → 买入倾向
    up = w.evaluate_quote("300308", _q(104, pct=2.0), ref_price=100.0, cfg=cfg)
    assert ("急拉", w.BUY) in {(h["rule"], h["direction"]) for h in up}
    # 无 ref → 不判急拉/急跌
    none = w.evaluate_quote("300308", _q(104, pct=2.0), ref_price=None, cfg=cfg)
    assert all(h["rule"] not in ("急拉", "急跌") for h in none)


def test_missing_price_no_event():
    assert w.evaluate_quote("600519", _q(None, pct=9.0), ref_price=100.0, cfg=w.WatchConfig()) == []


# ── filter_new_events:防未来 + 迟滞去重 ──

def test_static_quote_time_no_trigger():
    st = w.WatchState()
    e = [{"code": "600519", "rule": "涨幅突破", "direction": w.BUY}]
    first = w.filter_new_events(st, "600519", "20260907143000", list(e))
    assert len(first) == 1
    # 同一 quote_time 再来 → 静态,不触发
    again = w.filter_new_events(st, "600519", "20260907143000", list(e))
    assert again == []


def test_dedup_then_reset_after_falloff():
    st = w.WatchState()
    e = [{"code": "600519", "rule": "涨幅突破", "direction": w.BUY}]
    assert len(w.filter_new_events(st, "600519", "t1", list(e))) == 1
    # quote_time 推进但仍命中 → 已触发未回落,不重复
    assert w.filter_new_events(st, "600519", "t2", list(e)) == []
    # 回落(本轮无命中)→ 清除;再命中 → 允许再触发
    assert w.filter_new_events(st, "600519", "t3", []) == []
    assert len(w.filter_new_events(st, "600519", "t4", list(e))) == 1


# ── run_watch 循环 ──

def test_run_watch_writes_events(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "OUT_ROOT", tmp_path)
    # 脚本化报价:第1轮命中涨幅突破,后续 quote_time 不变(静态)→ 只 1 条
    scripted = [
        {"600519": _q(110, pct=8.0, qt="t1")},
        {"600519": _q(110, pct=8.0, qt="t1")},
        {"600519": _q(110, pct=8.0, qt="t1")},
    ]
    calls = {"i": 0}

    def fake_fetch(codes):
        r = scripted[min(calls["i"], len(scripted) - 1)]
        calls["i"] += 1
        return r

    total = w.run_watch(["600519"], date="2026-09-07", max_iters=3,
                        quote_fn=fake_fetch, sleep_fn=lambda s: None)
    assert total == 1                      # 静态值不重复触发
    lines = (tmp_path / "2026-09-07" / "watch_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["code"] == "600519" and rec["rule"] == "涨幅突破" and "emitted_at" in rec


def test_run_watch_survives_fetch_error(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "OUT_ROOT", tmp_path)

    def boom(codes):
        raise ConnectionError("网络抖动")

    # 拉取整轮异常也不崩,累计 0 条
    total = w.run_watch(["600519"], date="2026-09-07", max_iters=2,
                        quote_fn=boom, sleep_fn=lambda s: None)
    assert total == 0


# ── 午休参考价加载 ──

def test_load_ref_prices(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "OUT_ROOT", tmp_path)
    snap = {"codes": {"300308": {"price": 12.34}, "002234": {"price": None}}}
    d = tmp_path / "2026-09-07"
    d.mkdir()
    (d / "T1145.json").write_text(json.dumps(snap), encoding="utf-8")
    ref = w.load_ref_prices("2026-09-07")
    assert ref == {"300308": 12.34}          # price=None 的不进(缺失不假造)


def test_load_ref_prices_missing_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "OUT_ROOT", tmp_path)
    assert w.load_ref_prices("2026-09-07") == {}   # 快照缺 → 空,不报错
