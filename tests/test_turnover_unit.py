"""turnover 单位口径单一真源 + 自动化护栏 单测(不触网)。

锁死的语义(**为什么改**,防未来重写无意删掉):
  1. **口径契约**:master.turnover 一律百分数;各源原始口径登记在
     `tools.config.units.TURNOVER_UNIT_BY_SOURCE`,归一只在 `units.to_percent` 一处发生
     (不许在消费侧各打一遍补丁——项目已有"12 处独立实现"的教训)。
  2. **sina 是小数源**:akshare `stock_zh_a_daily` 的 turnover = volume/流通股(小数),
     必须 ×100 才能进主档;这是 100 倍混用的根因,归一后与 baostock 同日同票一致。
  3. **真实极低换手值不被误纠**:判据是"与本票 volume 不自洽",不是"值小"。
     603161/2019-11-21 的 0.4976%(已用 baostock 交叉核实为真值)必须**既不被标记、
     也不被修改**。这条是本轮最容易被未来"优化"掉的护栏。
  4. **缺列的源不许擦除已有好值**:主档同日合并逐列取最后一个非空值。
  5. **写入闸门有自动断言**:put_master_kline 检出混用 → 记 ERROR + 写 meta;
     STRICT_TURNOVER_UNIT=1 时直接抛。
  6. **下游现算兜底仍生效**:缺 turnover 时 reversal_turnover 的现算兜底不受本轮改动影响。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.collectors import market
from tools.config import units
from tools.store import repo as store


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_MASTER_DIR", tmp_path / "master")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    (tmp_path / "master").mkdir()
    (tmp_path / "raw").mkdir()
    yield store


def _series(n=120, *, float_shares=1.0e8, base_vol=2.0e6, seed=0):
    """造一票自洽的 K 线:turnover% = volume / 流通股 × 100(与真实主档同构)。"""
    rng = np.random.default_rng(seed)
    vol = base_vol * (1 + 0.4 * rng.random(n))
    dates = pd.bdate_range("2026-01-01", periods=n)
    close = 10 + np.cumsum(rng.normal(0, 0.1, n))
    return pd.DataFrame({
        "date": dates, "open": close, "high": close + 0.2, "low": close - 0.2,
        "close": close, "volume": vol, "amount": vol * close,
        "turnover": vol / float_shares * 100.0, "pct_chg": 0.0,
    })


# ————————————————— 1. 口径契约 / 单一真源 —————————————————
def test_master_turnover_unit_is_percent():
    assert units.MASTER_TURNOVER_UNIT == units.PERCENT


def test_every_known_source_declares_its_turnover_unit():
    """项目在用的每个采集源都必须登记口径:漏登记 → 归一层只能猜,就是本 bug 的成因。"""
    for src in ("baostock", "sina", "tencent", "tencent_hk", "eastmoney",
                "akshare_spot", "tushare_daily"):
        assert units.turnover_unit(src) in (units.PERCENT, units.FRACTION, units.ABSENT), \
            f"采集源 {src} 未登记 turnover 口径"


def test_unregistered_source_logs_error_and_does_not_scale(caplog):
    """未登记的新源:不静默乘 100(不放大风险),但必须**吵**——记 ERROR 提醒登记。"""
    df = pd.DataFrame({"turnover": [1.0, 2.0]})
    with caplog.at_level("ERROR", logger="config.units"):
        out = units.to_percent(df.copy(), "some_new_source")
    assert list(out["turnover"]) == [1.0, 2.0]
    assert any("未在 units" in r.message or "未在 units" in r.getMessage()
               for r in caplog.records)


def test_percent_and_absent_sources_are_untouched():
    df = pd.DataFrame({"turnover": [2.7058, 1.5643]})
    assert list(units.to_percent(df.copy(), "baostock")["turnover"]) == [2.7058, 1.5643]
    assert list(units.to_percent(df.copy(), "tencent")["turnover"]) == [2.7058, 1.5643]


# ————————————————— 2. sina 小数源归一 —————————————————
def _sina_frame():
    """akshare stock_zh_a_daily 的真实返回形状:turnover = volume/outstanding_share(小数)。
    数值取 603161 / 2026-09-02 实调值(baostock 同日同票 turnover=2.7058)。"""
    return pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"],
        "open": [15.76, 15.81], "high": [16.05, 16.36], "low": [15.60, 15.45],
        "close": [15.89, 16.24], "volume": [2983878.0, 5161198.0],
        "amount": [47372601.0, 82508354.0],
        "outstanding_share": [190748750.0, 190748750.0],
        "turnover": [0.015643, 0.027058],
    })


def test_sina_fraction_is_normalized_to_percent():
    """核心红线:sina 的小数 turnover 经 _normalize(df, "sina") 后变百分数,
    与 baostock 同日同票值一致(实调 2.7058) → 混用消失。"""
    out = market._normalize(_sina_frame(), "sina")
    assert out["turnover"].iloc[-1] == pytest.approx(2.7058, rel=1e-6)
    assert out["turnover"].iloc[0] == pytest.approx(1.5643, rel=1e-6)


def test_sina_and_baostock_same_bar_agree_after_normalize():
    """同日同票两源归一后必须同口径(差 <1%),否则又会在主档里混存。"""
    sina = market._normalize(_sina_frame(), "sina")
    bao = market._normalize(pd.DataFrame({
        "date": ["2026-09-01", "2026-09-02"], "open": [15.76, 15.81],
        "high": [16.05, 16.36], "low": [15.60, 15.45], "close": [15.89, 16.24],
        "volume": [2983878.0, 5161198.0], "amount": [47372601.3, 82508354.18],
        "turnover": [1.5643, 2.7058], "pct_chg": [0.82, 2.20],
    }), "baostock")
    assert sina["turnover"].iloc[-1] == pytest.approx(bao["turnover"].iloc[-1], rel=1e-4)


def test_normalize_without_source_keeps_turnover():
    """source 未知 → 不猜、不动数据(测试/历史调用方向后兼容)。"""
    out = market._normalize(_sina_frame())
    assert out["turnover"].iloc[-1] == pytest.approx(0.027058)


def test_tencent_source_has_no_amount_turnover_columns():
    """腾讯 fqkline 端点不给额/换手 → 归一后补 NA(这就是近端整段缺失的来源)。"""
    tx = pd.DataFrame({"date": ["2026-09-02"], "open": [15.81], "close": [16.24],
                       "high": [16.36], "low": [15.45], "volume": [5161200.0]})
    out = market._normalize(tx, "tencent")
    assert pd.isna(out["turnover"].iloc[0]) and pd.isna(out["amount"].iloc[0])


# ————————————————— 3. 检测:混用被抓、真值不被误纠 —————————————————
def test_anomaly_mask_flags_hundredfold_row():
    df = _series()
    df.loc[60, "turnover"] = df.loc[60, "turnover"] / 100.0     # 注入 sina 小数行
    mask = units.turnover_unit_anomaly_mask(df)
    assert mask.sum() == 1 and bool(mask.loc[60])


def test_anomaly_mask_ignores_genuine_low_turnover():
    """真实极低换手票(流通股很大 → 全序列 turnover ~0.3%)整体自洽,一行都不该被标记。"""
    df = _series(float_shares=6.0e8)
    assert df["turnover"].max() < 0.5                # 全部落在"看起来可疑"的量级
    assert not units.turnover_unit_anomaly_mask(df).any()


def test_real_low_turnover_bar_is_not_repaired():
    """603161/2019 实测真值行(0.4976% 等,已用 baostock 交叉核实):
    值本身很小、落在"小数口径下是 49.76%"的模糊带里,但与本票 volume 自洽 → **不许动**。"""
    df = _series(float_shares=6.94e7, base_vol=3.45e5, n=80)
    real = pd.DataFrame({
        "date": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
        "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.0,
        "volume": [345317.0, 278200.0, 342040.0],
        "amount": [4474645.0, 3488362.0, 4326241.0],
        "turnover": [0.4976, 0.4009, 0.4929], "pct_chg": 0.0,
    })
    # 用这三行真值的比值当整票基准(流通股 ≈ 6.94e7)
    df = pd.concat([df.iloc[:40], real, df.iloc[40:]], ignore_index=True)
    df = df.sort_values("date").reset_index(drop=True)
    fixed, rep = units.repair_turnover_unit(df)
    assert rep["repaired"] == 0 and rep["refused"] == 0
    got = fixed.loc[fixed["turnover"].notna(), "turnover"]
    for v in (0.4976, 0.4009, 0.4929):
        assert any(abs(x - v) < 1e-9 for x in got), f"真值 {v} 被改动"


def test_repair_restores_hundredfold_rows():
    df = _series()
    truth = [df.loc[i, "turnover"] for i in (30, 61)]
    for i in (30, 61):
        df.loc[i, "turnover"] /= 100.0
    fixed, rep = units.repair_turnover_unit(df)
    assert rep["repaired"] == 2 and rep["refused"] == 0
    for i, t in zip((30, 61), truth):
        assert fixed.loc[i, "turnover"] == pytest.approx(t, rel=1e-9)


def test_repair_blanks_unprovable_row():
    """判据说"这行不自洽"但 ×100 也解释不通(实测 1 行:错行恰好落在解禁日、流通股同日阶跃):
    默认置 NaN 显式标记"不可信",绝不硬塞一个不能自证的数;--keep-unresolved 则原样保留。"""
    # 复刻实测唯一一例 603190/2026-08-17 的形状:流通股当日阶跃(1e8→3e8),
    # 错行恰好就是阶跃当日,且其后所有行 turnover 整段缺失 → 新流通股水平**没有任何**
    # 干净参考点,"×100" 只能拿旧流通股比值去比,差 3 倍 → 无法自证。
    df = _series(n=100)
    df.loc[80, "turnover"] = df.loc[80, "volume"] / 3.0e8       # 真值/100(新流通股)
    df.loc[81:, "turnover"] = np.nan
    fixed, rep = units.repair_turnover_unit(df)
    assert rep["refused"] == 1 and rep["repaired"] == 0
    assert pd.isna(fixed.loc[80, "turnover"])
    kept, rep2 = units.repair_turnover_unit(df, blank_unresolved=False)
    assert rep2["refused"] == 1
    assert kept.loc[80, "turnover"] == pytest.approx(df.loc[80, "turnover"])


def test_mask_needs_volume_column():
    """缺 volume → 无从判断,一律不判(宁可漏报,绝不误报)。"""
    df = pd.DataFrame({"date": pd.bdate_range("2026-01-01", periods=30),
                       "turnover": [0.01] * 30})
    assert not units.turnover_unit_anomaly_mask(df).any()


# ————————————————— 4. 缺列的源不许擦除已有好值 —————————————————
def test_column_poor_source_does_not_erase_amount_turnover(iso):
    """核心红线:主档已有 amount/turnover 的当日 bar,被"只带 OHLCV 的源"再写一次时,
    这两列必须**保留**(此前整行 keep='last' 会擦成 NaN → 近端换手覆盖率塌到 0.04%)。"""
    good = pd.DataFrame({"date": pd.to_datetime(["2026-08-12", "2026-08-13"]),
                         "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
                         "close": [1.0, 1.0], "volume": [1e6, 8186753.0],
                         "amount": [1e7, 1.344168e8], "turnover": [6.9821, 4.2919],
                         "pct_chg": [0.0, 0.0]})
    iso.put_master_kline("603161", good)
    # 腾讯口径的同日 bar:amount/turnover 缺(NaN),volume 略有取整差异
    poor = pd.DataFrame({"date": pd.to_datetime(["2026-08-13"]),
                         "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.05],
                         "volume": [8186800.0], "amount": [np.nan],
                         "turnover": [np.nan], "pct_chg": [0.0]})
    iso.append_master_kline("603161", poor, meta={"source": "fallback_advance"})
    m = iso.get_master_kline("603161").set_index("date")
    row = m.loc[pd.Timestamp("2026-08-13")]
    assert row["turnover"] == pytest.approx(4.2919), "缺列的源把已有 turnover 擦成 NaN 了"
    assert row["amount"] == pytest.approx(1.344168e8)
    assert row["close"] == pytest.approx(1.05), "新源真正带的列仍应覆盖"
    assert row["volume"] == pytest.approx(8186800.0)
    assert len(m) == 2, "同日不得出现重复行"


def test_non_null_new_value_still_overwrites(iso):
    """反向:新数据带了非空值,仍必须覆盖旧值(盘中→收盘更新的既有语义不许被本轮改坏)。"""
    a = pd.DataFrame({"date": pd.to_datetime(["2026-08-12"]), "open": [1.0], "high": [1.0],
                      "low": [1.0], "close": [35.6], "volume": [1e6], "amount": [1e7],
                      "turnover": [1.0], "pct_chg": [0.0]})
    iso.put_master_kline("300209", a)
    b = a.copy()
    b["close"] = 36.5
    b["turnover"] = 2.0
    iso.append_master_kline("300209", b)
    m = iso.get_master_kline("300209")
    assert len(m) == 1
    assert float(m["close"].iloc[0]) == 36.5 and float(m["turnover"].iloc[0]) == 2.0


# ————————————————— 5. 写入闸门自动断言 —————————————————
def test_put_master_records_anomaly_in_meta(iso, caplog):
    df = _series()
    df.loc[60, "turnover"] /= 100.0
    with caplog.at_level("ERROR", logger="store.repo"):
        iso.put_master_kline("000001", df)
    meta = iso.get_master_kline_meta("000001")
    assert meta["turnover_unit_anomaly"]["rows"] == 1
    assert meta["turnover_unit_anomaly"]["dates"]
    assert any("turnover 单位异常" in r.getMessage() for r in caplog.records)


def test_clean_write_records_zero_anomaly(iso):
    """干净时显式写 0,便于区分"检过是干净"与"根本没检过"。"""
    iso.put_master_kline("000002", _series())
    assert iso.get_master_kline_meta("000002")["turnover_unit_anomaly"]["rows"] == 0


def test_strict_mode_raises(iso, monkeypatch):
    """CI/单测闸门:STRICT_TURNOVER_UNIT=1 时混用直接抛,不给"只记一行日志"的机会。"""
    monkeypatch.setenv("STRICT_TURNOVER_UNIT", "1")
    df = _series()
    df.loc[60, "turnover"] /= 100.0
    with pytest.raises(store.TurnoverUnitError):
        iso.put_master_kline("000003", df)


def test_guard_survives_broken_frame(iso):
    """护栏自身绝不能拖垮落盘:退化 df(无 volume)照样写得进去。"""
    df = pd.DataFrame({"date": pd.to_datetime(["2026-08-12"]), "close": [1.0]})
    iso.put_master_kline("000004", df)
    assert len(iso.get_master_kline("000004")) == 1


# ————————————————— 6. 下游现算兜底仍生效 —————————————————
def test_downstream_derivation_still_fills_missing_turnover():
    """缺 turnover 时,反转低换手的现算兜底(commit a42d50c)必须照旧生效——
    本轮"换源补齐"是纵深防御的第一层,不取代它、也不改它的契约。"""
    from tools.strategy.reversal_turnover import derive_missing_turnover_amount
    n = 70
    vol = [2.0e6] * n
    closes = [10.0] * n
    turn = [v / 1.0e8 * 100.0 for v in vol]
    turn[-5:] = [float("nan")] * 5          # 近端整段缺失(腾讯回退的形状)
    amt = [v * 10.0 for v in vol]
    amt[-5:] = [float("nan")] * 5
    t2, a2, info = derive_missing_turnover_amount(closes, vol, turn, amt)
    assert info["turnover_derived"] == 5 and info["amount_derived"] == 5
    assert t2[-1] == pytest.approx(2.0, rel=1e-6)


def test_enrich_skipped_when_columns_complete():
    """回退补齐只在真缺 amount/turnover 时才走 baostock:列齐时**不触网**。"""
    from tools.collectors import master_sync
    tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6],
                         "amount": [1e7], "turnover": [1.0]})
    assert master_sync._lacks_amount_turnover(tail) is False
    assert master_sync._enrich_turnover_amount({"000001": tail})["need"] == 0


def test_enrich_detects_missing_columns():
    from tools.collectors import master_sync
    tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6]})
    assert master_sync._lacks_amount_turnover(tail) is True
    nan_tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6],
                             "amount": [np.nan], "turnover": [np.nan]})
    assert master_sync._lacks_amount_turnover(nan_tail) is True


def test_enrich_fills_from_baostock(monkeypatch):
    """补齐语义:按日期对齐把 baostock 的 amount/turnover 填进缺口,volume/OHLC 不动。"""
    from contextlib import contextmanager

    from tools.collectors import baostock_src, master_sync

    @contextmanager
    def _fake_session():
        yield None

    def _fake_fetch(code, start, end, adjust="qfq"):
        return pd.DataFrame({"date": pd.to_datetime(["2026-09-02", "2026-09-03"]),
                             "volume": [5161198, 5228124],
                             "amount": [8.2508354e7, 8.5885652e7],
                             "turnover": [2.7058, 2.7408]})

    monkeypatch.setattr(baostock_src, "session", _fake_session)
    monkeypatch.setattr(baostock_src, "fetch_one", _fake_fetch)
    tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-02", "2026-09-03"]),
                         "close": [16.24, 16.48], "volume": [5161200.0, 5228100.0],
                         "amount": [np.nan, np.nan], "turnover": [np.nan, np.nan]})
    tails = {"603161": tail}
    stats = master_sync._enrich_turnover_amount(tails)
    assert stats["filled"] == 1 and stats["need"] == 1
    assert stats["failed"] == 0 and stats["still_missing"] == 0 and stats["session_failed"] is False
    got = tails["603161"]
    assert got["turnover"].tolist() == pytest.approx([2.7058, 2.7408])
    assert got["volume"].tolist() == pytest.approx([5161200.0, 5228100.0]), "volume 不该被改"


def test_enrich_is_best_effort_on_source_failure(monkeypatch, caplog):
    """baostock 会话整体失败:保持 NaN、filled=0、**不抛**;且聚合里 failed>0/session_failed
    且升 **error** 级日志——补齐网整体失效正是本 bug 的静默根因,必须有声可巡检。"""
    import logging
    from tools.collectors import baostock_src, master_sync

    def _boom():
        raise ConnectionError("baostock down")

    monkeypatch.setattr(baostock_src, "session", _boom)
    tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6],
                         "amount": [np.nan], "turnover": [np.nan]})
    with caplog.at_level(logging.ERROR, logger="collectors.master_sync"):
        stats = master_sync._enrich_turnover_amount({"000001": tail})
    assert stats["filled"] == 0
    assert stats["failed"] == 1 and stats["session_failed"] is True
    assert stats["still_missing"] == 1 and stats["ratio"] == 1.0
    assert pd.isna(tail["turnover"].iloc[0])
    # 有声:整体失败必须落 error(不再是单票 warning 静默淹没)。
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "补齐网整体失败必须升 error"


def test_enrich_all_single_failures_escalate_error(monkeypatch, caplog):
    """会话通、但每只票补齐都失败(需补 N 但成功 0)→ 聚合 failed==need、filled==0,升 error。"""
    import logging
    from contextlib import contextmanager
    from tools.collectors import baostock_src, master_sync

    @contextmanager
    def _fake_session():
        yield None

    def _fetch_boom(code, start, end, adjust="qfq"):
        raise RuntimeError("no data for %s" % code)

    monkeypatch.setattr(baostock_src, "session", _fake_session)
    monkeypatch.setattr(baostock_src, "fetch_one", _fetch_boom)
    tails = {c: pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6],
                              "amount": [np.nan], "turnover": [np.nan]})
             for c in ("000001", "600000")}
    with caplog.at_level(logging.ERROR, logger="collectors.master_sync"):
        stats = master_sync._enrich_turnover_amount(tails)
    assert stats["need"] == 2 and stats["filled"] == 0 and stats["failed"] == 2
    assert stats["session_failed"] is False and stats["still_missing"] == 2
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "补齐 0 只必须升 error"


def test_enrich_switch_off(monkeypatch):
    """开关:FALLBACK_ENRICH_TURNOVER=0 → 完全不走 baostock,聚合为空(need=0)。"""
    from tools.collectors import baostock_src, master_sync
    monkeypatch.setenv("FALLBACK_ENRICH_TURNOVER", "0")
    monkeypatch.setattr(baostock_src, "session",
                        lambda: (_ for _ in ()).throw(AssertionError("不该触网")))
    tail = pd.DataFrame({"date": pd.to_datetime(["2026-09-03"]), "volume": [1e6]})
    stats = master_sync._enrich_turnover_amount({"000001": tail})
    assert stats["need"] == 0 and stats["filled"] == 0


# ————————————————————————————————————————————————
# turnover 近端整段缺失(NaN)的 volume÷流通股 回填(backfill_turnover_from_volume)
# 锁死语义:①用本票自身正常行 ratio × volume 还原,回填值是百分数、口径不二义;
#          ②volume 缺 / 无参考 → refuse,诚实留 NaN 不猜;③流通股阶跃(解禁)行 refuse。
# ————————————————————————————————————————————————
def _kline_with_gap(float_shares=1.9e8, turn_pct=3.0, n=40, gap=12):
    """造一段流通股恒定的 K线:前 n-gap 行 turnover 正常,末 gap 行 turnover=NaN(volume 在位)。
    恒等式 turnover% = 100 * volume / 流通股 ⇒ volume = turnover% * 流通股 / 100。"""
    dates = pd.bdate_range(end="2026-09-03", periods=n)
    vol = np.full(n, turn_pct * float_shares / 100.0)     # 每日 volume 对应 turn_pct%
    turn = np.full(n, turn_pct, dtype=float)
    turn[n - gap:] = np.nan                                # 末 gap 行缺失
    return pd.DataFrame({"date": dates, "volume": vol, "turnover": turn})


def test_backfill_restores_missing_from_own_ratio():
    """末段 turnover 缺失、volume 在位 → 用本票 ratio 还原,值≈原真值、全部回填。"""
    df = _kline_with_gap(turn_pct=3.0, n=40, gap=12)
    out, rep = units.backfill_turnover_from_volume(df)
    assert rep["filled"] == 12 and rep["refused"] == 0
    filled = pd.to_numeric(out["turnover"], errors="coerce")
    assert filled.notna().all()                            # 缺口补齐
    assert np.allclose(filled.tail(12).to_numpy(), 3.0, rtol=1e-6)  # 还原到真值


def test_backfill_value_is_percent_not_fraction():
    """回填值是百分数(与 MASTER_TURNOVER_UNIT=percent 一致),不是小数(相差 100 倍)。"""
    df = _kline_with_gap(turn_pct=5.0, n=30, gap=8)
    out, _ = units.backfill_turnover_from_volume(df)
    assert 4.9 < float(pd.to_numeric(out["turnover"], errors="coerce").iloc[-1]) < 5.1


def test_backfill_refuses_when_volume_missing():
    """缺失行连 volume 也没有 → 无从还原,refuse 且保持 NaN(不猜)。"""
    df = _kline_with_gap(n=30, gap=6)
    df.loc[df.index[-3:], "volume"] = np.nan               # 末 3 行 volume 也缺
    out, rep = units.backfill_turnover_from_volume(df)
    assert rep["refused"] >= 3
    assert pd.to_numeric(out["turnover"], errors="coerce").tail(3).isna().all()


def test_backfill_refuses_on_float_share_step():
    """参考窗内流通股阶跃(解禁)→ ratio 非常数,refuse 该行而非用错 ratio 外推。"""
    # 前半流通股 1e8、后半(仍有正常 turnover 的参考行)跳到 3e8,末尾再缺失
    n, gap = 40, 6
    dates = pd.bdate_range(end="2026-09-03", periods=n)
    fs = np.where(np.arange(n) < n - 15, 1e8, 3e8).astype(float)
    turn = np.full(n, 3.0)
    vol = turn * fs / 100.0
    turn[n - gap:] = np.nan
    df = pd.DataFrame({"date": dates, "volume": vol, "turnover": turn})
    out, rep = units.backfill_turnover_from_volume(df)
    # 参考窗横跨阶跃 → CV 超阈值 → 拒填(诚实留 NaN),不产生错值
    assert rep["refused"] >= 1


def test_backfill_noop_when_full():
    """无缺失 → 不动数据、filled=refused=0。"""
    df = _kline_with_gap(n=30, gap=0)
    out, rep = units.backfill_turnover_from_volume(df)
    assert rep["filled"] == 0 and rep["refused"] == 0
