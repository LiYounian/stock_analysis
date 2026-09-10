"""IET 探针·真实数据 reader(成分源无关,A/B 两方案都用)。

⚠️ 数据落盘现状:本 worktree 的 data/ 独立且无个股原料;个股K线/估值/board指数**全在主仓**。
故读原料前须 bind_main_repo() 把 store 根指向主仓(只读复用全量历史);
面板写盘另指 probe 目录(见 run_probe.build_and_run),不污染生产。

防未来:
  - 估值 PE/PB:读整条历史序列,按 date≤T 过滤取最后一条(as-of,序列本身即PIT)。
  - 换手:主档K线 date≤T 最后一条。
  - 动量 RS-Momentum:board指数与沪深300按date对齐,取≤T的trailing SMA序列末值(因果)。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd

from tools.store import repo

_MAIN = Path("/Users/yqg/Documents/projects/stock_analysis")


def bind_main_repo() -> None:
    """把 store 读根指向主仓 data(只读复用全量历史原料)。写面板另走 probe 目录。"""
    repo._RAW_DIR = _MAIN / "data" / "raw"
    repo._MASTER_DIR = _MAIN / "data" / "master"
    repo._ANALYSIS_DIR = _MAIN / "data" / "analysis"


def universe_from_master() -> list[str]:
    """全A票池 = 主仓 master/kline 文件名(.parquet)。"""
    kd = _MAIN / "data" / "master" / "kline"
    return sorted(p.stem for p in kd.glob("*.parquet"))


# ————————————————————— 估值(as-of PE/PB) —————————————————————
@lru_cache(maxsize=8192)
def _valuation_series(code: str) -> Optional[pd.DataFrame]:
    try:
        df = repo.get_raw("valuation", code)     # latest 分区 = 整条历史序列
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date")


def _asof_val(code: str, date: str, col: str) -> Optional[float]:
    df = _valuation_series(code)
    if df is None:
        return None
    sub = df[df["date"] <= date]
    if sub.empty:
        return None
    v = sub[col].iloc[-1]
    return None if pd.isna(v) else float(v)


def pe_reader(code: str, date: str) -> Optional[float]:
    return _asof_val(code, date, "PE_TTM")


def pb_reader(code: str, date: str) -> Optional[float]:
    return _asof_val(code, date, "PB")


# ————————————————————— 换手(as-of) —————————————————————
@lru_cache(maxsize=8192)
def _kline(code: str) -> Optional[pd.DataFrame]:
    try:
        df = repo.get_master_kline(code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.sort_values("date")


def turnover_reader(code: str, date: str) -> Optional[float]:
    df = _kline(code)
    if df is None:
        return None
    sub = df[df["date"] <= date]
    if sub.empty:
        return None
    v = sub["turnover"].iloc[-1]
    return None if pd.isna(v) else float(v)


# ————————————————————— 动量(因果 RS-Momentum 序列,每行业一次) —————————————————————
@lru_cache(maxsize=64)
def _momentum_by_date(industry: str) -> dict:
    """行业 board指数 vs 沪深300 → 每日因果 RS-Momentum 映射 {date: mom}。

    board与bench按date inner-merge对齐(避免尾部错位)→ 全历史 rs→ratio→momentum(trailing SMA,因果);
    mom_seq 尾对齐回 date。空/算不出 → {}。
    """
    from tools.collectors import board, index
    from tools.analysis import rrg, industry_map

    sw = industry_map.to_sw(industry) or industry
    try:
        bdf = board.load_board_kline(sw)
        idf = index.load_index(rrg._C["基准"])
    except Exception:
        return {}
    if bdf is None or bdf.empty or idf is None or idf.empty:
        return {}
    b = bdf[["date", "close"]].copy(); b["date"] = pd.to_datetime(b["date"]).dt.strftime("%Y-%m-%d")
    k = idf[["date", "close"]].copy(); k["date"] = pd.to_datetime(k["date"]).dt.strftime("%Y-%m-%d")
    m = b.merge(k, on="date", suffixes=("_b", "_k")).sort_values("date")
    if len(m) < rrg._min_bars() + 5:
        return {}
    dates = m["date"].tolist()
    try:
        rs = rrg.rs_line(m["close_b"].tolist(), m["close_k"].tolist())
        ratio_seq = rrg.rs_ratio_series(rs)
        mom_seq = rrg.rs_momentum_series(ratio_seq)
    except Exception:
        return {}
    if not mom_seq:
        return {}
    offset = len(dates) - len(mom_seq)          # mom_seq 尾对齐(SMA warmup 丢前段)
    if offset < 0:
        return {}
    return {dates[offset + j]: float(mom_seq[j]) for j in range(len(mom_seq))}


def rs_momentum_of(industry: str, date: str) -> Optional[float]:
    """行业在 date 的因果 RS-Momentum(≤date 的最后一个有值日;缺 → None)。"""
    mp = _momentum_by_date(industry)
    if not mp:
        return None
    # 取 ≤date 的最近有值日(date 可能非交易日)
    keys = [d for d in mp if d <= date]
    if not keys:
        return None
    return mp[max(keys)]


def clear_caches() -> None:
    for fn in (_valuation_series, _kline, _momentum_by_date):
        cc = getattr(fn, "cache_clear", None)   # monkeypatch 替换后可能无 cache_clear,防御
        if cc:
            cc()
