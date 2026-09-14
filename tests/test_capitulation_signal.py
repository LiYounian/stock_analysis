"""Live capitulation 信号测试。

锁四条"为什么改"的语义(防未来 prompt/代码重写时无意删规则):
  1. 信号 as-of 因果无未来:追加/改未来行,as-of[t] 不变(build_capitulation_flags 的 trailing 分位只回看);
  2. warmup 未满 500 → degraded、cap 恒 False(判据未生效不硬给信号);
  3. 有界窗口聚合 `_aggregate_breadth_window` 与全量 `compute_breadth` 逐日**数值一致**
     (below_ma20_ratio/mean_pct 单一真源,无第二套口径漂移);
  4. 放量收阳闸门口径 = 量比>1.5 且 收阳(close>open),与 MA5 闸门口径分开(H2 的 ① 触发下移)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.pipeline import capitulation_signal as cs


# ───────────────────────── 合成广度序列(不需真 K线) ─────────────────────────

def _synth_breadth(n: int, *, seed: int = 0) -> pd.DataFrame:
    """n 个交易日的温和广度序列(非极端),用于因果/warmup 测试。"""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({
        "below_ma20_ratio": np.clip(0.5 + rng.normal(0, 0.05, n), 0, 1),
        "mean_pct": rng.normal(0.1, 0.4, n),
        "net_adv": rng.normal(0.0, 0.2, n),
    }, index=idx)


def test_signal_causal_no_future():
    """追加/篡改**未来**行,as-of[T] 信号不变(防未来函数)。"""
    b = _synth_breadth(560, seed=1)
    as_of = b.index[540].strftime("%Y-%m-%d")     # 取中间某日,后面还有未来行
    sig_a = cs.compute_signal(b, as_of)
    assert sig_a is not None

    # 篡改 T 之后的所有行为极端崩跌值(若信号泄露未来,必变)
    b2 = b.copy()
    fut = b2.index > pd.Timestamp(as_of)
    b2.loc[fut, "below_ma20_ratio"] = 0.99
    b2.loc[fut, "mean_pct"] = -9.0
    # 再往后追加更多极端未来行
    ext_idx = pd.bdate_range(b2.index[-1] + pd.Timedelta(days=1), periods=30)
    ext = pd.DataFrame({"below_ma20_ratio": 0.99, "mean_pct": -9.0, "net_adv": -0.9},
                       index=ext_idx)
    b2 = pd.concat([b2, ext])
    sig_b = cs.compute_signal(b2, as_of)

    assert sig_b["as_of"] == sig_a["as_of"]
    for tier in ("primary", "deep"):
        assert sig_b[tier]["capitulation"] == sig_a[tier]["capitulation"]
        assert sig_b[tier]["below_ma20_thr"] == sig_a[tier]["below_ma20_thr"]
        assert sig_b[tier]["crash_thr"] == sig_a[tier]["crash_thr"]


def test_warmup_degraded_forces_false():
    """trailing 未满 500 → warmup True、cap 恒 False、payload degraded。"""
    b = _synth_breadth(120, seed=2)               # < 500
    as_of = b.index[-1].strftime("%Y-%m-%d")
    sig = cs.compute_signal(b, as_of)
    assert sig is not None and sig["warmup"] is True
    assert sig["primary"]["capitulation"] is False
    assert sig["deep"]["capitulation"] is False

    payload = cs.build_payload(as_of, sig, b, pd.Timestamp.now(tz="UTC").to_pydatetime(),
                               mode="kline_backfill")
    assert payload["degraded"] is True
    assert any("warmup" in r for r in payload["degrade_reasons"])
    assert payload["capitulation"] is False


def test_capitulation_fires_on_double_extreme():
    """构造末日双极端(破位广度极高 + 近端崩跌极深)→ 主档 cap=True;温和末日→False。"""
    n = 560
    idx = pd.bdate_range("2022-01-03", periods=n)
    rng = np.random.default_rng(7)
    # 有方差的温和基线(否则常量序列的分位=常量,>=/<= 会退化触发)
    base = pd.DataFrame({
        "below_ma20_ratio": np.clip(0.45 + rng.normal(0, 0.05, n), 0, 1),
        "mean_pct": rng.normal(0.05, 0.4, n),
        "net_adv": rng.normal(0, 0.1, n),
    }, index=idx)
    # 温和末日(设成中位附近):不该触发
    calm = base.copy()
    calm.iloc[-1, calm.columns.get_loc("below_ma20_ratio")] = 0.45
    calm.iloc[-2, calm.columns.get_loc("mean_pct")] = 0.05
    calm.iloc[-1, calm.columns.get_loc("mean_pct")] = 0.05
    s_calm = cs.compute_signal(calm, idx[-1].strftime("%Y-%m-%d"))
    assert s_calm["warmup"] is False
    assert s_calm["primary"]["capitulation"] is False

    # 双极端末日:below_ma20 顶格 + 连续两日深跌(cum2 极低)
    ext = base.copy()
    ext.iloc[-1, ext.columns.get_loc("below_ma20_ratio")] = 0.98
    ext.iloc[-2, ext.columns.get_loc("mean_pct")] = -6.0
    ext.iloc[-1, ext.columns.get_loc("mean_pct")] = -6.0
    s_ext = cs.compute_signal(ext, idx[-1].strftime("%Y-%m-%d"))
    assert s_ext["primary"]["capitulation"] is True
    # 连续强度余量应 > 0(两维都越阈)
    assert s_ext["primary"]["os_margin"] > 0
    assert s_ext["primary"]["crash_margin"] > 0


# ───────────────────────── 有界窗口 vs 全量(单一真源无漂移) ─────────────────────────

def _synth_kline(code: str, n: int, seed: int) -> pd.DataFrame:
    """合成单票日 K线(date/open/high/low/close/volume/pct_chg),价格随机游走。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    ret = rng.normal(0, 0.02, n)
    close = 10.0 * np.cumprod(1 + ret)
    openp = close / (1 + rng.normal(0, 0.005, n))
    high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.005, n)))
    pct = np.concatenate([[np.nan], (close[1:] / close[:-1] - 1) * 100.0])
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "open": openp, "high": high, "low": low, "close": close,
        "volume": rng.integers(1e6, 5e6, n).astype(float), "pct_chg": pct,
    })


def test_window_aggregation_matches_full_compute(monkeypatch):
    """`_aggregate_breadth_window`(有界)与 `compute_breadth`(全量)在重叠日**数值一致**。

    锁"破位广度/全A等权只有一套口径"——有界窗口只改内存足迹,不改数字。
    """
    from tools.analysis.market_forecast import breadth as B
    from tools.analysis.market_forecast import dataroot

    codes = ["600001", "000002", "300003", "688004"]
    klines = {c: _synth_kline(c, 80, seed=i) for i, c in enumerate(codes)}
    get_kline = lambda c: klines[c]

    # 全量:monkeypatch store + ensure_data_root(避免真实数据根探测)
    from tools.store import repo as store
    monkeypatch.setattr(store, "get_master_kline", get_kline)
    monkeypatch.setattr(store, "list_master_codes", lambda: list(codes))
    monkeypatch.setattr(dataroot, "ensure_data_root", lambda *a, **k: None)
    full = B.compute_breadth(codes=codes)

    # 取重叠尾段(MA20 已 valid)的一个子集日
    valid_dates = full.index[full["below_ma20_ratio"].notna()]
    subset = list(valid_dates[-20:])

    win = cs._aggregate_breadth_window(codes, subset, get_kline=get_kline)

    assert not win.empty
    for d in subset:
        for col in ("below_ma20_ratio", "mean_pct", "net_adv"):
            a = float(full.loc[d, col])
            b = float(win.loc[d, col])
            assert a == pytest.approx(b, rel=1e-9, abs=1e-9), f"{col}@{d}: full={a} win={b}"


# ───────────────────────── ① 放量收阳闸门口径(与 MA5 分开) ─────────────────────────

def test_volup_close_gate_口径():
    """放量收阳(G_volup_close)= 量比>1.5 且 close>open;MA5 闸门口径分开。

    锁 H2 的①触发下移:超跌反抽首入场闸门用"放量收阳",不用"放量站上MA5"。
    """
    from tools.backtest.capitulation.h2_gates import build_gate_panels, VOL_RATIO_GATE

    assert VOL_RATIO_GATE == 1.5

    dates = pd.bdate_range("2026-01-05", periods=4)
    codes = ["A"]
    def P(vals):
        return pd.DataFrame({"A": vals}, index=dates)
    panels = {
        # 行0:放量收阳(vr>1.5,close>open)→ volup_close True;但 close<MA5 → MA5 False
        # 行1:放量收阳且 close≥MA5 → 两者 True
        # 行2:缩量收阳(vr<1.5)→ 均 False
        # 行3:放量收阴(close<open)→ volup_close False
        "close":     P([10.0, 12.0, 10.0, 9.0]),
        "open":      P([9.5, 11.0, 9.5, 10.0]),
        "ma5":       P([10.5, 11.5, 10.5, 10.5]),
        "ma3":       P([10.2, 11.2, 10.2, 10.2]),
        "vol_ratio": P([2.0, 2.0, 1.2, 2.0]),
        "prev_high": P([11.0, 11.0, 11.0, 11.0]),
    }
    g = build_gate_panels(panels)
    volup_close = list(g["G_volup_close"]["A"])
    ma5 = list(g["G_MA5"]["A"])

    assert volup_close == [True, True, False, False]   # 放量收阳:①②真,缩量/收阴假
    assert ma5 == [False, True, False, False]           # MA5:仅②(放量且站上MA5)
    # 关键分离:行0 放量收阳已触发,MA5 闸门尚未开(反抽首波在 MA5 下方)
    assert volup_close[0] is True and ma5[0] is False
