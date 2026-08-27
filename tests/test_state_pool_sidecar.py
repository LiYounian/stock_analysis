"""state_pool sidecar + O(1) 递推深度重构单测(feat/state-pool-sidecar,路线甲)。

设计文档:docs/计划/state_pool_sidecar深度重构_设计.md §5。锁死语义:
  · 抽离散化零变化(_labels_from_indicators 提取后 _pool_labels 全量输出逐值+NaN 不变)。
  · 递推标签 == 全算标签(逐值锁,核心);递推 MACD/RSI 连续指标 == 全算(bit-exact,diff==0.0)。
  · 新 bar 走 O(1) 递推,零调用 _pool_labels(monkeypatch 抛异常证)。
  · 除权改写被全史值校验捕获 → fallback 全算 + 重建 sidecar。
  · param_hash / schema_version / 位置锚点失效 → 全量重建,不误用旧 sidecar。
  · 停牌/NaN close 下 RSI 递推与 ta.rsi 一致(prev-carry)。

均用临时目录 + 合成数据,绝不触碰生产 data/backtest_local/*.parquet。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis import conditional_predict as cp
from tools.analysis import technical as ta
from tools.config.strategy import THRESHOLDS


def _kline(closes, start="2015-01-01"):
    n = len(closes)
    closes = list(closes)
    return pd.DataFrame({
        "date": pd.bdate_range(start, periods=n),
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1e5] * n, "amount": [c * 1e4 for c in closes],
        "turnover": [0.05] * n,
        "pct_chg": pd.Series(closes).pct_change().mul(100).tolist(),
    })


def _rand(n, seed):
    rng = np.random.RandomState(seed)
    return list(10 * np.cumprod(1 + rng.normal(0, 0.02, n)))


# ————————————————————————— ① 抽离散化零变化(golden = 重构前原实现)—————————————————————————
def _pool_labels_golden(df):
    """重构前 _pool_labels 的原始实现(逐字保留),用于证明抽 _labels_from_indicators 未改任何逻辑。"""
    close = df["close"]
    cfg = THRESHOLDS["指标状态"]
    tb = THRESHOLDS["BOLL"]
    ma5, ma10, ma20, ma60 = (ta.ma(close, w) for w in (5, 10, 20, 60))
    valid_ma = ma5.notna() & ma10.notna() & ma20.notna() & ma60.notna()
    up = (ma5 >= ma10) & (ma10 >= ma20) & (ma20 >= ma60)
    dn = (ma5 <= ma10) & (ma10 <= ma20) & (ma20 <= ma60)
    trend = pd.Series(np.select([~valid_ma, up, dn], ["数据不足", "多头排列", "空头排列"], "纠缠"),
                      index=df.index)
    bar = ta.macd(close)["macd"]
    prev = bar.shift(1)
    macd_state = pd.Series(np.select([(prev <= 0) & (bar > 0), (prev >= 0) & (bar < 0), bar > 0],
                                     ["金叉", "死叉", "多头"], "空头"), index=df.index)
    rsi12 = ta.rsi(close, 12)
    bull = macd_state.isin(["金叉", "多头"])
    bear = macd_state.isin(["死叉", "空头"])
    mom = pd.Series(np.select([bull & (rsi12 >= cfg["动量RSI强"]), bear & (rsi12 <= cfg["动量RSI弱"])],
                              ["强", "弱"], "中"), index=df.index)
    pb = ta.boll(close)["percent_b"]
    boll = pd.Series(np.select(
        [pb.isna(), pb > 1, pb >= tb["触轨上_percentB"], pb < 0, pb <= tb["触轨下_percentB"]],
        ["数据不足", "破上轨", "触上轨", "破下轨", "触下轨"], "中性"), index=df.index)
    return trend, mom, boll


@pytest.mark.parametrize("seed,n", [(1, 400), (2, 800), (3, 1200), (4, 65)])
def test_labels_from_indicators_extract_noop(seed, n):
    """抽 _labels_from_indicators 后 _pool_labels 全量输出逐值+NaN 与重构前完全一致。"""
    df = _kline(_rand(n, seed))
    g_trend, g_mom, g_boll = _pool_labels_golden(df)
    trend, mom, boll = cp._pool_labels(df)
    assert (trend.astype(str).to_numpy() == g_trend.astype(str).to_numpy()).all(), "trend 变了"
    assert (mom.astype(str).to_numpy() == g_mom.astype(str).to_numpy()).all(), "mom 变了"
    assert (boll.astype(str).to_numpy() == g_boll.astype(str).to_numpy()).all(), "boll 变了"
