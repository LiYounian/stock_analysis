"""逐票技术指标面板(复用 tools/analysis/technical.py 的算子,不自造)。

产出每票逐日的 close/open/high/low/volume/pct_chg + ma3/ma5/ma20 + bias20 +
rsi12 + kdj_k/kdj_j + vol_ratio + oversold(超卖共振) + 前日high/前日close。

超卖(oversold)口径**镜像** technical._overbought_oversold 的 verdict=="超卖":
KDJ(k<超卖_K 或 j<超卖_J) / RSI12<超卖 / BIAS20<超卖 三票中,超卖票数 n_os≥共振数阈值
且 n_os≥n_ob。阈值取 THRESHOLDS["超买超卖"](KDJ超卖_K=20/超卖_J=0, RSI12超卖=30,
BIAS20超卖=-10, 共振数阈值=2)。NaN 视为不成立(与原函数 pd.isna 跳过等价)。

因果性:所有指标为 rolling/递归,只回看;vol_ratio 用**前** 5 日均量(shift(1))防当日自含。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis import technical
from tools.config.strategy import THRESHOLDS

_OBOS = THRESHOLDS["超买超卖"]


def _oversold_flags(bias20: pd.Series, rsi12: pd.Series,
                    k: pd.Series, j: pd.Series) -> pd.DataFrame:
    """向量化超买超卖共振,镜像 technical._overbought_oversold。返回 oversold/overbought 布尔列。"""
    kt, rt, bt = _OBOS["KDJ"], _OBOS["RSI12"], _OBOS["BIAS20"]
    need = int(_OBOS["共振数阈值"])

    kdj_ob = (k > kt["超买_K"]) | (j > kt["超买_J"])
    kdj_os = (k < kt["超卖_K"]) | (j < kt["超卖_J"])
    rsi_ob, rsi_os = rsi12 > rt["超买"], rsi12 < rt["超卖"]
    bias_ob, bias_os = bias20 > bt["超买"], bias20 < bt["超卖"]

    # NaN → False(与原函数 pd.isna 跳过该指标等价:不计入 ob/os 计数)
    def _b(s):
        return s.fillna(False).astype(bool)

    n_ob = _b(kdj_ob).astype(int) + _b(rsi_ob).astype(int) + _b(bias_ob).astype(int)
    n_os = _b(kdj_os).astype(int) + _b(rsi_os).astype(int) + _b(bias_os).astype(int)
    oversold = (n_os >= need) & (n_os >= n_ob)
    overbought = (n_ob >= need) & (n_ob > n_os)
    return pd.DataFrame({"oversold": oversold, "overbought": overbought,
                         "n_os": n_os, "n_ob": n_ob})


def compute_code_indicators(kline_df: pd.DataFrame) -> pd.DataFrame | None:
    """单票全史 K线 → 逐日指标面板(date 升序索引)。样本过短返回 None。"""
    if kline_df is None or len(kline_df) < 25 or "close" not in kline_df.columns:
        return None
    d = kline_df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date").reset_index(drop=True)
    close, high, low = d["close"].astype(float), d["high"].astype(float), d["low"].astype(float)
    open_ = d["open"].astype(float)
    vol = d["volume"].astype(float)

    ma20 = technical.ma(close, 20)
    ma5 = technical.ma(close, 5)
    ma3 = technical.ma(close, 3)
    bias20 = (close - ma20) / ma20 * 100.0
    rsi12 = technical.rsi(close, 12)
    kd = technical.kdj(d)           # 需要 low/high/close 列
    k, j = kd["k"], kd["j"]
    # 量比:当日量 / 前 5 日均量(shift(1) 防当日自含)——镜像 technical.compute 的 vol_ma5_prev
    vol_ratio = vol / vol.rolling(5).mean().shift(1)

    obos = _oversold_flags(bias20, rsi12, k, j)

    out = pd.DataFrame({
        "date": d["date"],
        "open": open_, "high": high, "low": low, "close": close,
        "volume": vol, "pct_chg": d.get("pct_chg"),
        "ma3": ma3, "ma5": ma5, "ma20": ma20,
        "bias20": bias20, "rsi12": rsi12, "kdj_k": k, "kdj_j": j,
        "vol_ratio": vol_ratio,
        "prev_close": close.shift(1), "prev_high": high.shift(1),
        "oversold": obos["oversold"], "overbought": obos["overbought"],
    }).set_index("date")
    return out
