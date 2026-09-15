"""次日执行实盘口径内核(vendored primitives)。

口径与 `tools/backtest/nextday_entry.py`(分支 origin/feat/nextday-entry-backtest)一致,
为保持本分支自包含(基于 origin/main,不依赖其它 chip 的分支专属文件)而复刻其核心函数,
并新增「涨停不可买」过滤(Doc3 §7.2)。等价性由 tests 对拍原始实现锁定。

⚠️ 测试环境研究模拟,非投资建议。全部向量化、无前视(前向量仅作标签)。
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd

_BJ_PREFIX = ("8", "4")

# ── 预注册入场档网格(动手前写死;跑完不改) ──
LIMIT_PC_KS = (0.0, 0.01)          # 主口径:P=close[t]*(1-k)(Pareto 最优档)
BETA_LO, BETA_HI = 0.8, 1.2
BETA_WIN, BETA_MIN = 60, 40
OOS_SPLIT_DEFAULT = "2026-09-04"    # 样本区间短,晚段起点(见报告"OOS 局限"说明)

# 涨停线(板块) —— Doc3 §7.2
LIMIT_MAIN = 0.10                    # 主板 60/00/001/002/003
LIMIT_GEM = 0.20                     # 创业板 300 / 科创板 688
LIMIT_TOL = 0.005                   # open ≥ close*(1+limit-0.005) 判不可买


def board_limit(code: str) -> float:
    """按板块返回涨停幅度。北交所在 universe 已排除。ST(5%) 不可由 code 判,按板近似(标注)。"""
    if code.startswith("300") or code.startswith("688"):
        return LIMIT_GEM
    return LIMIT_MAIN


def universe_codes(data_root: str, exclude_bj: bool = True) -> list[str]:
    d = os.path.join(data_root, "data", "master", "kline")
    out = []
    for fn in os.listdir(d):
        if not fn.endswith(".parquet"):
            continue
        code = fn[:-8]
        if exclude_bj and code[:1] in _BJ_PREFIX:
            continue
        out.append(code)
    return sorted(out)


def load_kline(data_root: str, code: str, min_date: str | None = None) -> pd.DataFrame | None:
    path = os.path.join(data_root, "data", "master", "kline", f"{code}.parquet")
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if df is None or "date" not in df.columns or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    if min_date is not None:
        # 保留 min_date 之前一段缓冲(供 MA60/β 滚动),按行裁而非按日裁
        keep = df["date"] >= pd.Timestamp(min_date)
        if keep.any():
            first = int(np.argmax(keep.to_numpy()))
            start = max(0, first - (BETA_WIN + 65))
            df = df.iloc[start:].reset_index(drop=True)
    return df


def precompute(df: pd.DataFrame) -> dict:
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    to = df["turnover"].to_numpy(float) if "turnover" in df.columns else np.full(len(c), np.nan)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    n = len(c)
    prev = np.empty(n); prev[0] = np.nan; prev[1:] = c[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ir = c / o - 1.0
        ret_cc = c / prev - 1.0
    ir[~(o > 0)] = np.nan
    sc = pd.Series(c)
    ma20 = sc.rolling(20, min_periods=20).mean().to_numpy()
    ma60 = sc.rolling(60, min_periods=60).mean().to_numpy()

    def past_ret(k):
        r = np.full(n, np.nan)
        if n > k:
            r[k:] = c[k:] / c[:-k] - 1.0
        return r

    ret5, ret10, ret20 = past_ret(5), past_ret(10), past_ret(20)
    to_ma20 = pd.Series(to).rolling(20, min_periods=10).mean().to_numpy()
    return dict(dates=dates, o=o, h=h, lo=lo, c=c, prev=prev, ir=ir, ret_cc=ret_cc,
                turnover=to, to_ma20=to_ma20, ma20=ma20, ma60=ma60,
                ret5=ret5, ret10=ret10, ret20=ret20, n=n)


def build_market(feats: dict) -> dict:
    """全A等权:mkt_ir[d](open→close)、mkt_ret_cc[d](昨收→今收)、breadth[d](c>ma20 占比)。"""
    acc_ir: dict[str, list] = {}
    acc_cc: dict[str, list] = {}
    acc_br: dict[str, list] = {}
    for f in feats.values():
        d = f["dates"]
        ir, cc, c, ma20 = f["ir"], f["ret_cc"], f["c"], f["ma20"]
        for j in np.nonzero(~np.isnan(ir))[0]:
            cell = acc_ir.get(d[j]); acc_ir[d[j]] = [cell[0] + ir[j], cell[1] + 1] if cell else [float(ir[j]), 1]
        for j in np.nonzero(~np.isnan(cc))[0]:
            cell = acc_cc.get(d[j]); acc_cc[d[j]] = [cell[0] + cc[j], cell[1] + 1] if cell else [float(cc[j]), 1]
        valid = ~np.isnan(ma20)
        for j in np.nonzero(valid)[0]:
            above = 1.0 if c[j] > ma20[j] else 0.0
            cell = acc_br.get(d[j]); acc_br[d[j]] = [cell[0] + above, cell[1] + 1] if cell else [above, 1]
    mkt_ir = {d: s / n for d, (s, n) in acc_ir.items() if n}
    mkt_cc = {d: s / n for d, (s, n) in acc_cc.items() if n}
    breadth = {d: s / n for d, (s, n) in acc_br.items() if n}
    return dict(mkt_ir=mkt_ir, mkt_cc=mkt_cc, breadth=breadth)


def rolling_beta(stock_cc: np.ndarray, mkt_cc_aligned: np.ndarray) -> np.ndarray:
    s = pd.Series(stock_cc, dtype=float)
    m = pd.Series(mkt_cc_aligned, dtype=float)
    good = s.notna() & m.notna()
    s = s.where(good); m = m.where(good)
    r = lambda x: x.rolling(BETA_WIN, min_periods=BETA_MIN)  # noqa: E731
    cnt = good.astype(float).where(good).rolling(BETA_WIN, min_periods=BETA_MIN).sum()
    mean_s = r(s).sum() / cnt
    mean_m = r(m).sum() / cnt
    mean_sm = r(s * m).sum() / cnt
    mean_mm = r(m * m).sum() / cnt
    cov = mean_sm - mean_s * mean_m
    var = mean_mm - mean_m * mean_m
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = (cov / var).to_numpy().copy()
    beta[~(var.to_numpy() > 0)] = np.nan
    return beta


def entry_prices(feat: dict, t_idx: np.ndarray) -> dict:
    """信号索引 t_idx → D+1 各入场档挂价 P(未含成交判定)。"""
    c_t = feat["c"][t_idx]
    o_n = feat["o"][t_idx + 1]
    out = {"open": o_n.copy()}
    for k in LIMIT_PC_KS:
        out[f"limit_pc_{k}"] = c_t * (1.0 - k)
    return out


def fill_and_return(P: np.ndarray, o_n, h_n, l_n, c_n, model: str):
    """挂价 P + D+1 OHLC → (fill, ret, filled)。
    marketable: open≤P→open; 否则 low≤P→P; 否则未触发。
    doc:        low≤P≤high→P; 否则未触发。
    """
    P = np.asarray(P, float)
    fill = np.full(len(P), np.nan)
    if model == "marketable":
        take_open = o_n <= P
        fill[take_open] = o_n[take_open]
        rest = ~take_open & (l_n <= P)
        fill[rest] = P[rest]
    elif model == "doc":
        ok = (l_n <= P) & (P <= h_n)
        fill[ok] = P[ok]
    else:
        raise ValueError(model)
    filled = ~np.isnan(fill)
    ret = np.full(len(P), np.nan)
    ret[filled] = c_n[filled] / fill[filled] - 1.0
    return fill, ret, filled


def limit_up_unbuyable(code: str, close_t: np.ndarray, open_n: np.ndarray) -> np.ndarray:
    """Doc3 §7.2: open[D+1] ≥ close[D]*(1+limit-0.005) → 次日开盘涨停不可买(移出可交易集)。"""
    lim = board_limit(code)
    with np.errstate(invalid="ignore"):
        return open_n >= close_t * (1.0 + lim - LIMIT_TOL)


def net_return(ret: np.ndarray, cost_bps: float) -> np.ndarray:
    cost = cost_bps / 1e4
    return (1.0 + ret) * (1.0 - cost) - 1.0
