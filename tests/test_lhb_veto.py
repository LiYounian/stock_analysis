"""lhb_veto.py 龙虎榜否决/反转风控信号单测(纯离线,不触网)。

锁的语义(防未来 + 否决/反转口径):
- **防未来函数红线**:裁决只用 list_date **严格小于 as_of** 的上榜(盘后披露,上榜日当天不可用);
- 近日窗口:仅 as_of 前 window_days 自然日内的上榜才算活跃,过期不触发;
- entry_veto/exit:净买上榜(dir=+1 且占比≥门槛)→ 触发否决/离场;
- reversal:净卖上榜(dir=−1 且 |占比|≥门槛)→ 触发反转候选;
- 门槛过滤:min_net_buy_ratio / min_net_sell_ratio 生效;
- 多条命中取最近一条锚定,n_recent 计窗口内同向条数;
- veto_asof 走 lhb_asof(严格<as_of),无快照 → 保守放行(不触发)。
"""
import pytest

from tools.backtest import lhb_veto as V
from tools.collectors import lhb
from tools.store import repo as store


def _ev(list_date, direction, ratio):
    return {"list_date": list_date, "direction": direction, "net_buy_ratio": ratio}


# ———————————— 防未来函数:上榜日当天不可用 ————————————
def test_no_future_function_sameday():
    """as_of == list_date:盘后披露,当天不可用 → 不触发。"""
    v = V.verdict_from_events([_ev("2024-01-10", 1, 9.0)], "2024-01-10")
    assert v.triggered is False


def test_no_future_function_after_asof():
    """list_date > as_of(未来上榜)绝不纳入。"""
    v = V.verdict_from_events([_ev("2024-01-15", 1, 20.0)], "2024-01-10")
    assert v.triggered is False


# ———————————— entry_veto / exit:净买上榜触发 ————————————
def test_entry_veto_triggers_on_recent_net_buy():
    v = V.verdict_from_events([_ev("2024-01-08", 1, 12.0)], "2024-01-10",
                              mode=V.MODE_ENTRY_VETO)
    assert v.triggered is True
    assert v.direction == 1 and v.list_date == "2024-01-08" and v.days_since == 2


def test_exit_mode_same_trigger_as_entry():
    """exit 与 entry_veto 触发条件一致(施加对象不同),净买上榜均触发。"""
    kw = dict(events=[_ev("2024-01-09", 1, 8.0)], as_of="2024-01-10")
    assert V.verdict_from_events(mode=V.MODE_EXIT, **kw).triggered
    assert V.verdict_from_events(mode=V.MODE_ENTRY_VETO, **kw).triggered


def test_entry_veto_not_triggered_by_net_sell():
    """净卖上榜不触发入选否决(方向不符)。"""
    v = V.verdict_from_events([_ev("2024-01-09", -1, -8.0)], "2024-01-10",
                              mode=V.MODE_ENTRY_VETO)
    assert v.triggered is False


# ———————————— 近日窗口 ————————————
def test_window_gate_excludes_stale():
    """超出 window_days 的旧上榜不再活跃 → 不触发。"""
    v = V.verdict_from_events([_ev("2024-01-01", 1, 15.0)], "2024-01-20",
                              window_days=7)
    assert v.triggered is False
    # 放宽窗口则触发
    v2 = V.verdict_from_events([_ev("2024-01-01", 1, 15.0)], "2024-01-20",
                               window_days=30)
    assert v2.triggered is True


# ———————————— 门槛过滤 ————————————
def test_min_ratio_gate():
    """净买占比低于门槛 → 不触发(只否决强追高)。"""
    weak = V.verdict_from_events([_ev("2024-01-09", 1, 3.0)], "2024-01-10",
                                 min_net_buy_ratio=5.0)
    assert weak.triggered is False
    strong = V.verdict_from_events([_ev("2024-01-09", 1, 8.0)], "2024-01-10",
                                   min_net_buy_ratio=5.0)
    assert strong.triggered is True


# ———————————— 反转:净卖上榜 ————————————
def test_reversal_triggers_on_net_sell():
    v = V.verdict_from_events([_ev("2024-01-09", -1, -6.0)], "2024-01-10",
                              mode=V.MODE_REVERSAL)
    assert v.triggered is True and v.direction == -1


def test_reversal_not_triggered_by_net_buy():
    v = V.verdict_from_events([_ev("2024-01-09", 1, 6.0)], "2024-01-10",
                              mode=V.MODE_REVERSAL)
    assert v.triggered is False


# ———————————— 多条命中取最近 + 计数 ————————————
def test_multiple_hits_anchor_latest():
    evs = [_ev("2024-01-05", 1, 6.0), _ev("2024-01-09", 1, 10.0),
           _ev("2024-01-07", 1, 8.0)]
    v = V.verdict_from_events(evs, "2024-01-10", mode=V.MODE_ENTRY_VETO)
    assert v.triggered is True
    assert v.list_date == "2024-01-09"      # 最近一条锚定
    assert v.n_recent == 3                   # 窗口内 3 条同向命中


def test_ratio_falls_back_to_sig_field():
    """lab 长表用 sig 名承载 net_buy_ratio,应能回退取到。"""
    v = V.verdict_from_events([{"list_date": "2024-01-09", "direction": 1, "sig": 7.0}],
                              "2024-01-10", min_net_buy_ratio=5.0)
    assert v.triggered is True and v.net_buy_ratio == 7.0


# ———————————— veto_asof 走落盘(严格<as_of + 无快照放行) ————————————
def _raw_row(code, list_date, net_buy, reason="X"):
    return {
        "代码": code, "名称": "某股", "上榜日": list_date, "解读": "游资",
        "收盘价": 1.66, "涨跌幅": 5.0, "龙虎榜净买额": net_buy,
        "龙虎榜买入额": 700.0, "龙虎榜卖出额": 400.0, "龙虎榜成交额": 1100.0,
        "市场总成交额": 1e7, "净买额占总成交比": 11.9, "成交额占总成交比": 48.2,
        "换手率": 0.04, "流通市值": 2.5e10, "上榜原因": reason,
    }


def test_veto_asof_reads_store_strict_less_than(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    import pandas as pd
    df = pd.DataFrame([lhb._norm_row(_raw_row("000564", "2024-01-05", 954439.0))])
    monkeypatch.setattr(lhb, "fetch_range_df", lambda *a, **k: df)
    lhb.fetch_lhb("20240101", "20240110")
    # as_of == 上榜日:严格小于 → 不触发
    assert V.veto_asof("000564", "2024-01-05").triggered is False
    # as_of 上榜后次日:触发否决
    v = V.veto_asof("000564", "2024-01-06")
    assert v.triggered is True and v.direction == 1


def test_veto_asof_missing_snapshot_permits(monkeypatch, tmp_path):
    """无龙虎榜快照(从未上榜/未采集)→ 保守放行(不触发),由上层其它风控兜底。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    v = V.veto_asof("999999", "2024-01-10")
    assert v.triggered is False and "无龙虎榜快照" in v.reason


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
