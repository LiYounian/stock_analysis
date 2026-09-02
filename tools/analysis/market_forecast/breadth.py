"""市场广度聚合器(纯本地,不触网)——大盘预测的"广度"维。

扫 master/kline 全A日K,逐交易日横截面聚合:
  · 涨跌家数(adv/dec/flat)与净涨占比
  · 涨停/跌停家数(**涨停线按板块**:主板±10% / 创业·科创±20% / 北交所±30% / 主板ST±5%)
  · 创 N 日新高/新低家数(默认 20/60)
  · 破位广度(收盘跌破 MA20 的家数占比)
  · 全市场中位 / 均值涨幅、站上 MA20 占比

输出**可回测的历史广度日序列**(DataFrame,index=date)。防未来函数:每个交易日的
广度只用该日及之前的横截面(rolling 新高/MA 均为因果窗),不引入未来行情。

涨停线启发式(无 ST 名单时):按代码前缀路由板块主限价;主板另用 5% 带兜住 ST,
即"封板 + pct 落在某板块允许限价的容差带内"→ 记涨停/跌停。见 §说明。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("market_forecast.breadth")

_CFG = THRESHOLDS["大盘预测"]


# ————————————————————————— 板块 / 涨停线 —————————————————————————
def board_of(code: str) -> str:
    """按代码前缀判板块:主板 / 创业板 / 科创板 / 北交所。"""
    code = str(code)
    if code[:2] == "30":
        return "创业板"
    if code[:2] == "68":
        return "科创板"
    if code[:2] in ("92", "83", "87", "43") or code[:1] in ("8", "4"):
        return "北交所"
    return "主板"  # 60 / 00


def allowed_limits(code: str, cfg: dict | None = None) -> list[float]:
    """该票**允许的**涨停线百分比集合(主板含 ST 的 5%,故 {10,5})。"""
    cfg = cfg or _CFG
    L = cfg["涨停线"]
    b = board_of(code)
    if b == "主板":
        return [L["主板"], L["ST"]]           # 10% 或(ST)5%
    if b == "创业板":
        return [L["创业板"]]
    if b == "科创板":
        return [L["科创板"]]
    return [L["北交所"]]


def _hit_limit(pct: np.ndarray, close: np.ndarray, high: np.ndarray,
               low: np.ndarray, limits: list[float], tol: float,
               seal_tol: float, up: bool) -> np.ndarray:
    """逐行判涨停(up=True)/跌停(up=False):封板 + pct 落在允许限价容差带内。

    封板:涨停要求 close≈high(收在最高),跌停要求 close≈low。pct 需 |pct∓L|≤tol。
    """
    if up:
        sealed = close >= high * (1.0 - seal_tol)
        near = np.zeros_like(pct, dtype=bool)
        for L in limits:
            near |= np.abs(pct - L) <= tol
    else:
        sealed = close <= low * (1.0 + seal_tol)
        near = np.zeros_like(pct, dtype=bool)
        for L in limits:
            near |= np.abs(pct + L) <= tol
    return sealed & near


# ————————————————————————— 单票 → 逐日指标 —————————————————————————
def _per_stock_indicators(df: pd.DataFrame, code: str,
                          cfg: dict | None = None) -> pd.DataFrame | None:
    """单票 K线 → 逐日广度贡献(date + 各计数列,均为 0/1 或数值)。样本过短→None。

    因果性:新高/新低用 rolling(window).max/min(含当日,只回看);MA20 同理;pct_chg 为当日。
    """
    cfg = cfg or _CFG
    if df is None or len(df) < 2 or "close" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    close = d["close"].astype(float)
    high = d["high"].astype(float)
    low = d["low"].astype(float)
    # pct_chg:优先用现成列,缺则用 close 环比(首行 NaN)
    if "pct_chg" in d.columns and d["pct_chg"].notna().any():
        pct = d["pct_chg"].astype(float).to_numpy()
    else:
        pct = (close.pct_change() * 100.0).to_numpy()

    c, h, lo = close.to_numpy(), high.to_numpy(), low.to_numpy()
    tol = float(cfg["涨停容差"])
    seal = float(cfg["封板容差"])
    lims = allowed_limits(code, cfg)

    up = pct > 0
    dn = pct < 0
    flat = pct == 0
    lu = _hit_limit(pct, c, h, lo, lims, tol, seal, up=True)
    ld = _hit_limit(pct, c, h, lo, lims, tol, seal, up=False)

    out = {"date": d["date"].values,
           "listed": np.ones(len(d), dtype=int),
           "adv": up.astype(int), "dec": dn.astype(int), "flat": flat.astype(int),
           "limit_up": lu.astype(int), "limit_down": ld.astype(int),
           "pct": pct}

    for w in cfg["新高新低窗口"]:
        roll_hi = high.rolling(w, min_periods=w).max()
        roll_lo = low.rolling(w, min_periods=w).min()
        out[f"nh{w}"] = (close >= roll_hi).astype(int).to_numpy()
        out[f"nl{w}"] = (close <= roll_lo).astype(int).to_numpy()

    maw = int(cfg["破位MA"])
    ma = close.rolling(maw, min_periods=maw).mean()
    out[f"above_ma{maw}"] = (close > ma).astype(int).to_numpy()
    out[f"below_ma{maw}"] = (close < ma).astype(int).to_numpy()
    out[f"ma{maw}_valid"] = ma.notna().astype(int).to_numpy()

    return pd.DataFrame(out)


# ————————————————————————— 全市场聚合 —————————————————————————
def compute_breadth(codes: list[str] | None = None, data_root=None,
                    chunk: int = 800, cfg: dict | None = None) -> pd.DataFrame:
    """扫 master/kline 全A → 逐交易日广度日序列(index=date 升序)。

    分块累加(groupby.sum 逐块相加),内存有界。列见模块 docstring。
    衍生比率列:adv_dec_ratio / net_adv / limit_net / nh_net_20 / breadth_ma20 等。
    """
    cfg = cfg or _CFG
    from tools.analysis.market_forecast.dataroot import ensure_data_root
    from tools.store import repo as store

    ensure_data_root(str(data_root) if data_root else None)
    codes = codes or store.list_master_codes()
    if not codes:
        raise RuntimeError("master/kline 无代码;检查数据根")

    maw = int(cfg["破位MA"])
    count_cols = ["listed", "adv", "dec", "flat", "limit_up", "limit_down",
                  f"above_ma{maw}", f"below_ma{maw}", f"ma{maw}_valid"]
    for w in cfg["新高新低窗口"]:
        count_cols += [f"nh{w}", f"nl{w}"]

    acc_counts: pd.DataFrame | None = None      # 计数列逐块相加
    pct_sum: pd.Series | None = None            # 涨幅和(算均值)
    pct_lists: dict = {}                        # date -> list(pct)(算中位数)

    buf = []
    n_used = 0
    for i, code in enumerate(codes):
        try:
            df = store.get_master_kline(code)
        except Exception:
            continue
        ind = _per_stock_indicators(df, code, cfg)
        if ind is None:
            continue
        buf.append(ind)
        n_used += 1
        if len(buf) >= chunk or i == len(codes) - 1:
            block = pd.concat(buf, ignore_index=True)
            buf = []
            g = block.groupby("date")
            cnt = g[count_cols].sum()
            acc_counts = cnt if acc_counts is None else acc_counts.add(cnt, fill_value=0)
            psum = g["pct"].sum()
            pct_sum = psum if pct_sum is None else pct_sum.add(psum, fill_value=0)
            for dt, sub in block.groupby("date")["pct"]:
                pct_lists.setdefault(dt, []).append(sub.to_numpy())

    if acc_counts is None:
        raise RuntimeError("无可用个股 K线")

    res = acc_counts.sort_index()
    res.index.name = "date"
    total = res["listed"].replace(0, np.nan)
    res["total"] = res["listed"]
    res["net_adv"] = (res["adv"] - res["dec"]) / total
    res["adv_dec_ratio"] = res["adv"] / res["dec"].replace(0, np.nan)
    res["limit_net"] = (res["limit_up"] - res["limit_down"]) / total
    res["limit_up_ratio"] = res["limit_up"] / total
    for w in cfg["新高新低窗口"]:
        res[f"nh_net_{w}"] = (res[f"nh{w}"] - res[f"nl{w}"]) / total
    ma_valid = res[f"ma{maw}_valid"].replace(0, np.nan)
    res["above_ma20_ratio"] = res[f"above_ma{maw}"] / ma_valid
    res["below_ma20_ratio"] = res[f"below_ma{maw}"] / ma_valid   # 破位广度
    # 均值 / 中位涨幅
    res["mean_pct"] = (pct_sum.sort_index() / total)
    med = {dt: float(np.nanmedian(np.concatenate(v))) for dt, v in pct_lists.items()}
    res["median_pct"] = pd.Series(med).sort_index()

    res.attrs["n_stocks_used"] = n_used
    logger.info("广度聚合完成:%d 票 · %d 交易日", n_used, len(res))
    return res


def load_or_compute(cache_path, codes=None, data_root=None,
                    force: bool = False, cfg: dict | None = None) -> pd.DataFrame:
    """带缓存的广度序列:cache_path 存在且 !force → 直接读 parquet,否则重算并落盘。"""
    import os
    if cache_path and os.path.exists(cache_path) and not force:
        df = pd.read_parquet(cache_path)
        return df.set_index("date") if "date" in df.columns else df
    res = compute_breadth(codes=codes, data_root=data_root, cfg=cfg)
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        res.reset_index().to_parquet(cache_path)
    return res
