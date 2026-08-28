"""趋势模板筛选 · 单测。

锁住需求 §12 验收标准的可测部分:指标与标准 SMA 一致、无未来函数、52 周高低窗、
Return250、ValidBars 数据完整性、配置零硬编码(§8 全参数在位)。
后续步骤(conditions/rps/编排)的用例追加到本文件。
"""
from __future__ import annotations

import math

import pandas as pd

from tools.analysis.trend_template import conditions as cond
from tools.analysis.trend_template import indicators as ind
from tools.analysis.trend_template import rps as rps_mod
from tools.config.strategy import THRESHOLDS


def _kdf(close, high=None, low=None):
    """按收盘序列造 kline;不传 high/low 默认与 close 同(便于构造)。"""
    high = close if high is None else high
    low = close if low is None else low
    return pd.DataFrame({"close": list(close), "high": list(high), "low": list(low)})


def _uptrend(n=251, start=100.0, step=0.2):
    """平缓单调上升序列:价>MA50>MA150>MA200、MA200上升、距52低远、距52高近。"""
    return [start + step * i for i in range(n)]


# ————————————————————— 指标层:MA —————————————————————

def test_ma_matches_standard_sma():
    xs = [float(i) for i in range(1, 21)]          # 1..20
    # 第 19 根(0-based)的 5 日均线 = mean(16,17,18,19,20)
    assert ind.ma(xs, 19, 5) == sum([16, 17, 18, 19, 20]) / 5
    # 与 pandas rolling 口径一致
    s = pd.Series(xs)
    assert math.isclose(ind.ma(xs, 19, 5), float(s.rolling(5).mean().iloc[19]))


def test_ma_insufficient_returns_none_not_zero():
    xs = [10.0, 11.0, 12.0]
    assert ind.ma(xs, 1, 5) is None          # 只有 2 根,不足 5 → None(不是 0)
    assert ind.ma(xs, 2, 3) == 11.0          # 恰好 3 根可算


def test_ma_no_lookahead_truncation_invariance():
    xs = [float(i) for i in range(1, 31)]
    t, n = 20, 10
    full = ind.ma(xs, t, n)
    trunc = ind.ma(xs[: t + 1], t, n)        # 砍掉 t 之后 → 结果不变
    assert full == trunc


# ————————————————————— 52 周高/低 —————————————————————

def test_lowest_highest_window():
    low = [5.0] * 249 + [3.0, 9.0]           # 251 根,最后两根 3、9
    high = [8.0] * 249 + [7.0, 12.0]
    t = 250
    assert ind.lowest_low(low, t, win=250) == 3.0     # 窗 [1..250] 内最低=3
    assert ind.highest_high(high, t, win=250) == 12.0


def test_52w_insufficient_returns_none():
    low = [5.0] * 100
    assert ind.lowest_low(low, 99, win=250) is None   # 不足 250 → None
    assert ind.highest_high([8.0] * 100, 99, win=250) is None


# ————————————————————— Return250 —————————————————————

def test_return_n_basic():
    xs = [10.0] * 1 + [0.0] * 249 + [13.0]   # index0=10, index250=13
    xs[0] = 10.0
    # 用干净序列避免 0 干扰:构造 index (t-250)=10, t=13
    close = [1.0] * 251
    close[0] = 10.0
    close[250] = 13.0
    assert math.isclose(ind.return_n(close, 250, 250), 13.0 / 10.0 - 1.0)


def test_return_n_guards():
    close = [1.0] * 251
    assert ind.return_n(close, 100, 250) is None      # t-250<0 不足
    close[0] = 0.0
    assert ind.return_n(close, 250, 250) is None      # 基准价 ≤0 → None


# ————————————————————— ValidBars —————————————————————

def test_valid_bars_excludes_invalid():
    df = pd.DataFrame({"close": [10.0, float("nan"), 0.0, -1.0, 12.0]})
    # 有效:index0(10)、index4(12);NaN/0/负 都不计
    assert ind.valid_bars(df, 4) == 2


def test_valid_bars_respects_is_trading():
    df = pd.DataFrame({
        "close": [10.0, 11.0, 12.0],
        "is_trading": [True, False, True],
    })
    assert ind.valid_bars(df, 2) == 2         # 停牌那根(index1)不计


# ————————————————————— 配置零硬编码(§8) —————————————————————

def test_config_block_has_all_section8_params():
    cfg = THRESHOLDS["趋势模板"]
    for k in ("ma_short", "ma_medium", "ma_long", "trend_lookback_days",
              "week52_window", "min_bars", "min_gain_from_52w_low",
              "max_distance_from_52w_high", "min_rps", "min_price",
              "min_amount", "adjustment", "universe"):
        assert k in cfg, f"缺配置参数 {k}"
    assert cfg["adjustment"] == "qfq"
    assert cfg["week52_window"] == 250 and cfg["min_bars"] == 250


# ————————————————————— 条件层:A1–A8 —————————————————————

def test_uptrend_passes_a1_to_a7():
    df = _kdf(_uptrend())
    r = cond.evaluate(df, rps250=None)
    assert r["异常"] is None
    for k in ("a1", "a2", "a3", "a4", "a5", "a6", "a7"):
        assert r["conditions"][k] is True, f"{k} 应通过"
    assert r["conditions"]["a8"] is None            # 无 RPS → A8 无法判


def test_a6_boundary_equal_passes():
    # 52 周低=100,收盘=130=100×1.30(等号)→ A6 通过
    close = [110.0] * 250 + [130.0]
    low = [100.0] * 251
    r = cond.evaluate(_kdf(close, low=low), rps250=None)
    assert r["conditions"]["a6"] is True


def test_a7_boundary_equal_passes():
    # 52 周高=200,收盘=150=200×0.75(等号)→ A7 通过
    close = [150.0] * 251
    high = [200.0] * 251
    r = cond.evaluate(_kdf(close, high=high), rps250=None)
    assert r["conditions"]["a7"] is True


def test_a8_threshold_and_modes():
    df = _kdf(_uptrend())
    # 基础模式:无 RPS 也能通过 A1–A7
    assert cond.evaluate(df, rps250=None)["pass_mode"] == "基础"
    # 完整模式:RPS≥70
    assert cond.evaluate(df, rps250=85.0)["pass_mode"] == "完整"
    # RPS 未达门槛 → 只到基础
    assert cond.evaluate(df, rps250=60.0)["pass_mode"] == "基础"
    # 增强模式:完整 + 价≥5 + 当日成交额≥1e8
    assert cond.evaluate(df, rps250=85.0, amount=2e8)["pass_mode"] == "增强"
    # 成交额缺失 → 不进增强,仍是完整(不静默当 0、不报错)
    assert cond.evaluate(df, rps250=85.0, amount=None)["pass_mode"] == "完整"
    # 成交额不足 → 不进增强
    assert cond.evaluate(df, rps250=85.0, amount=5e7)["pass_mode"] == "完整"


def test_insufficient_and_invalid_data():
    short = _kdf(_uptrend(n=100))
    assert cond.evaluate(short)["异常"] == "INSUFFICIENT_DATA"
    bad = _uptrend()
    bad[-1] = -1.0                                  # 收盘为负
    assert cond.evaluate(_kdf(bad))["异常"] == "INVALID_DATA"


def test_conditions_no_lookahead_truncation_invariance():
    close = _uptrend(n=260)
    df_full = _kdf(close)
    df_trunc = _kdf(close[:251])
    a = cond.evaluate(df_full, t=250, rps250=75.0)["conditions"]
    b = cond.evaluate(df_trunc, t=250, rps250=75.0)["conditions"]
    assert a == b


# ————————————————————— RPS250 横截面 —————————————————————

def test_rps_ordering_and_percentile():
    r = rps_mod.rps_from_returns({"A": 0.5, "B": 0.3, "C": 0.1, "D": -0.1})
    assert r["A"] == 100.0                      # 最高收益 → 满分位
    assert r["D"] == 25.0                        # 最低 → 1/4
    assert r["A"] > r["B"] > r["C"] > r["D"]


def test_rps_repeatable():
    data = {"A": 0.2, "B": -0.05, "C": 0.4, "D": 0.4}
    assert rps_mod.rps_from_returns(data) == rps_mod.rps_from_returns(dict(data))


def test_rps_ties_use_mean_rank():
    r = rps_mod.rps_from_returns({"A": 0.1, "B": 0.1, "C": 0.5})
    assert r["A"] == r["B"] == 50.0             # 并列取均秩
    assert r["C"] == 100.0


def test_rps_drops_none_and_nan():
    r = rps_mod.rps_from_returns({"A": 0.3, "B": None, "C": float("nan"), "D": 0.1})
    assert set(r.keys()) == {"A", "D"}           # 无法计算的票不出现(§10 跳过)
    assert rps_mod.rps_from_returns({}) == {}


def test_rps_threshold_configurable_70_80_90():
    df = _kdf(_uptrend())
    for thr, expect in ((70, True), (80, True), (90, False)):
        cfg = dict(THRESHOLDS["趋势模板"], min_rps=thr)
        r = cond.evaluate(df, rps250=85.0, cfg=cfg)
        assert r["conditions"]["a8"] is expect, f"门槛 {thr} 时 A8 应={expect}"


# ————————————————————— 编排层:screen_trend_template —————————————————————

from tools.pipeline import screen_trend_template as pipe  # noqa: E402


def _dated(close, amount=None):
    n = len(close)
    dates = pd.date_range("2025-01-01", periods=n, freq="D").strftime("%Y-%m-%d")
    df = pd.DataFrame({"date": dates, "close": close, "high": close, "low": close})
    if amount is not None:
        df["amount"] = [amount] * n
    return df


def test_passes_mode_gate():
    assert pipe._passes("增强", "基础") and pipe._passes("完整", "完整")
    assert not pipe._passes("完整", "增强")
    assert not pipe._passes(None, "基础")


def test_amount_of_last_row():
    assert pipe._amount_of(_dated(_uptrend(), amount=3e8)) == 3e8
    assert pipe._amount_of(_dated(_uptrend())) is None          # 无 amount 列 → None


def test_pipeline_end_to_end(monkeypatch):
    # 两只上升趋势(强弱不同)+ 一只横盘(不入选)
    strong = _dated([100.0 + 0.3 * i for i in range(251)], amount=2e8)
    weak = _dated([100.0 + 0.2 * i for i in range(251)], amount=2e8)
    flat = _dated([100.0] * 251, amount=2e8)
    kmap = {"000001": strong, "000002": weak, "000003": flat}
    monkeypatch.setattr(pipe, "_load_or_fetch_kline", lambda code, fetch: kmap[code])
    monkeypatch.setattr(pipe.store, "put_view", lambda *a, **k: None)

    # 基础模式(A1–A7,不卡 RPS 门槛):两只上升趋势入选,横盘剔除
    v = pipe.run_trend_template(list(kmap), as_of="2025-09-09", mode="基础",
                                fetch=False, export=())
    picks = [r["symbol"] for r in v["rows"]]
    assert "000003" not in picks                     # 横盘不符合趋势模板(A1 即失败)
    assert set(picks) == {"000001", "000002"}
    # RPS 排序:强趋势(涨幅高)RPS 更高、排前
    assert v["rows"][0]["symbol"] == "000001"
    assert v["rows"][0]["rps250"] >= v["rows"][1]["rps250"]
    # 结构化字段齐全(§7)
    row = v["rows"][0]
    for k in ("condition_a1", "condition_a8", "pass_mode", "rps250", "return250",
              "trade_date", "generated_at", "amount"):
        assert k in row
    assert v["免责"].find("VCP") >= 0                # 显式声明未含 VCP 等


def test_pipeline_lag_flag(monkeypatch):
    kmap = {"000001": _dated(_uptrend(), amount=2e8)}   # 数据日 = 2025-09-08(251根)
    monkeypatch.setattr(pipe, "_load_or_fetch_kline", lambda code, fetch: kmap[code])
    monkeypatch.setattr(pipe.store, "put_view", lambda *a, **k: None)
    v = pipe.run_trend_template(["000001"], as_of="2026-01-01", mode="完整",
                                fetch=False, export=())
    assert v["滞后"] is True and v["数据日期"] < "2026-01-01"


def test_pipeline_base_mode_no_rps(monkeypatch):
    # 基础模式:不喂 RPS 环境也应能出候选(A1–A7)。单票时 RPS 必=100,故另测 conditions 已覆盖;
    # 这里验证基础模式下 A8 缺失不挡入选。
    kmap = {"000001": _dated(_uptrend(), amount=2e8)}
    monkeypatch.setattr(pipe, "_load_or_fetch_kline", lambda code, fetch: kmap[code])
    monkeypatch.setattr(pipe.store, "put_view", lambda *a, **k: None)
    v = pipe.run_trend_template(["000001"], mode="基础", fetch=False, export=())
    assert v["rows"] and v["rows"][0]["pass_mode"] in ("基础", "完整", "增强")
