"""forward_scorecard 增量化(方案 B:双 mtime 校验 + 只重算失效行)单测。

锁死语义(防未来重写无意破坏):
- 增量(喂 prev)与全量从零 **逐值 + NaN 模式一致**(核心回归锁,CSV 制品级比对)。
- pending 行按与全算完全相同的收益口径(close[idx+N]/close[idx])到期刷新。
- 冻结行(全 r_N 到期)复用 prev,**不触碰 _tilt_labels**(monkeypatch 抛异常仍绿)。
- record json / 该 code K线 parquet 任一 mtime > csv_mtime → 该行/该 code 行重算而非复用。
- --rebuild 强制忽略 prev 全量重算。

全部用临时目录 + 合成数据 + monkeypatch store/market,**绝不碰生产 data/**。
"""
import json
import os
import types

import numpy as np
import pandas as pd
import pytest

from tools.backtest import forward_scorecard as fs

_HZ = (1, 5)
_DATES = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
          "2026-08-07", "2026-08-10", "2026-08-11"]   # 7 个交易日


def _kline_df(dates, closes):
    return pd.DataFrame({"date": pd.to_datetime(dates), "close": [float(c) for c in closes]})


@pytest.fixture
def env(tmp_path, monkeypatch):
    """搭一套临时 store/market:record json + master parquet,monkeypatch 进 fs 模块。"""
    adir = tmp_path / "analysis"
    kdir = tmp_path / "master"
    adir.mkdir()
    kdir.mkdir()

    def rec_path(code, date):
        return adir / date / f"{code}.json"

    def mst_path(code):
        return kdir / f"{code}.parquet"

    def write_record(code, date, trend="偏多", senti=0.1, name="测试股"):
        p = rec_path(code, date)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"meta": {"code": code, "name": name},
               "signals": {"trend": {"评级": trend}},
               "sentiment": {"净情绪分": senti}}
        p.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return p

    def write_kline(code, dates, closes):
        p = mst_path(code)
        _kline_df(dates, closes).to_parquet(p, index=False)
        return p

    def set_mtime(path, t):
        os.utime(path, (t, t))

    # —— monkeypatch:让 build 读临时目录,mtime 用真实文件(可 os.utime 控制)——
    def list_dates(root="analysis"):
        return sorted(p.name for p in adir.iterdir() if p.is_dir())

    def iter_records(date="latest"):
        dd = adir / date
        if not dd.exists():
            return
        for p in sorted(dd.glob("*.json")):
            yield json.loads(p.read_text(encoding="utf-8"))

    def load_kline(code):
        p = mst_path(code)
        if not p.exists():
            raise FileNotFoundError(code)
        return pd.read_parquet(p)

    monkeypatch.setattr(fs.store, "list_dates", list_dates)
    monkeypatch.setattr(fs.store, "iter_records", iter_records)
    monkeypatch.setattr(fs.store, "_record_path", rec_path)
    monkeypatch.setattr(fs.store, "_master_path", mst_path)
    monkeypatch.setattr(fs.market, "load_kline", load_kline)
    # 无 state_pool / 无 LLM:激进版倾斜列走降级留空(确定性)
    monkeypatch.setattr(fs, "_load_pool_index", lambda: None)

    return types.SimpleNamespace(
        tmp=tmp_path, adir=adir, kdir=kdir,
        rec_path=rec_path, mst_path=mst_path,
        write_record=write_record, write_kline=write_kline, set_mtime=set_mtime,
        monkeypatch=monkeypatch)


def _via_csv(df, tmp, tag):
    """经 to_csv/read_csv 归一表示(制品级比对:None/NaN 统一、dtype 归一)。"""
    p = tmp / f"_cmp_{tag}.csv"
    df.to_csv(p, index=False, encoding="utf-8-sig")
    return pd.read_csv(p, dtype={"code": str}).reset_index(drop=True)


def _assert_same(a, b, tmp):
    pd.testing.assert_frame_equal(_via_csv(a, tmp, "a"), _via_csv(b, tmp, "b"),
                                  check_dtype=False, check_like=False)


# ————————————————————————————————————————————————————————————————
def test_incremental_equals_full(env):
    """全量从零 vs 喂 prev 增量 → 逐值 + NaN 模式完全一致(核心回归锁)。

    含冻结行(D0:全 r_N 到期→复用)与 pending 行(D4:r_5 未到期→刷新仍 NaN)。
    """
    code = "600001"
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15, 16])
    env.write_record(code, _DATES[0])   # D0:idx0,idx+5=5<7 → r_1/r_5 均到期(冻结)
    env.write_record(code, _DATES[4])   # D4:idx4,idx+5=9>=7 → r_5 pending,r_1 到期

    full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True)
    prev = _via_csv(full, env.tmp, "prev")
    huge = os.path.getmtime(env.mst_path(code)) + 1e6   # csv 比一切都新 → 无失效
    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True,
                             prev=prev, csv_mtime=huge)

    _assert_same(full, inc, env.tmp)
    # D4 的 r_5 应确实是 pending(NaN),证明我们走的是"复用+刷新"而非误算
    d4 = inc[(inc["date"] == _DATES[4]) & (inc["code"] == code)].iloc[0]
    assert pd.isna(d4["r_5"])
    assert not pd.isna(d4["r_1"])


def test_pending_matures(env):
    """prev 里 r_5 为 pending,K线走出 t+5 → 增量正确补 r_5/hit_5,且与全量逐值一致。"""
    code = "600002"
    d0 = _DATES[0]
    # 先用"短 K线"(只到 idx+3)建 prev:r_1 到期、r_5 pending
    env.write_kline(code, _DATES[:4], [10, 11, 12, 13])
    env.write_record(code, d0)
    prev_full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False)
    prev = _via_csv(prev_full, env.tmp, "prev")
    assert pd.isna(prev[(prev["code"] == code)].iloc[0]["r_5"])   # 建 prev 时确为 pending

    # K线补齐到 idx+6(走出 t+5);csv_mtime 设为比该 K线更新 → 不判失效,走 pending 刷新分支
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15.5, 16])
    huge = os.path.getmtime(env.mst_path(code)) + 1e6
    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False,
                             prev=prev, csv_mtime=huge)

    # 全量(prev=None,长 K线)作 ground truth
    full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False)
    _assert_same(full, inc, env.tmp)

    row = inc[(inc["code"] == code)].iloc[0]
    assert not pd.isna(row["r_5"])                                # 已到期补上
    exp_r5 = (15.5 / 10.0 - 1.0) * 100.0                          # close[idx+5]/close[idx]
    assert row["r_5"] == pytest.approx(exp_r5)
    assert row["hit_5"] == 1                                      # pred_dir=+1,r_5>0 → 命中


def test_frozen_skips_compute(env):
    """monkeypatch _tilt_labels 抛异常:全到期的 prev 走增量仍绿(冻结行未触碰重算);
    对照——同样 monkeypatch 下全量从零会因触发 _tilt_labels 而崩。"""
    code = "600003"
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15, 16])
    env.write_record(code, _DATES[0])   # 全 r_N 到期 → 冻结
    prev_full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True)
    prev = _via_csv(prev_full, env.tmp, "prev")
    huge = os.path.getmtime(env.mst_path(code)) + 1e6

    def _boom(*a, **k):
        raise RuntimeError("冻结行不应调用 _tilt_labels")

    env.monkeypatch.setattr(fs, "_tilt_labels", _boom)

    # 冻结行复用 → 不调 _tilt_labels → 不崩
    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True,
                             prev=prev, csv_mtime=huge)
    _assert_same(prev_full, inc, env.tmp)
    # 对照:全量从零会走全算路径 → 触发异常(证明 monkeypatch 生效、冻结分支确实绕开了它)
    with pytest.raises(RuntimeError):
        fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True)


def test_stale_record_recompute(env):
    """record json mtime > csv_mtime → 该行重算而非复用(拿到改后的 trend)。"""
    code = "600004"
    d0 = _DATES[0]
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15, 16])
    rp = env.write_record(code, d0, trend="偏多")   # pred_dir=+1
    prev_full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False)
    prev = _via_csv(prev_full, env.tmp, "prev")
    assert prev.iloc[0]["pred_dir"] == 1

    T = 1_000_000.0
    env.set_mtime(env.mst_path(code), T - 100)       # K线不失效
    # 改写 record(趋势翻空)并把 mtime 顶到 csv_mtime 之后 → record 失效
    env.write_record(code, d0, trend="偏空")          # pred_dir=-1
    env.set_mtime(rp, T + 100)

    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False,
                             prev=prev, csv_mtime=T)
    assert inc[(inc["code"] == code)].iloc[0]["pred_dir"] == -1   # 重算拿到新 trend


def test_stale_kline_recompute(env):
    """除权 backfill **改写历史前复权价** → 值校验捕获已到期 r_N 不一致 → 该 code 行重算。

    关键:parquet mtime 甚至可以**不**新于 csv_mtime(纯值校验,不再看 mtime),
    重算仍被正确触发——证明规则③已从 mtime 改为值校验。
    """
    code = "600005"
    d0 = _DATES[0]
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15, 16])
    env.write_record(code, d0)
    prev_full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False)
    prev = _via_csv(prev_full, env.tmp, "prev")
    old_r5 = prev.iloc[0]["r_5"]
    assert not pd.isna(old_r5)

    T = 1_000_000.0
    env.set_mtime(env.rec_path(code, d0), T - 100)      # record 不失效
    # 非均匀改写历史价(模拟数据回补/纠错,改变了窗口收益率);mtime 故意压在 csv 之前,证明不靠 mtime
    env.write_kline(code, _DATES, [10, 20, 12, 13, 14, 30, 16])   # r_1、r_5 都变(非等比缩放)
    env.set_mtime(env.mst_path(code), T - 50)

    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=False,
                             prev=prev, csv_mtime=T)
    new_r5 = inc[(inc["code"] == code)].iloc[0]["r_5"]
    assert new_r5 == pytest.approx((30.0 / 10.0 - 1.0) * 100.0)   # 用改写后的新价重算 → 200
    assert new_r5 != pytest.approx(old_r5)


def test_append_new_bar_reuses(env):
    """纯 append 一根**未改历史前复权价**的新 bar → 值校验一致 → 冻结行不重算(不走 tilt)。

    反例对照 test_stale_kline_recompute:daily append 会 bump parquet mtime,若仍按旧规则③
    (parquet mtime)会误判失效重算;值校验只看历史价链是否变,append 不改历史 → 正确复用。
    monkeypatch _tilt_labels 抛异常来证明冻结行确实没走全算。
    """
    code = "600007"
    d0 = _DATES[0]
    # 历史只到 _DATES[:6](6 根),d0 的 r_1/r_5 均已到期(idx0,idx+5=5<6)→ 冻结
    hist = _DATES[:6]
    env.write_kline(code, hist, [10, 11, 12, 13, 14, 15])
    env.write_record(code, d0)
    prev_full = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True)
    prev = _via_csv(prev_full, env.tmp, "prev")
    assert not pd.isna(prev.iloc[0]["r_5"])

    T = 1_000_000.0
    env.set_mtime(env.rec_path(code, d0), T - 100)      # record 不失效
    # 纯 append:历史 6 根价原样不动,仅在尾部加一根新 bar;parquet 被重写 → mtime 变新
    env.write_kline(code, _DATES[:7], [10, 11, 12, 13, 14, 15, 99])
    env.set_mtime(env.mst_path(code), T + 100)          # mtime 新于 csv(旧规则③会误判失效)

    def _boom(*a, **k):
        raise RuntimeError("append 未改历史价,冻结行不应重算/调 _tilt_labels")

    env.monkeypatch.setattr(fs, "_tilt_labels", _boom)
    inc = fs.build_scorecard(dates=None, horizons=_HZ, classify_persist=False, tilt=True,
                             prev=prev, csv_mtime=T)
    # 值校验一致 → 冻结复用,不崩;r_5 沿用 prev(历史价未变)
    _assert_same(prev_full, inc, env.tmp)
    assert inc.iloc[0]["r_5"] == pytest.approx(prev.iloc[0]["r_5"])


def test_rebuild_flag(env):
    """run(rebuild=True) 强制忽略 prev 全量重算;rebuild=False 且未失效则复用旧值。"""
    code = "600006"
    d0 = _DATES[0]
    out = str(env.tmp / "sc.csv")
    env.write_kline(code, _DATES, [10, 11, 12, 13, 14, 15, 16])
    env.set_mtime(env.mst_path(code), 1000.0)        # K线很旧
    env.write_record(code, d0, trend="偏多")
    fs.run(out=out, horizons=_HZ, classify_persist=False, tilt=False)   # 首建(全量)→ pred_dir=+1
    csv_mtime = os.path.getmtime(out)

    # 改写 record 内容(翻空)但把 mtime 压到 csv 之前 → 按 mtime 不失效
    env.write_record(code, d0, trend="偏空")
    env.set_mtime(env.rec_path(code, d0), csv_mtime - 100)

    fs.run(out=out, horizons=_HZ, classify_persist=False, tilt=False, rebuild=False)
    reused = pd.read_csv(out, dtype={"code": str})
    assert reused.iloc[0]["pred_dir"] == 1           # 未失效 → 复用旧值(+1)

    fs.run(out=out, horizons=_HZ, classify_persist=False, tilt=False, rebuild=True)
    rebuilt = pd.read_csv(out, dtype={"code": str})
    assert rebuilt.iloc[0]["pred_dir"] == -1         # 强制全量 → 拿到新 trend(-1)
