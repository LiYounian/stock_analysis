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


# ————————————————————————— 建池/增量脚手架(临时目录 + 内存合成 kline)—————————————————————————
def _patch_market(monkeypatch, klines):
    def _load(code):
        df = klines.get(str(code))
        if df is None:
            raise FileNotFoundError(code)
        return df.copy(deep=True)
    import tools.collectors.market as market
    monkeypatch.setattr(market, "load_kline", _load)


_RCOLS = [f"r{N}" for N in cp._HORIZONS]
_ODCOLS = [f"od{N}" for N in cp._HORIZONS]


def _assert_pool_equal(a, b):
    a = a.sort_values(["code", "date"]).reset_index(drop=True)
    b = b.sort_values(["code", "date"]).reset_index(drop=True)
    assert len(a) == len(b), f"行数 {len(a)} != {len(b)}"
    for c in ["code", "date", "trend", "mom", "boll"]:
        assert (a[c].astype(str).to_numpy() == b[c].astype(str).to_numpy()).all(), f"列 {c} 不一致"
    for c in _RCOLS:
        va, vb = a[c].to_numpy(float), b[c].to_numpy(float)
        assert np.array_equal(np.isnan(va), np.isnan(vb)), f"{c} NaN 模式不一致"
        m = ~np.isnan(va)
        assert np.allclose(va[m], vb[m], rtol=1e-9, atol=1e-9), f"{c} 值不一致"
    for c in _ODCOLS:
        va = pd.to_datetime(a[c]).to_numpy("datetime64[ns]")
        vb = pd.to_datetime(b[c]).to_numpy("datetime64[ns]")
        na, nb = pd.isna(va), pd.isna(vb)
        assert np.array_equal(na, nb), f"{c} NaT 模式不一致"
        assert (va[~na] == vb[~nb]).all(), f"{c} 结局日不一致"


def _full(codes, klines, monkeypatch, path):
    _patch_market(monkeypatch, klines)
    return cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)


# ————————————————————————— ② 递推标签 == 全算(核心逐值锁)—————————————————————————
@pytest.mark.parametrize("seed,H,k", [(5, 1180, 20), (6, 300, 12), (7, 800, 1), (8, 950, 50)])
def test_recur_labels_equals_full(seed, H, k):
    """sidecar 末态递推的新 bar 标签,与全算 _pool_labels 对每个新 bar 逐值(trend/mom/boll)相同。"""
    n = H + k
    closes = _rand(n, seed)
    df = _kline(closes)
    hist = df.iloc[:H]
    state = cp._build_sidecar_state(hist["close"], hist["date"].iloc[-1])
    new_close = df["close"].to_numpy(float)[H:]
    trend, mom, boll, _ = cp._recur_new_bars(state, new_close, cp._HORIZONS)
    gt, gm, gb = cp._pool_labels(df)
    assert (trend == gt.to_numpy()[H:]).all(), "trend 递推≠全算"
    assert (mom == gm.to_numpy()[H:]).all(), "mom 递推≠全算"
    assert (boll == gb.to_numpy()[H:]).all(), "boll 递推≠全算"


# ————————————————————————— ③ 递推连续指标 bit-exact(diff==0.0)—————————————————————————
def test_recur_bitexact_indicators():
    """递推推进后的 MACD(ema_fast/ema_slow/dea/柱)与 RSI(up/down)末态,与全算 ta.* 末值精确相等(diff==0.0)。"""
    n, H = 1300, 1250
    df = _kline(_rand(n, 13))
    close = df["close"]
    state = cp._build_sidecar_state(close.iloc[:H], df["date"].iloc[H - 1])
    _, _, _, ns = cp._recur_new_bars(state, close.to_numpy(float)[H:], cp._HORIZONS)
    # 全算末值(全序列尾根)
    ema_f = close.ewm(span=cp._MACD_FAST, adjust=False).mean().iloc[-1]
    ema_s = close.ewm(span=cp._MACD_SLOW, adjust=False).mean().iloc[-1]
    dif = close.ewm(span=cp._MACD_FAST, adjust=False).mean() - close.ewm(span=cp._MACD_SLOW, adjust=False).mean()
    dea = dif.ewm(span=cp._MACD_SIGNAL, adjust=False).mean().iloc[-1]
    bar = ta.macd(close)["macd"].iloc[-1]
    diff = close.diff()
    up = cp.ta_technical_sma_cn(diff.clip(lower=0), cp._RSI_WIN, 1).iloc[-1]
    down = cp.ta_technical_sma_cn((-diff).clip(lower=0), cp._RSI_WIN, 1).iloc[-1]
    assert abs(ns["ema_fast"] - ema_f) == 0.0, ns["ema_fast"] - ema_f
    assert abs(ns["ema_slow"] - ema_s) == 0.0
    assert abs(ns["dea"] - dea) == 0.0
    assert abs(ns["macd_bar_last"] - bar) == 0.0
    assert abs(ns["rsi_up"] - up) == 0.0
    assert abs(ns["rsi_down"] - down) == 0.0


# ————————————————————————— ④ 新 bar 走 O(1),零调 _pool_labels —————————————————————————
def test_new_bar_O1_no_pool_labels(tmp_path, monkeypatch):
    codes = ["A", "B"]
    old_kl = {"A": _kline(_rand(600, 71)), "B": _kline(_rand(700, 72))}
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)   # 写 sidecar

    new_kl = {"A": _kline(_rand(605, 71)), "B": _kline(_rand(708, 72))}
    assert np.allclose(new_kl["A"]["close"].iloc[:600], old_kl["A"]["close"])
    # 先算全量参照(未 patch)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    # patch _pool_labels 抛异常:递推路径若真 O(1) 则不触发
    monkeypatch.setattr(cp, "_pool_labels",
                        lambda df: (_ for _ in ()).throw(AssertionError("新 bar 不应调 _pool_labels")))
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    _assert_pool_equal(inc, full)


# ————————————————————————— ⑤ 除权改写被全史值校验捕获 → fallback + 重建 sidecar —————————————————————————
def test_qfq_rewrite_detected_fallback(tmp_path, monkeypatch):
    codes = ["A", "B"]
    old_kl = {"A": _kline(_rand(400, 81)), "B": _kline(_rand(400, 82))}
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    new_kl = {}
    a = _kline(list(old_kl["A"]["close"]) + _rand(5, 999)[:5])
    cl = a["close"].to_numpy(float).copy()
    cl[:200] *= 0.8            # 非等比改写前半段 → 跨 200 边界比率变 → 值校验命中
    a["close"] = cl
    new_kl["A"] = a
    new_kl["B"] = _kline(list(old_kl["B"]["close"]) + _rand(5, 888)[:5])   # 纯 append

    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)
    # A 走了全算 → sidecar 应基于改写后的新价重建(prev_close == 新末收盘)
    warmup = THRESHOLDS["指标条件化"]["池预热根数"]
    sc = cp._read_sidecar_index(path, warmup, cp._HORIZONS)
    assert "A" in sc and abs(sc["A"]["prev_close"] - float(new_kl["A"]["close"].iloc[-1])) < 1e-9


# ————————————————————————— ⑥ param_hash / schema_version 失效 —————————————————————————
def test_param_hash_invalidation(tmp_path, monkeypatch):
    codes = ["A"]
    _patch_market(monkeypatch, {"A": _kline(_rand(400, 91))})
    path = str(tmp_path / "sp.parquet")
    warmup = THRESHOLDS["指标条件化"]["池预热根数"]
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)
    assert cp._read_sidecar_index(path, warmup, cp._HORIZONS), "同口径应读到 sidecar"
    # 改一个阈值 → param_hash 变 → 整表作废
    monkeypatch.setitem(THRESHOLDS["指标状态"], "动量RSI强", 60)
    assert cp._read_sidecar_index(path, warmup, cp._HORIZONS) == {}, "口径变应作废 sidecar"


def test_schema_version_bump(tmp_path, monkeypatch):
    codes = ["A"]
    _patch_market(monkeypatch, {"A": _kline(_rand(400, 92))})
    path = str(tmp_path / "sp.parquet")
    warmup = THRESHOLDS["指标条件化"]["池预热根数"]
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)
    monkeypatch.setattr(cp, "_SIDECAR_SCHEMA_VERSION", cp._SIDECAR_SCHEMA_VERSION + 1)
    assert cp._read_sidecar_index(path, warmup, cp._HORIZONS) == {}, "schema 升级应作废旧 sidecar"


# ————————————————————————— ⑦ 首建(无 sidecar)—————————————————————————
def test_sidecar_missing_first_build(tmp_path, monkeypatch):
    codes = ["A", "B"]
    kl = {"A": _kline(_rand(400, 101)), "B": _kline(_rand(500, 102))}
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, kl)
    pool = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)  # 无旧池无 sidecar
    full = _full(codes, kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(pool, full)
    warmup = THRESHOLDS["指标条件化"]["池预热根数"]
    sc = cp._read_sidecar_index(path, warmup, cp._HORIZONS)
    assert set(sc) == {"A", "B"}, "首建应落全票 sidecar"


# ————————————————————————— ⑧ tail_close 有限窗精确 —————————————————————————
def test_tail_close_finite_window_exact():
    df = _kline(_rand(300, 111))
    close = df["close"]
    state = cp._build_sidecar_state(close, df["date"].iloc[-1])
    assert len(state["tail_close"]) == cp._TAIL_KEEP
    assert np.allclose(np.asarray(state["tail_close"]), close.to_numpy(float)[-cp._TAIL_KEEP:])
    # 用 tail_close 递推 1 根,MA/BOLL 应与全序列 rolling 末值一致(有限窗必等)
    new_c = close.iloc[-1] * 1.03
    df2 = _kline(list(close) + [new_c])
    trend, mom, boll, _ = cp._recur_new_bars(state, np.array([new_c]), cp._HORIZONS)
    gt, gm, gb = cp._pool_labels(df2)
    assert trend[0] == gt.iloc[-1] and mom[0] == gm.iloc[-1] and boll[0] == gb.iloc[-1]


# ————————————————————————— ⑨ pending 到期(sidecar 在场)—————————————————————————
def test_pending_matures_with_sidecar(tmp_path, monkeypatch):
    codes = ["A"]
    old_kl = {"A": _kline(_rand(300, 121))}
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    old_pool = cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)
    a = old_pool.sort_values("date")
    assert a["r10"].isna().sum() >= 10
    last_date = a["date"].iloc[-1]
    new_kl = {"A": _kline(_rand(312, 121))}
    assert np.allclose(new_kl["A"]["close"].iloc[:300], old_kl["A"]["close"])
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)
    row = inc[inc["date"] == last_date]
    assert not row["r1"].isna().all(), "旧末端行 r1 应随新 bar 兑现"


# ————————————————————————— ⑩ 停牌/NaN close 下 RSI 递推 prev-carry 与 ta.rsi 一致 —————————————————————————
def test_stale_close_nan_carry():
    """新 bar 序列含 NaN close 时,RSI 递归 SMA 走 prev-carry(_sma_cn 口径),末态与 ta 全算一致。"""
    H = 200
    closes = _rand(H, 131)
    state = cp._build_sidecar_state(pd.Series(closes), "2015-01-01")
    new = [closes[-1] * 1.01, np.nan, closes[-1] * 1.02, closes[-1] * 0.99]
    _, _, _, ns = cp._recur_new_bars(state, np.array(new, float), cp._HORIZONS)
    full_close = pd.Series(closes + new)
    diff = full_close.diff()
    up = cp.ta_technical_sma_cn(diff.clip(lower=0), cp._RSI_WIN, 1).iloc[-1]
    down = cp.ta_technical_sma_cn((-diff).clip(lower=0), cp._RSI_WIN, 1).iloc[-1]
    assert abs(ns["rsi_up"] - up) < 1e-12, (ns["rsi_up"], up)
    assert abs(ns["rsi_down"] - down) < 1e-12, (ns["rsi_down"], down)
