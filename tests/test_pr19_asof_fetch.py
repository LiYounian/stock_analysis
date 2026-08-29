"""PR#19 数据源 as_of point-in-time + 低频源新鲜度门控 单测(不触网)。

锁语义(为什么改,防未来重写无意删掉规则):
  1. chip 按 as_of 重算:结果**只由 ≤as_of 的 K线 bar 决定**,追加未来 bar 不改变历史 as_of 结果。
  2. consensus/holder date-pin:as_of 指定时只取 **≤as_of 的最近采集分区**,绝不返回未来分区;
     ≤as_of 无分区 → 降级(FileNotFoundError),不注入今值。
  3. 股东户数新鲜度门控:缓存新鲜(≤阈值天)则跳过逐票网络拉;无缓存/阈值0 → 照拉(首采不漏)。
路径隔离:monkeypatch store._RAW_DIR 到 tmp。
"""
import numpy as np
import pandas as pd
import pytest

from tools.collectors import chip
from tools.collectors import consensus
from tools.collectors import market
from tools.collectors import smart_money as sm
from tools.store import repo as store


def _synth(n=120, up=True, start="2026-01-01"):
    """合成一段带换手率的日 K线(默认单边上涨)。"""
    rng = np.random.RandomState(1)
    base = np.linspace(10, 15, n) if up else np.linspace(15, 10, n)
    base = base + rng.randn(n) * 0.15
    rows = []
    for i in range(n):
        c = float(base[i]); o = c * (1 + rng.randn() * 0.01)
        h = max(o, c) * 1.01; l = min(o, c) * 0.99
        vol = 1e6 * (1 + rng.rand())
        rows.append(dict(date=pd.Timestamp(start) + pd.Timedelta(days=i),
                         open=o, high=h, low=l, close=c, volume=vol,
                         amount=vol * c, turnover=2.0 + rng.rand(), pct_chg=0.0))
    return pd.DataFrame(rows)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(store, "_RAW_DIR", raw)
    yield store
    store.set_active_date(None)


# ————————————————————————————————————————————————
# 1) chip as_of point-in-time:结果只由 ≤as_of 的 bar 决定
# ————————————————————————————————————————————————
def test_chip_asof_ignores_future_bars():
    df = _synth(n=120, up=True)
    as_of = df["date"].iloc[80].strftime("%Y-%m-%d")     # 取中间某日为锁定日
    # 只喂 ≤as_of 的历史(≡ 当时能看到的全部)
    hist = df[df["date"] <= pd.Timestamp(as_of)]
    ref = chip.summarize(hist)
    # 完整序列(含未来 bar)按 as_of 截断重算
    got = chip.summarize_asof(df, as_of)
    assert got == ref                                     # 未来 bar 不参与,结果全等
    assert got["获利比例"] is not None


def test_chip_asof_future_bars_do_not_change_result():
    """给定历史 as_of,追加更多未来 bar 后同 as_of 重算结果不变。"""
    df = _synth(n=100, up=True)
    as_of = df["date"].iloc[70].strftime("%Y-%m-%d")
    r1 = chip.summarize_asof(df, as_of)
    df_more = pd.concat([df, _synth(n=40, up=False,     # 后续走跌的未来 bar
                                    start="2026-06-01")], ignore_index=True)
    r2 = chip.summarize_asof(df_more, as_of)
    assert r1 == r2


def test_load_chip_asof_recomputes_from_kline(monkeypatch):
    df = _synth(n=120, up=True)
    monkeypatch.setattr(market, "load_kline", lambda code: df)
    as_of = df["date"].iloc[90].strftime("%Y-%m-%d")
    got = chip.load_chip("000001", as_of)
    assert got == chip.summarize_asof(df, as_of)


def test_load_chip_asof_degrades_when_no_turnover(monkeypatch):
    df = _synth(n=120).drop(columns=["turnover"])
    monkeypatch.setattr(market, "load_kline", lambda code: df)
    with pytest.raises(FileNotFoundError):               # ≤as_of 无法推演 → 降级(不伪造)
        chip.load_chip("000001", "2026-03-01")


def test_load_chip_no_asof_reads_snapshot(iso):
    """as_of=None 走缓存快照读法(当日/存在性检查路径),不重算。"""
    iso.set_active_date("2026-08-20")
    iso.put_raw("chip", "600000", {"获利比例": 0.7, "平均成本": 12.0})
    assert chip.load_chip("600000")["获利比例"] == 0.7


# ————————————————————————————————————————————————
# 2) consensus / holder date-pin:≤as_of 最近分区,不取未来
# ————————————————————————————————————————————————
def test_consensus_asof_datepin_no_future(iso):
    iso.put_raw("consensus", "600000", {"预期EPS当年": 1.0}, date="2026-08-10")
    iso.put_raw("consensus", "600000", {"预期EPS当年": 2.0}, date="2026-08-20")  # 未来分区
    # as_of 落在两分区之间 → 取 ≤as_of 的最近分区(0810),不取未来(0820)
    assert consensus.load_consensus("600000", "2026-08-15")["预期EPS当年"] == 1.0
    # as_of=None → 全局最新(0820)
    assert consensus.load_consensus("600000")["预期EPS当年"] == 2.0
    # as_of 早于任何分区 → 降级,不注入今值
    with pytest.raises(FileNotFoundError):
        consensus.load_consensus("600000", "2026-08-01")


def test_holder_asof_datepin_no_future(iso):
    iso.put_raw("holder_num", "600000", [{"date": "2026-06-30", "holders": 100}],
                date="2026-08-10")
    iso.put_raw("holder_num", "600000", [{"date": "2026-06-30", "holders": 90}],
                date="2026-08-20")
    got = sm.load_holder_num("600000", "2026-08-15")
    assert got[0]["holders"] == 100                       # ≤as_of 最近分区(0810)
    assert sm.load_holder_num("600000")[0]["holders"] == 90  # latest


# ————————————————————————————————————————————————
# 3) 股东户数新鲜度门控:新鲜跳过、无缓存/阈值0 照拉
# ————————————————————————————————————————————————
def test_holder_freshness_gate(iso, monkeypatch):
    from tools.config import settings, stock_pool
    monkeypatch.setattr(settings, "FETCH_SLEEP_SEC", 0)
    monkeypatch.setattr(stock_pool, "is_hk", lambda c: False)
    monkeypatch.setattr(sm, "_safe_market", lambda *a, **k: None)   # lhb/block 不触网
    calls = {"n": 0}

    def _fake_holder(code):
        calls["n"] += 1
        return [{"date": "2026-06-30", "holders": 100, "change_ratio": -1.0}]

    monkeypatch.setattr(sm, "_fetch_holder_num", _fake_holder)
    iso.set_active_date("2026-08-20")

    # 首采:无缓存 → is_stale=True → 拉
    sm.fetch_smart_money(["600000"], holder_max_stale_days=28)
    assert calls["n"] == 1

    # 再采(缓存刚写、新鲜)→ 门控跳过,不再拉
    sm.fetch_smart_money(["600000"], holder_max_stale_days=28)
    assert calls["n"] == 1
    # 跳过后读取侧仍可 date-pin 回退到最近分区(不丢数据)
    assert sm.load_holder_num("600000", "2026-08-20")[0]["holders"] == 100

    # 阈值 0(强制视为陈旧)→ 照拉
    sm.fetch_smart_money(["600000"], holder_max_stale_days=0)
    assert calls["n"] == 2

    # 无门控(None)→ 恒拉
    sm.fetch_smart_money(["600000"], holder_max_stale_days=None)
    assert calls["n"] == 3
