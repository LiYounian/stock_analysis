"""策略 S01「趋势深跌反包」入场 Screener 单测。

锁死规格红线(改坏即挂):
  · C1 均线完整多头 + C≥MA5;C2 贴近52周高(H52 **不含当日**);
  · C3 近10日偏强(平盘 C==O **不计**);C4 深跌**收阳**(C>O 严格,平/阴不算);
  · SELECT = C1 AND C2 AND C3 AND C4;历史<251「不足不选」。
  · 防未来函数:H52 只取 [t-250, t-1],当日 HIGH 极端值不得污染。
"""
import numpy as np
import pandas as pd
import pytest

from tools.collectors import market
from tools.pipeline import screen_s01 as s01
from tools.store import repo as store


def _uptrend(n: int = 260, start: float = 100.0, step: float = 0.5) -> dict:
    """严格上行趋势(closes 单调增 → MA5>MA10>...>MA200);每根收阳(open<close)。

    返回可变 dict(open/high/low/close 列表),便于测试改写指定根。
    """
    close = [start + i * step for i in range(n)]
    open_ = [c - 0.2 for c in close]                 # 收阳
    high = [c + 0.1 for c in close]
    low = [o - 0.1 for o in open_]
    return {"open": open_, "high": high, "low": low, "close": close}


def _set_trigger_last(d: dict, drop_frac: float = -0.05, green: bool = True) -> None:
    """把最后一根设为「深跌但收盘回到趋势位」:LOW 下探 drop_frac,收盘≈趋势(≥MA5)。"""
    t = len(d["close"]) - 1
    prev_c = d["close"][t - 1]
    d["close"][t] = prev_c + 0.1                       # 收盘回到趋势位(保证 ≥ MA5、在 C2 带内)
    d["open"][t] = d["close"][t] - 1.0 if green else d["close"][t] + 1.0
    d["low"][t] = prev_c * (1 + drop_frac)             # 盘中深跌
    d["high"][t] = d["close"][t] + 0.1


def _df(d: dict) -> pd.DataFrame:
    n = len(d["close"])
    return pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "volume": [1000.0] * n,
    })


# ———————————————————— SELECT 全满足 ————————————————————
def test_select_all_conditions_true():
    d = _uptrend()
    _set_trigger_last(d)
    r = s01.screen_latest(_df(d))
    assert r["C1_均线多头"] and r["C2_贴近52周高"] and r["C3_近强"] and r["C4_深跌收阳"]
    assert r["SELECT"] is True


# ———————————————————— C1 ————————————————————
def test_c1_fail_when_not_bullish_stack():
    """打乱均线多头(把序列改成下行)→ C1 假 → 不入选。"""
    n = 260
    close = [200.0 - i * 0.3 for i in range(n)]        # 单调降 → MA 空头排列
    d = {"open": [c + 0.2 for c in close], "high": [c + 0.3 for c in close],
         "low": [c - 0.3 for c in close], "close": close}
    _set_trigger_last(d)
    r = s01.screen_latest(_df(d))
    assert r["C1_均线多头"] is False and r["SELECT"] is False


def test_c1_requires_close_ge_ma5():
    """趋势多头但当日收盘压在 MA5 下方 → C1 假(C≥MA5 不满足)。"""
    d = _uptrend()
    _set_trigger_last(d)
    t = len(d["close"]) - 1
    # 收盘砸到远低于近5日均线(仍收阳:open 更低),破坏 C≥MA5,但 MA 排列仍多头
    ma5 = np.mean(d["close"][t - 4:t + 1])
    d["close"][t] = ma5 - 5.0
    d["open"][t] = d["close"][t] - 0.5
    r = s01.screen_latest(_df(d))
    assert r["C1_均线多头"] is False


# ———————————————————— C2(含防未来函数)————————————————————
def test_c2_fail_when_price_far_below_h52():
    d = _uptrend()
    _set_trigger_last(d)
    t = len(d["close"]) - 1
    h52 = max(d["high"][t - 250:t])
    d["close"][t] = 0.5 * h52                          # 远低于 0.9·H52
    d["open"][t] = d["close"][t] - 1.0
    r = s01.screen_latest(_df(d))
    assert r["C2_贴近52周高"] is False


def test_h52_excludes_current_day():
    """当日 HIGH 设成天价:若 H52 含当日 → C2 会因 0.9·H52 高于收盘而假;
    规格 H52 不含当日 → C2 仍真。用此锁死防未来函数。"""
    d = _uptrend()
    _set_trigger_last(d)
    t = len(d["close"]) - 1
    prior_max = max(d["high"][t - 250:t])
    d["high"][t] = prior_max * 100                     # 当日极端高点(应被 H52 忽略)
    r = s01.screen_latest(_df(d))
    assert r["明细"]["H52"] == pytest.approx(round(prior_max, 4))
    assert r["C2_贴近52周高"] is True                   # 未被当日天价污染


# ———————————————————— C3(平盘不计)————————————————————
def _c3_probe(last10_open_close):
    """构造末 10 根的 (open, close),其余为干净上行,只测 C3。返回 signal_at 结果。"""
    d = _uptrend()
    t = len(d["close"]) - 1
    for k, (o, c) in enumerate(last10_open_close):
        i = t - 9 + k
        d["open"][i] = o
        d["close"][i] = c
        d["high"][i] = max(o, c) + 0.1
        d["low"][i] = min(o, c) - 0.1
    return s01.signal_at(_df(d), t)


def test_c3_flat_bars_not_counted():
    """近10日:3 阳 2 阴 5 平盘 → COUNT(C>O)=3 > COUNT(C<O)=2 → C3 真(平盘被忽略)。"""
    bars = [(10, 11), (10, 11), (10, 11),            # 3 阳
            (10, 9), (10, 9),                        # 2 阴
            (10, 10), (10, 10), (10, 10), (10, 10), (10, 10)]  # 5 平盘
    r = _c3_probe(bars)
    assert r["明细"]["近强_涨/跌"] == [3, 2] and r["C3_近强"] is True


def test_c3_false_when_down_ge_up_ignoring_flats():
    """2 阳 3 阴 5 平盘 → 3>2 不成立 → C3 假(证明平盘既不帮阳也不帮阴)。"""
    bars = [(10, 11), (10, 11),                      # 2 阳
            (10, 9), (10, 9), (10, 9),               # 3 阴
            (10, 10), (10, 10), (10, 10), (10, 10), (10, 10)]  # 5 平盘
    r = _c3_probe(bars)
    assert r["明细"]["近强_涨/跌"] == [2, 3] and r["C3_近强"] is False


# ———————————————————— C4(深跌 + 收阳定义)————————————————————
def test_c4_true_deep_drop_and_green():
    d = _uptrend()
    _set_trigger_last(d, drop_frac=-0.06, green=True)
    r = s01.screen_latest(_df(d))
    assert r["C4_深跌收阳"] is True and r["明细"]["当日跌幅"] <= -0.04


def test_c4_false_when_not_green_even_if_deep_drop():
    """深跌到位但收阴(C<O)→ C4 假(收阳是硬定义)。"""
    d = _uptrend()
    _set_trigger_last(d, drop_frac=-0.06, green=False)  # open>close → 阴
    r = s01.screen_latest(_df(d))
    assert r["明细"]["当日跌幅"] <= -0.04                # 深跌确实到位
    assert r["明细"]["收阳"] is False and r["C4_深跌收阳"] is False


def test_c4_false_when_flat_close_equals_open():
    """C==O(平盘)不算收阳 → C4 假(C>O 严格)。"""
    d = _uptrend()
    _set_trigger_last(d, drop_frac=-0.06, green=True)
    t = len(d["close"]) - 1
    d["open"][t] = d["close"][t]                         # 平盘
    r = s01.screen_latest(_df(d))
    assert r["C4_深跌收阳"] is False


def test_c4_false_when_drop_too_shallow():
    """盘中仅跌 2%(< 4%)→ C4 假。"""
    d = _uptrend()
    _set_trigger_last(d, drop_frac=-0.02, green=True)
    r = s01.screen_latest(_df(d))
    assert r["C4_深跌收阳"] is False


# ———————————————————— 历史不足:不足不选 ————————————————————
def test_insufficient_history_not_selected():
    d = _uptrend(n=200)                                 # <251
    _set_trigger_last(d)
    r = s01.screen_latest(_df(d))
    assert r["SELECT"] is False and "历史不足" in r.get("原因", "")


# ———————————————————— pipeline 落 view ————————————————————
def test_run_s01_screen_writes_view(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    good = _uptrend()
    _set_trigger_last(good)
    short = _uptrend(n=100)                             # 历史不足 → 跳过
    kl = {"GOOD": _df(good), "SHORT": _df(short)}
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    v = s01.run_s01_screen(["GOOD", "SHORT"], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 1
    assert [x["code"] for x in v["入选清单"]] == ["GOOD"]
    assert v["跳过数(历史不足)"] == 1 and v["有效样本"] == 1
    # 落库可回读
    got = store.get_view("趋势深跌反包", date="2024-06-01")
    assert got["入选数"] == 1


# ———————————————————— 可选入场确认 confirm_entry(向后兼容)————————————————————
def test_confirm_entry_none_returns_signal_day():
    """无确认(mode=None)→ 返回信号日 t 本身(保持原行为)。"""
    d = _uptrend()
    df = _df(d)
    t = len(df) - 2                                     # 留一根做次日
    assert s01.confirm_entry(df, t, None) == t
    assert s01.confirm_entry(df, t, "none") == t


def test_confirm_entry_t1_nobreak_pass():
    """T+1 不破低 → 返回 t+1(在次日入场)。构造次日 low ≥ 信号日 low。"""
    d = _uptrend()
    t = len(d["close"]) - 2
    d["low"][t] = d["close"][t] - 2.0                   # 信号日低点
    d["low"][t + 1] = d["low"][t] + 0.5                 # 次日不破低
    df = _df(d)
    assert s01.confirm_entry(df, t, "t1_nobreak") == t + 1


def test_confirm_entry_t1_nobreak_reject_on_break():
    """T+1 跌破信号日最低价 → 放弃该信号(返回 None),避免接飞刀。"""
    d = _uptrend()
    t = len(d["close"]) - 2
    d["low"][t] = d["close"][t] - 2.0
    d["low"][t + 1] = d["low"][t] - 0.5                 # 次日破低
    df = _df(d)
    assert s01.confirm_entry(df, t, "t1_nobreak") is None


def test_confirm_entry_t1_no_next_day_returns_none():
    """信号日是最后一根、无次日数据 → 无法确认 → None。"""
    d = _uptrend()
    df = _df(d)
    t = len(df) - 1
    assert s01.confirm_entry(df, t, "t1_nobreak") is None
