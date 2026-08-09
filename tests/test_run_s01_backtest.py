"""S01 全A回测**薄驱动** run_s01_backtest 单测。

锁语义:显式(或自动挑非空)日期分区读 raw → 复用回测器 backtest_one/summarize_trades →
汇总含分板块;自动解析日期时跳过空的 latest 占位分区(本驱动存在的理由)。
不改回测器算法,仅编排。
"""
import pandas as pd
import pytest

from tools.backtest import position_backtest as pb
from tools.backtest import run_s01_backtest as rd
from tools.config import settings
from tools.store import repo as store

E = 240


def _flat(n=260, price=100.0, vol=1000.0):
    return pd.DataFrame({
        "date": pd.bdate_range("2019-01-01", periods=n),
        "open": [price] * n, "close": [price] * n,
        "high": [price + 0.2] * n, "low": [price - 0.2] * n,
        "volume": [vol] * n, "amount": [price * vol] * n,
    })


def _with_rule3(df):
    df = df.copy()
    for col, val in (("open", 125), ("high", 131), ("low", 124), ("close", 130)):
        df.loc[E + 3, col] = val
    return df


def _seed_raw(tmp_path, monkeypatch, date, kl: dict):
    """在 tmp 造一个 raw 日期分区,写入若干票 kline;隔离 DATA_RAW,不碰主仓/软链。"""
    raw = tmp_path / "raw"
    monkeypatch.setattr(settings, "DATA_RAW", raw)
    monkeypatch.setattr(store, "_RAW_DIR", raw)
    kdir = raw / date / "kline"
    kdir.mkdir(parents=True)
    for code, df in kl.items():
        df.to_parquet(kdir / f"{code}.parquet", index=False)
    monkeypatch.setattr(pb, "find_signals", lambda k, cfg=None: [E])  # 定点信号绕历史门槛


def test_run_aggregates_and_layers_by_board(tmp_path, monkeypatch):
    kl = {"600001": _with_rule3(_flat()), "000002": _flat()}   # 沪主板赢 / 深主板平盘不赢
    _seed_raw(tmp_path, monkeypatch, "2026-08-08", kl)
    monkeypatch.setattr(rd, "load_bench", lambda d: None)

    r = rd.run(date="2026-08-08", min_sample=2)
    assert r["数据日期"] == "2026-08-08"
    assert r["扫描票数"] == 2 and r["有效样本票"] == 2 and r["出信号票数"] == 2
    assert r["汇总"]["交易数"] == 2 and r["汇总"]["胜率"] == pytest.approx(0.5)
    assert set(r["分板块"]) == {"沪主板", "深主板"}
    assert r["有基准"] is False and "Alpha" in r["Alpha说明"]


def test_resolve_date_skips_empty_latest_partition(tmp_path, monkeypatch):
    """驱动存在的理由:自动挑日期时跳过空的 latest 占位分区,取有 kline 的最新分区。"""
    kl = {"600001": _flat()}
    _seed_raw(tmp_path, monkeypatch, "2026-08-08", kl)
    (settings.DATA_RAW / "2026-08-09" / "kline").mkdir(parents=True)  # 空占位(如当日盘中)

    assert rd.resolve_data_date() == "2026-08-08"          # 不是空的 08-09
    assert rd.list_codes("2026-08-08") == ["600001"]


def test_limit_truncates_universe(tmp_path, monkeypatch):
    kl = {"600001": _flat(), "000002": _flat(), "300003": _flat()}
    _seed_raw(tmp_path, monkeypatch, "2026-08-08", kl)
    assert len(rd.list_codes("2026-08-08", limit=2)) == 2
