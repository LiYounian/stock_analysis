"""策略 S01 持仓回测器(离场状态机)单测。

正确性红线(改坏即挂):
  · 离场 5 条**逐条**触发正确 + **优先级**(同日多命中取靠前者成交价);
  · P0=信号日收盘;硬止损以 P0×0.97 成交、其余收盘价;时间成本第 11 日;
  · 防未来函数:离场决策只用当日及之前,截断/追加未来 bar 不改已定离场;
  · 一字板(跌停一字)不可成交 → 顺延至下一可成交日并标注;涨跌停方向/板块幅度区分;
  · 汇总:胜率/中位收益/盈亏比/最大回撤/平均持有天数/Alpha。
"""
import numpy as np
import pandas as pd
import pytest

from tools.backtest import position_backtest as pb
from tools.pipeline import screen_s01 as s01

_CFG = pb._ALL


def _df(d: dict) -> pd.DataFrame:
    n = len(d["close"])
    return pd.DataFrame({
        "date": pd.bdate_range("2020-01-01", periods=n),
        "open": d["open"], "high": d["high"], "low": d["low"],
        "close": d["close"], "volume": d["volume"],
    })


def _flat(n: int = 40, price: float = 100.0, vol: float = 1000.0) -> dict:
    """平盘基座:close/open=price,微幅 high/low(避免误判一字);无离场触发。"""
    return {
        "open": [price] * n, "close": [price] * n,
        "high": [price + 0.2] * n, "low": [price - 0.2] * n,
        "volume": [vol] * n,
    }


def _set(d: dict, i: int, o=None, h=None, l=None, c=None, v=None) -> None:
    if o is not None: d["open"][i] = o
    if h is not None: d["high"][i] = h
    if l is not None: d["low"][i] = l
    if c is not None: d["close"][i] = c
    if v is not None: d["volume"][i] = v


ENTRY = 20                                              # 统一进场索引(P0=100)


# ———————————————————— 离场 5 条逐条 ————————————————————
def test_exit_rule1_hard_stop():
    """LOW≤P0×0.97 → 以 97 成交(非收盘价)。"""
    d = _flat()
    _set(d, ENTRY + 1, o=99, h=101, l=96, c=100)        # 盘中触及 96 ≤ 97
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 1 and tr["离场价"] == 97.0
    assert tr["持有天数"] == 1 and tr["收益"] == pytest.approx(-0.03)


def test_exit_rule2_trend_stop_below_ma13():
    """收盘跌破 MA13 → 收盘价离场(未触硬止损)。"""
    d = _flat()
    _set(d, ENTRY + 1, o=99, h=99.5, l=98, c=98)        # low 98>97 不触rule1;close98<MA13≈99.85
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 2 and tr["离场价"] == 98.0 and tr["持有天数"] == 1


def test_exit_rule3_accel_take_profit():
    """C/C₍ₜ₋₃₎−1 ≥ 0.25 → 收盘价离场。"""
    d = _flat()
    j = ENTRY + 3
    _set(d, j, o=125, h=131, l=124, c=130)              # 130/100-1=0.30 ≥ 0.25
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 3 and tr["离场价"] == 130.0 and tr["持有天数"] == 3


def test_exit_rule4_volume_stall():
    """放量(V>V₋₁×1.5)且涨幅递减 → 收盘价离场。"""
    d = _flat()
    _set(d, ENTRY + 4, c=105, v=1000)                   # 前一日 +5%
    _set(d, ENTRY + 5, o=106, h=107.5, l=104, c=107, v=2000)  # +1.9%<+5%,量2000>1500
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 4 and tr["离场价"] == 107.0 and tr["持有天数"] == 5


def test_exit_rule5_time_cost_11th_day():
    """持股>10 且 收益<5% → 第 11 日收盘强制离场。"""
    d = _flat(n=40)
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)      # 全程平盘,前 10 日无触发
    assert tr["离场规则号"] == 5 and tr["持有天数"] == 11 and tr["离场价"] == 100.0


def test_time_cost_not_fired_when_gain_ge_threshold():
    """持股>10 但已涨≥5% → 时间成本不触发(继续持有直到别的规则或数据末)。"""
    d = _flat(n=40)
    for i in range(ENTRY + 1, 40):
        _set(d, i, o=106, h=106.2, l=105.8, c=106)      # 稳在 +6% > 5%,且不触其它规则
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["状态"] == "持有中(数据不足)" and tr["收益"] is None


# ———————————————————— 优先级(同日多命中取靠前)————————————————————
def test_priority_rule1_over_rule2():
    """同日既触硬止损又跌破MA13 → 取靠前的硬止损(价 97,非收盘 96)。"""
    d = _flat()
    _set(d, ENTRY + 1, o=98, h=99, l=95, c=96)          # low95≤97(rule1) 且 close96<MA13(rule2)
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 1 and tr["离场价"] == 97.0   # 不是 96


def test_priority_rule2_over_rule5():
    """第 11 日既满足时间成本(rule5)又跌破MA13(rule2)→ 取靠前的 rule2。"""
    d = _flat(n=40)
    _set(d, ENTRY + 11, o=99, h=99.5, l=97.5, c=98)     # held=11;close98<MA13;low97.5>97 不触rule1
    tr = pb.simulate_position(_df(d), ENTRY, _CFG)
    assert tr["离场规则号"] == 2 and tr["持有天数"] == 11 and tr["离场价"] == 98.0


# ———————————————————— 防未来函数 ————————————————————
def test_no_lookahead_truncate_and_append_invariant():
    """已定离场不受其后 bar 影响:截断到离场日 / 追加暴跌未来 bar,离场日与价均不变。"""
    d = _flat()
    j = ENTRY + 3
    _set(d, j, o=125, h=131, l=124, c=130)              # rule3 于 j 离场
    full = pb.simulate_position(_df(d), ENTRY, _CFG)
    # 截断到离场日(其后不存在)
    trunc = {k: v[: j + 1] for k, v in d.items()}
    cut = pb.simulate_position(_df(trunc), ENTRY, _CFG)
    # 追加暴跌未来 bar(离场后)
    d2 = {k: list(v) for k, v in d.items()}
    for i in range(j + 1, len(d2["close"])):
        _set(d2, i, o=1, h=1, l=1, c=1)
    app = pb.simulate_position(_df(d2), ENTRY, _CFG)
    assert full["离场日"] == cut["离场日"] == app["离场日"]
    assert full["离场价"] == cut["离场价"] == app["离场价"] == 130.0


# ———————————————————— 一字板口径 ————————————————————
def test_oneword_down_untradeable_direction_and_board():
    """跌停一字(卖不出)=True;涨停一字/科创同幅未及停=False。"""
    assert pb._oneword_down(90, 90, 90, 100, "600000", _CFG) is True    # 主板 -10% 一字
    assert pb._oneword_down(110, 110, 110, 100, "600000", _CFG) is False  # 涨停一字(可卖)
    assert pb._oneword_down(90, 90, 90, 100, "688001", _CFG) is False   # 科创限20%,-10%非停
    assert pb._oneword_down(80, 80, 80, 100, "688001", _CFG) is True    # 科创 -20% 一字
    assert pb._oneword_down(90, 91, 90, 100, "600000", _CFG) is False   # 有振幅→非一字


def test_oneword_down_defers_exit_to_next_tradeable_day():
    """离场条件在跌停一字日命中但卖不出 → 顺延到下一可成交日,标注一字板顺延+顺延天数。"""
    d = _flat()
    _set(d, ENTRY + 1, o=90, h=90, l=90, c=90)          # 跌停一字(prev=100,-10%),想卖卖不出
    _set(d, ENTRY + 2, o=95, h=99, l=95, c=96)          # 次日可成交,low95≤97 触硬止损
    tr = pb.simulate_position(_df(d), ENTRY, _CFG, code="600000")
    assert tr["一字板顺延"] is True and tr["顺延天数"] == 1
    assert tr["离场规则号"] == 1 and tr["离场价"] == 97.0 and tr["持有天数"] == 2


# ———————————————————— 信号扫描 + 汇总 ————————————————————
def _uptrend_signal_df():
    """借 screener 的上行趋势 + 末根深跌收阳,构造恰好 1 个信号(在末根)。"""
    from tests.test_screen_s01 import _uptrend, _set_trigger_last, _df as mkdf
    d = _uptrend()
    _set_trigger_last(d)
    return mkdf(d)


def test_find_signals_locates_signal():
    kdf = _uptrend_signal_df()
    sigs = pb.find_signals(kdf)
    assert sigs == [len(kdf) - 1]                        # 仅末根命中 SELECT


def test_backtest_one_produces_closed_trade_with_alpha(monkeypatch):
    """监督 signal_at 在指定索引给信号,验证 backtest_one 撮合出已离场交易 + Alpha。"""
    d = _flat()
    _set(d, ENTRY + 3, o=125, h=131, l=124, c=130)      # rule3 → +30%
    kdf = _df(d)
    # 直接指定信号索引(绕开 ≥251 根历史门槛,专测 backtest_one 撮合+Alpha 接线)
    monkeypatch.setattr(pb, "find_signals", lambda k, cfg=None: [ENTRY])
    bench = pd.DataFrame({"date": kdf["date"], "close": [1000.0] * len(kdf)})  # 平盘基准
    trades = pb.backtest_one(kdf, code="600000", bench=bench)
    assert len(trades) == 1
    tr = trades[0]
    assert tr["状态"] == "已离场" and tr["离场规则号"] == 3
    assert tr["收益"] == pytest.approx(0.30)
    assert tr["基准收益"] == 0.0 and tr["Alpha"] == pytest.approx(0.30)  # 平盘基准→Alpha=收益


def test_summarize_trades_metrics():
    trades = [
        {"状态": "已离场", "收益": 0.20, "持有天数": 4, "离场日": "2020-01-02",
         "离场规则": "加速止盈", "Alpha": 0.18, "一字板顺延": False},
        {"状态": "已离场", "收益": -0.03, "持有天数": 1, "离场日": "2020-01-03",
         "离场规则": "硬止损", "Alpha": -0.04, "一字板顺延": True},
        {"状态": "已离场", "收益": 0.06, "持有天数": 11, "离场日": "2020-01-06",
         "离场规则": "时间成本", "Alpha": 0.05, "一字板顺延": False},
        {"状态": "持有中(数据不足)", "收益": None, "持有天数": None, "离场日": None,
         "离场规则": None, "Alpha": None, "一字板顺延": False},
    ]
    s = pb.summarize_trades(trades, min_sample=2)
    assert s["交易数"] == 4 and s["已离场数"] == 3 and s["未离场数(持有中)"] == 1
    assert s["一字板顺延笔数"] == 1
    assert s["胜率"] == pytest.approx(2 / 3)             # 2 正 / 3
    assert s["中位收益"] == pytest.approx(0.06)
    # 盈亏比 = avg(win) / |avg(loss)| = mean(0.20,0.06)/0.03
    assert s["盈亏比"] == pytest.approx(round((0.13) / 0.03, 4))
    assert s["平均持有天数"] == pytest.approx((4 + 1 + 11) / 3, abs=1e-3)
    assert s["最大回撤"] is not None
    assert s["平均Alpha(同持有期vs沪深300)"] == pytest.approx((0.18 - 0.04 + 0.05) / 3, abs=1e-4)
    assert s["离场规则分布"]["加速止盈"] == 1


def test_summarize_empty_is_graceful():
    s = pb.summarize_trades([], min_sample=10)
    assert s["已离场数"] == 0 and s["胜率"] is None and "待积累" in s["状态"]
