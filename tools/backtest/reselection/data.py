"""数据层:全A特征加载 + 向量化持续型排名视图打分 + 全A等权基准。

复用 nextday_kernel 的 universe/load/precompute/build_market(口径一致、防未来)。
向量化「加权对数动量」打分(等价 tools.strategy.momentum.weighted_log_momentum,单测锁)。

⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from tools.research.selection_alpha import nextday_kernel as K

logger = logging.getLogger("backtest.reselection.data")

MOM_LOOKBACK = 25          # 与 momentum.weighted_log_momentum 默认一致
_ANN = 250


def universe_codes(data_root: str, exclude_bj: bool = True) -> list[str]:
    return K.universe_codes(data_root, exclude_bj=exclude_bj)


def load_kline(data_root: str, code: str, min_date: str | None = None) -> pd.DataFrame | None:
    return K.load_kline(data_root, code, min_date=min_date)


# ───────────────────── 向量化加权对数动量(复刻 weighted_log_momentum) ─────────────────────
def _mom_consts(L: int):
    x = np.arange(L, dtype=float)
    w = np.linspace(1.0, 2.0, L)
    W = w ** 2
    W_sum = W.sum()
    x_bar = (W * x).sum() / W_sum
    dx = x - x_bar
    var_x = (W * dx * dx).sum()
    return x, w, W, W_sum, x_bar, dx, var_x


def momentum_score_series(close: np.ndarray, lookback: int = MOM_LOOKBACK) -> np.ndarray:
    """逐日加权对数动量 score(= (exp(slope·250)-1)·R²),对齐输入下标。

    score[t] 用 close[t-lookback..t](共 lookback+1 根),与 weighted_log_momentum(df.iloc[:t+1])
    的 closes[-(lookback+1):] 逐点等价(test_momentum_equiv 锁)。数据不足 → NaN。
    """
    c = np.asarray(close, dtype=float)
    n = len(c)
    L = lookback + 1
    out = np.full(n, np.nan)
    if n < L:
        return out
    with np.errstate(invalid="ignore", divide="ignore"):
        y_all = np.log(c)
    x, w, W, W_sum, x_bar, dx, var_x = _mom_consts(L)
    Y = sliding_window_view(y_all, L)              # (n-L+1, L),窗口 j 覆盖 [j, j+L-1]
    finite = np.isfinite(Y).all(axis=1)
    y_bar = (Y * W).sum(axis=1) / W_sum            # 加权均值
    dy = Y - y_bar[:, None]
    slope = (W * dx * dy).sum(axis=1) / var_x
    intercept = y_bar - slope * x_bar
    ann = np.exp(slope * _ANN) - 1.0
    y_pred = slope[:, None] * x[None, :] + intercept[:, None]
    ss_res = (w * (Y - y_pred) ** 2).sum(axis=1)
    y_mean = Y.mean(axis=1)                         # 注意:ss_tot 用无权均值(与原实现一致)
    ss_tot = (w * (Y - y_mean[:, None]) ** 2).sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        r2 = np.where(ss_tot != 0, 1.0 - ss_res / ss_tot, 0.0)
    score = ann * r2
    score[~finite] = np.nan
    score[var_x == 0] = np.nan                      # 理论不会发生(x 固定),稳妥起见
    # 窗口 j 的 score 落在结束下标 j+L-1
    out[L - 1:] = score
    return out


# ───────────────────── 单票特征(含动量 score) ─────────────────────
def build_feats(data_root: str, codes: list[str], min_date: str,
                with_momentum: bool = True) -> dict[str, dict]:
    feats: dict[str, dict] = {}
    skipped = 0
    for code in codes:
        df = K.load_kline(data_root, code, min_date=min_date)
        if df is None or len(df) < 2:
            skipped += 1
            continue
        f = K.precompute(df)
        if with_momentum:
            f["mom"] = momentum_score_series(f["c"])
        # 近20日均成交额(元),as-of 无未来:liq[t] 用 amount[t-19..t]
        if "amount" in df.columns:
            f["liq"] = pd.Series(df["amount"].to_numpy(float)).rolling(
                20, min_periods=10).mean().to_numpy()
        else:
            f["liq"] = np.full(len(f["c"]), np.nan)
        feats[code] = f
    logger.info("加载特征 %d 只(跳过 %d)", len(feats), skipped)
    return feats


def build_market(feats: dict[str, dict]) -> dict:
    return K.build_market(feats)


def trading_calendar(market: dict) -> list[str]:
    """全A执行日历(有 open→close 的日),升序。"""
    return sorted(market["mkt_ir"])
