"""state_pool 增量重建单测(feat/state-pool-incremental)。

锁死语义:
  ① 全量重建 vs 读旧+增量 → 逐值 + NaN 一致(核心锁,无未来函数、口径一致)。
  ② 新增 bar:只加该行 + 回填末端 pending,且标签只在尾窗上算(非全史重算)。
  ③ pending 到期:旧池 NaN 的前瞻收益经新 bar 正确兑现。
  ④ 除权 backfill 改写历史前复权价 → 廉价值校验捕获 → 该 code 全量重算,结果与全量一致。
  ⑤ kline 未变重跑 → _pool_labels 零调用(historical 行不重算),输出与旧一致。
  ⑥ rebuild=True 强制全量。

均用临时目录 + 合成数据,绝不触碰生产 data/backtest_local/state_pool.parquet。
"""
import numpy as np
import pandas as pd
import pytest

from tools.analysis import conditional_predict as cp

_H = cp._HORIZONS
_RCOLS = [f"r{N}" for N in _H]
_ODCOLS = [f"od{N}" for N in _H]


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


def _klines(seeds_lens):
    """{code: (seed, n)} → {code: DataFrame}"""
    return {code: _kline(_rand(n, seed)) for code, (seed, n) in seeds_lens.items()}


def _patch_market(monkeypatch, klines):
    """把 market.load_kline 指到内存合成 kline(深拷贝,防被就地改)。"""

    def _load(code):
        df = klines.get(str(code))
        if df is None:
            raise FileNotFoundError(code)
        return df.copy(deep=True)

    import tools.collectors.market as market
    monkeypatch.setattr(market, "load_kline", _load)


def _sorted(pool):
    return (pool.sort_values(["code", "date"]).reset_index(drop=True)
            if not pool.empty else pool)


def _assert_pool_equal(a, b):
    a, b = _sorted(a), _sorted(b)
    assert list(a.columns) == list(a.columns)
    assert len(a) == len(b), f"行数 {len(a)} != {len(b)}"
    cols = ["code", "date", "trend", "mom", "boll"]
    for c in cols:
        assert (a[c].reset_index(drop=True).astype(str)
                == b[c].reset_index(drop=True).astype(str)).all(), f"列 {c} 不一致"
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


# ---------- ① 核心锁:全量 == 读旧+增量 ----------
def test_state_pool_incremental_equals_full(tmp_path, monkeypatch):
    codes = ["A", "B", "C"]
    # 旧数据(短),先全量建旧池
    old_kl = _klines({"A": (1, 400), "B": (2, 500), "C": (3, 450)})
    path = str(tmp_path / "state_pool.parquet")
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path) \
        if False else None
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    # 新数据:每票在旧尾部各 append 若干新 bar(纯 append,历史价不动)
    new_kl = {}
    for c, df in old_kl.items():
        extra = _rand(15, seed=hash(c) % 1000)
        # 从旧收盘续走,保证历史段逐值不变
        tail = df["close"].iloc[-1]
        cont = [tail * v / 10.0 for v in extra]
        ext = _kline(list(df["close"]) + cont, start="2015-01-01")
        new_kl[c] = ext

    # 增量(读旧池)
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    # 参照:同样新数据全量重建
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "full.parquet"))
    _assert_pool_equal(inc, full)


# ---------- ② 新增 bar:标签只在尾窗算 ----------
def test_new_bar_appended(tmp_path, monkeypatch):
    codes = ["A"]
    old_kl = _klines({"A": (7, 900)})
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    new_kl = {"A": _kline(_rand(901, 7))}   # 同 seed 前 900 一致 + 1 根新 bar
    # 确认前 900 根历史价逐值一致(纯 append 前提)
    assert np.allclose(new_kl["A"]["close"].iloc[:900], old_kl["A"]["close"])

    # 记录 _pool_labels 调用输入长度(sidecar 深度重构后:新 bar 走 O(1) 递推,增量阶段应零调用)
    calls = []
    orig = cp._pool_labels
    monkeypatch.setattr(cp, "_pool_labels", lambda df: calls.append(len(df)) or orig(df))

    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    inc_calls = list(calls)   # 只看增量阶段(全量参照会再全史调用一次)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)

    # 上一步 rebuild=True 已写 sidecar → 本次增量新 bar 走 O(1) 递推、彻底不调 _pool_labels
    assert inc_calls == [], f"新 bar 应走 O(1) 递推、不调 _pool_labels:{inc_calls}"


# ---------- ③ pending 到期 ----------
def test_pending_matures(tmp_path, monkeypatch):
    codes = ["A"]
    old_kl = _klines({"A": (11, 300)})
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    old_pool = cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)
    # 旧池末端应有 pending(r10 尾部 NaN)
    a = old_pool[old_pool["code"] == "A"].sort_values("date")
    assert a["r10"].isna().sum() >= 10, "旧池末端应有 r10 pending"
    last_date = a["date"].iloc[-1]

    # append 12 根 → 之前 pending 的行(如末尾往前 10 根)全部到期
    new_kl = {"A": _kline(_rand(312, 11))}
    assert np.allclose(new_kl["A"]["close"].iloc[:300], old_kl["A"]["close"])
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)
    # 原旧池最后一行(曾经 r_N 全 NaN)现应兑现 r1(t+1 已存在)
    row = inc[(inc["code"] == "A") & (inc["date"] == last_date)]
    assert not row["r1"].isna().all(), "旧末端行 r1 应随新 bar 兑现"


# ---------- ④ 除权 backfill 改写历史价 → 值校验捕获 → 全量重算 ----------
def test_qfq_rewrite_recompute(tmp_path, monkeypatch):
    codes = ["A", "B"]
    old_kl = _klines({"A": (21, 400), "B": (22, 400)})
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    # A: 非等比改写中段前复权价(模拟一次新除权,改变跨除权日的比率)+ append 5 根
    new_kl = {}
    a = _kline(list(old_kl["A"]["close"]) + _rand(5, 999)[:5])
    cl = a["close"].to_numpy(float).copy()
    cl[:200] *= 0.8      # 只缩放前半段 → 跨 200 边界的比率变化(非等比)
    a["close"] = cl
    new_kl["A"] = a
    # B: 纯 append(历史不动)
    new_kl["B"] = _kline(list(old_kl["B"]["close"]) + _rand(5, 888)[:5])

    # 监控增量分支被调用(_incremental_code_frame),A 应返回 None → 全算
    inc_ret = {}
    orig_inc = cp._incremental_code_frame

    def _spy(df, code, old, warmup, horizons, sidecar=None):
        r = orig_inc(df, code, old, warmup, horizons, sidecar)
        inc_ret[str(code)] = (r[0] is None)   # 返回 (frame, state);frame None = 失效→全算
        return r

    monkeypatch.setattr(cp, "_incremental_code_frame", _spy)
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)
    assert inc_ret.get("A") is True, "A 被改写,值校验应判失效(返回 None→全算)"
    assert inc_ret.get("B") is False, "B 纯 append,应走增量复用"


# ---------- ⑤ 未变重跑 → _pool_labels 零调用,输出与旧一致 ----------
def test_append_only_reuses(tmp_path, monkeypatch):
    codes = ["A", "B"]
    old_kl = _klines({"A": (31, 500), "B": (32, 500)})
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    old_pool = cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    # kline 完全不变重跑:历史行 + 末端 pending 全复用,无新 bar → 不应算任何标签
    def _boom(df):
        raise AssertionError("历史行被重算了(_pool_labels 不该被调用)")

    monkeypatch.setattr(cp, "_pool_labels", _boom)
    _patch_market(monkeypatch, old_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=False, pool_path=path)
    _assert_pool_equal(inc, old_pool)


# ---------- ⑥ rebuild 强制全量 ----------
def test_rebuild_flag(tmp_path, monkeypatch):
    codes = ["A"]
    old_kl = _klines({"A": (41, 400)})
    path = str(tmp_path / "sp.parquet")
    _patch_market(monkeypatch, old_kl)
    cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)

    new_kl = {"A": _kline(_rand(410, 41))}
    # rebuild=True 时不读旧池,不走增量
    called = {"inc": False}
    monkeypatch.setattr(cp, "_incremental_code_frame",
                        lambda *a, **k: called.__setitem__("inc", True))
    _patch_market(monkeypatch, new_kl)
    inc = cp.build_state_pool(codes, save=True, rebuild=True, pool_path=path)
    full = _full(codes, new_kl, monkeypatch, str(tmp_path / "f.parquet"))
    _assert_pool_equal(inc, full)
    assert called["inc"] is False, "rebuild=True 不应走增量分支"
