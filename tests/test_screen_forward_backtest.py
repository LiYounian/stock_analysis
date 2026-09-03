"""趋势模板 / SEPA+VCP 前瞻回测的语义锁单测。

锁住"为什么这么回测"的口径,防未来 prompt/重写时无意破坏:
  · 无未来函数:追加未来 K 线不改变已发生日 t 的入池判定(三个策略函数都测);
  · 北交所已排:universe_codes 丢弃 8/4 头;
  · SEPA A/B 效率捷径的保真:post-hoc re-threshold 标签 == vcp.analyze_vcp 直算(默认口径);
  · A/B 网格全覆盖:趋势 3×2、SEPA 3×3×3 组合都跑到;
  · 前瞻收益数学:等比上涨序列的 T+N 收益等于闭式解;
  · 干净二阶段上升趋势会被 A1–A7 收进(N>0 可达)。
"""
import math

import pandas as pd
import pytest

from tools.analysis.sepa_vcp import sepa, vcp
from tools.analysis.trend_template import conditions
from tools.backtest import backtest_sepa_vcp as bs
from tools.backtest import backtest_trend_template as bt
from tools.backtest import screen_forward_common as C


# ———————————————————— 构造器 ————————————————————
def _wavy_df(n=340, start="2019-01-01"):
    """线性上行 + 双频正弦 → 制造波段高低点(供 VCP 切轮)。"""
    closes = []
    for i in range(n):
        base = 50.0 + 0.10 * i
        wave = 3.0 * math.sin(i / 11.0) + 1.5 * math.sin(i / 4.0)
        closes.append(round(base + wave, 4))
    dates = pd.bdate_range(start, periods=n)
    highs = [round(c + 0.6, 4) for c in closes]
    lows = [round(c - 0.6, 4) for c in closes]
    vols = [1000.0 + (i % 7) * 50 for i in range(n)]
    return pd.DataFrame({"date": dates, "open": closes, "high": highs,
                         "low": lows, "close": closes, "volume": vols,
                         "amount": [c * v for c, v in zip(closes, vols)]})


def _strong_uptrend_df(n=320, start="2019-01-01"):
    """干净二阶段上升趋势:稳步 +0.2/日,close 贴近新高。"""
    closes = [round(30.0 + 0.20 * i, 4) for i in range(n)]
    dates = pd.bdate_range(start, periods=n)
    highs = [round(c + 0.05, 4) for c in closes]
    lows = [round(c - 0.05, 4) for c in closes]
    vols = [2000.0] * n
    return pd.DataFrame({"date": dates, "open": closes, "high": highs,
                         "low": lows, "close": closes, "volume": vols,
                         "amount": [c * v for c, v in zip(closes, vols)]})


def _append_future(df, k=25):
    last = df["date"].iloc[-1]
    fdates = pd.bdate_range(last + pd.Timedelta(days=1), periods=k)
    fut = pd.DataFrame({
        "date": fdates, "open": [999.0] * k, "high": [1099.0] * k,
        "low": [1.0] * k, "close": [999.0] * k, "volume": [1.0] * k,
        "amount": [999.0] * k,
    })
    return pd.concat([df, fut], ignore_index=True)


# ———————————————————— 无未来函数 ————————————————————
def test_trend_evaluate_lookahead_invariant():
    df = _strong_uptrend_df()
    t = 300
    r1 = conditions.evaluate(df, t=t, rps250=85.0)
    r2 = conditions.evaluate(_append_future(df), t=t, rps250=85.0)
    assert r1 == r2


def test_sepa_pass_lookahead_invariant():
    df = _strong_uptrend_df()
    t = 300
    assert sepa.sepa_pass(df, t=t) == sepa.sepa_pass(_append_future(df), t=t)


def test_vcp_capture_lookahead_invariant():
    df = _wavy_df()
    t = 300
    assert bs._vcp_capture(df, t, 3) == bs._vcp_capture(_append_future(df), t, 3)


# ———————————————————— 北交所已排 ————————————————————
def test_universe_excludes_bj(monkeypatch):
    monkeypatch.setattr(C.store, "list_master_codes",
                        lambda: ["600000", "000001", "300750", "830799", "430139", "688981"])
    codes = C.universe_codes(exclude_bj=True)
    assert "830799" not in codes and "430139" not in codes
    assert set(codes) == {"600000", "000001", "300750", "688981"}


# ———————————————————— 向量化 pivot 与原实现逐点等价 ————————————————————
def test_fast_confirmed_pivots_equivalence():
    import numpy as np
    df = _wavy_df()
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    for n in (2, 3, 4):
        for t in (60, 150, 250, len(df) - 5):
            orig = vcp._confirmed_pivots(high, low, t, n)          # 原始 Python 版
            fast = C._fast_confirmed_pivots(high, low, t, n)       # 向量化版
            assert orig == fast, (n, t, orig[:5], fast[:5])


def test_install_fast_vcp_keeps_output():
    """装上向量化 pivot/_dates 后,analyze_vcp 逐 t 标签 + 轮次日期字段均不变。"""
    df = _wavy_df()
    cfg = dict(bs._CFG)
    before = [vcp.analyze_vcp(df, t=t, cfg=cfg) for t in range(240, 300)]
    C.install_fast_vcp()
    after = [vcp.analyze_vcp(df, t=t, cfg=cfg) for t in range(240, 300)]
    for b, a in zip(before, after):
        assert b["接近枢纽"] == a["接近枢纽"]
        assert b["VCP进行中"] == a["VCP进行中"]
        assert b["结构破坏"] == a["结构破坏"]
        # 轮次日期字段(_dates 提速后)必须逐字相等
        assert [r["start_date"] for r in b["轮次"]] == [r["start_date"] for r in a["轮次"]]
        assert [r["end_date"] for r in b["轮次"]] == [r["end_date"] for r in a["轮次"]]


# ———————————————————— SEPA post-hoc 标签保真 ————————————————————
def test_sepa_labels_match_analyze_vcp_primary():
    """默认口径(pivot3/距高8/失效1.5)下,post-hoc 标签必须逐 t 等于直算。"""
    df = _wavy_df()
    p, nx, ff = bs.PRIMARY["pivot"], bs.PRIMARY["near"], bs.PRIMARY["fail"]
    cfg = dict(bs._CFG); cfg["pivot窗口"] = p; cfg["距前高近%"] = nx; cfg["失效跌破%"] = ff
    checked = 0
    for t in range(230, len(df)):
        direct = vcp.analyze_vcp(df, t=t, cfg=cfg)
        posthoc = bs._labels(bs._vcp_capture(df, t, p), nx, ff)
        for k in ("VCP进行中", "接近枢纽", "结构破坏"):
            assert bool(direct[k]) == bool(posthoc[k]), (t, k, direct[k], posthoc[k])
        checked += 1
    assert checked > 50


# ———————————————————— A/B 网格全覆盖 ————————————————————
def test_ab_grids_sizes():
    assert len(bt.MIN_RPS_GRID) * len(bt.HI_MULT_GRID) == 6
    assert len(bs.PIVOT_GRID) * len(bs.NEAR_GRID) * len(bs.FAIL_GRID) == 27


# ———————————————————— 前瞻收益数学 ————————————————————
def test_forward_cache_math():
    """等比 +1%/日:T+n 收益 = 1.01**n − 1(闭式解),alpha=个股−基准。"""
    n = 60
    closes = [round(100.0 * (1.01 ** i), 6) for i in range(n)]
    dates = pd.bdate_range("2020-01-01", periods=n)
    df = pd.DataFrame({"date": dates, "open": closes, "high": closes,
                       "low": closes, "close": closes, "volume": [1.0] * n})
    hs = df.copy()  # 基准同序列 → alpha ≈ 0
    klines = {"X": df}
    eligible = {"X": [dates[10]]}
    cache = C.build_forward_cache(klines, eligible, hs, windows=(1, 5, 10, 20))
    rec = cache[("X", str(dates[10].date()))]
    for w in (1, 5, 10, 20):
        assert rec["前瞻"][w] == pytest.approx(1.01 ** w - 1, abs=1e-4)
        assert rec["alpha"][w] == pytest.approx(0.0, abs=1e-6)


# ———————————————————— 干净上升趋势被收进(N>0 可达)————————————————————
def test_clean_uptrend_passes_base():
    df = _strong_uptrend_df()
    r = conditions.evaluate(df, t=310, rps250=95.0)
    assert r["异常"] is None
    assert all(r["conditions"][f"a{i}"] for i in range(1, 8))   # A1–A7 全真
    assert r["pass_mode"] in {"完整", "增强"}                    # A8 也真(rps95)
    assert sepa.sepa_pass(df, t=310)["入池"] is True


# ———————————————————— regime 打标 ————————————————————
def _hs300_parquet(tmp_path, closes, start="2020-01-01") -> str:
    """把一条收盘序列落成最小 HS300 parquet(只需 date/close 两列供 regime 打标)。"""
    dates = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({"date": dates, "close": [float(c) for c in closes]})
    path = tmp_path / "hs300.parquet"
    df.to_parquet(path)
    return str(path)


def test_regime_tag_values(monkeypatch, tmp_path):
    """regime 打标四分支:牛/熊/震荡/未知,判据 = HS300 收盘 vs MA200 + 60 日收益 ±5%。

    为什么这么改(锁"为什么改"):
      旧写法 `C.load_hs300()` 直接读 `data/backtest_local/hs300.parquet` —— 那个目录在
      .gitignore 里(回测本地产物,可复现重生成、不入库),所以任何没跑过回测的检出上这条
      直接 FileNotFoundError;更要命的是它唯一的断言 `tag in {牛,熊,震荡,未知}` 是**恒真**的
      (regime_tag 的返回值域就是这四个),即便真实机器上有数据、它也没保护任何东西。
      → 现在改成:用最小合成 parquet 喂 load_hs300(monkeypatch HS300_LOCAL,顺带把
        "读 parquet → 排序 → date 转 Timestamp"的 IO 契约也一起锁上),再逐条锁**判据本身**
        —— 牛/熊各自的两个条件必须同时满足才成立、只满足一半必须落"震荡"、MA200/ret60 还没
        算出来(前 200 根内)必须是"未知"(不能拿 NaN 冒充某个 regime)。
      不加 skipif:这个函数是纯判据 + 一次 parquet 读,没有任何理由依赖本地回测产物。
    """
    # 牛:200 日后仍在 MA200 上方,且近 60 日 +30% ≫ +5%
    closes = [100.0 * (1.004 ** i) for i in range(300)]
    monkeypatch.setattr(C, "HS300_LOCAL", _hs300_parquet(tmp_path, closes))
    feat = C._hs300_regime_series(C.load_hs300())
    assert C.regime_tag(feat, feat["date"].iloc[-1]) == "牛"
    # 未知:MA200/ret60 窗口未满(第 100 根)→ 不得冒充任何 regime
    assert C.regime_tag(feat, feat["date"].iloc[100]) == "未知"
    # 早于全部样本 → 无可用行 → 未知
    assert C.regime_tag(feat, feat["date"].iloc[0] - pd.Timedelta(days=1)) == "未知"

    # 熊:同样长度但持续下行 → 收盘在 MA200 下方且近 60 日 −20% ≪ −5%
    closes = [100.0 * (0.996 ** i) for i in range(300)]
    monkeypatch.setattr(C, "HS300_LOCAL", _hs300_parquet(tmp_path, closes))
    feat = C._hs300_regime_series(C.load_hs300())
    assert C.regime_tag(feat, feat["date"].iloc[-1]) == "熊"

    # 震荡:先涨到 MA200 上方(close>MA200 成立),但近 60 日横盘(|ret60| < 5%)
    # → 只满足"位置"不满足"动量",必须落震荡而不是牛(两条件是「与」不是「或」)
    closes = [100.0 * (1.004 ** i) for i in range(240)] + [100.0 * (1.004 ** 239)] * 60
    monkeypatch.setattr(C, "HS300_LOCAL", _hs300_parquet(tmp_path, closes))
    feat = C._hs300_regime_series(C.load_hs300())
    last = feat.iloc[-1]
    assert last["close"] > last["ma200"] and abs(last["ret60"]) < 0.05   # 前提:确实只满足一半
    assert C.regime_tag(feat, feat["date"].iloc[-1]) == "震荡"
