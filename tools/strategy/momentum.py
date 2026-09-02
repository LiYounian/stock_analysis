"""动量类算子与组合选股(移植自聚宽社区 "ETF 动量" + "红利+创业板动量" 双策略)。

原脚本包含实盘专属逻辑(ETF 池/仓位分配/下单/涨停打开/移动止盈止损等),
这里**只提炼可分析层复用的算子**:

    算子(4 个,可独立回测):
      加权对数动量  评分  y=log(close) 加权最小二乘 → 年化收益 × R²
      拉普拉斯低通趋势  信号  EMA 式低通 L[t]=α·close+(1-α)·L[t-1];价上穿 L 且斜率>阈值 → 买
      BBI 站上  信号  BBI=(MA3+MA6+MA12+MA24)/4;价≥BBI×tol → 买,跌破 → 卖
      N日绝对动量  评分  ret = close[-1]/close[-N] - 1(直接作为分数)

    组合选股(2 个,复用上面算子):
      策略A_动量组合    加权对数动量打分排序 → 拉普拉斯闸门 + R² 过滤 → 输出候选
      策略B_红利动量组合  基本面质地过滤 → BBI 闸门 → 24 日动量排序 TopN

组合选股读中心记录(经 timeseries_refs → market.load_kline 拿收盘序列),
不采集、不落记录,守 docs/开发规范.md §5.1 分层单向依赖。
"""
from __future__ import annotations

import logging
import math
from typing import Iterable

import numpy as np
import pandas as pd

from tools.strategy.registry import strategy

logger = logging.getLogger("strategy.momentum")


# ————————————————————————————————————————————————
# 公共:取收盘价 list[float](DataFrame['close'] / Series / list 三种入参兼容)
# ————————————————————————————————————————————————
def _closes(kline_df) -> list[float]:
    if hasattr(kline_df, "columns") and "close" in getattr(kline_df, "columns"):
        return [float(x) for x in kline_df["close"].tolist()]
    if hasattr(kline_df, "tolist"):
        return [float(x) for x in kline_df.tolist()]
    return [float(x) for x in kline_df]


# ————————————————————————————————————————————————
# 算子 1:加权对数动量(评分)
# ————————————————————————————————————————————————
@strategy("加权对数动量", "评分",
          params_schema={"kline_df": "含 close 列的 DataFrame 或收盘价序列",
                         "lookback_days": "回看窗口(默认 25)"})
def weighted_log_momentum(kline_df, lookback_days: int = 25) -> dict:
    """加权对数动量评分。

    对最近 `lookback_days+1` 根 log(close) 做**权重递增的加权最小二乘**:
    权重 = (linspace(1,2,n))**2 —— 越近的样本权越大,抑制远端噪声。
    输出 score = 年化收益(exp(斜率·250)-1) × R²,越大越强势。

    数据不足返回 {score: 0.0, 依据: [...]},不抛错(守 docs §5.4 诚实性)。
    """
    closes = _closes(kline_df)
    if len(closes) < lookback_days + 1:
        return {"score": 0.0, "依据": [f"数据不足({len(closes)}<{lookback_days + 1})"]}

    y = np.log(np.asarray(closes[-(lookback_days + 1):], dtype=float))
    x = np.arange(len(y), dtype=float)
    w = np.linspace(1.0, 2.0, len(y))
    W = w ** 2
    W_sum = W.sum()
    x_bar = (W * x).sum() / W_sum
    y_bar = (W * y).sum() / W_sum
    dx, dy = x - x_bar, y - y_bar
    var_x = (W * dx * dx).sum()
    if var_x == 0:
        return {"score": 0.0, "依据": ["方差为 0"]}
    slope = (W * dx * dy).sum() / var_x
    intercept = y_bar - slope * x_bar
    annualized = math.exp(slope * 250) - 1

    y_pred = slope * x + intercept
    ss_res = (w * (y - y_pred) ** 2).sum()
    ss_tot = (w * (y - y.mean()) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot else 0.0
    score = annualized * r2
    return {
        "score": float(score),
        "依据": [f"年化={annualized:.3f}", f"R²={r2:.3f}", f"lookback={lookback_days}"],
        "annualized": float(annualized),
        "r_squared": float(r2),
        "slope": float(slope),
    }


# ————————————————————————————————————————————————
# 算子 2:拉普拉斯低通趋势(信号)
# ————————————————————————————————————————————————
def _laplace_filter(prices: np.ndarray, s: float) -> np.ndarray:
    """EMA 式低通滤波:L[t] = α·p[t] + (1-α)·L[t-1],其中 α = 1 - exp(-s)。"""
    alpha = 1.0 - math.exp(-s)
    L = np.empty(len(prices))
    L[0] = prices[0]
    for t in range(1, len(prices)):
        L[t] = alpha * prices[t] + (1.0 - alpha) * L[t - 1]
    return L


@strategy("拉普拉斯低通趋势", "信号",
          params_schema={"kline_df": "含 close 列的 DataFrame 或收盘价序列",
                         "s": "衰减常数(默认 0.07)",
                         "min_slope": "斜率下限(默认 0.002)"})
def laplace_trend_signal(kline_df, s: float = 0.07, min_slope: float = 0.002) -> list[str]:
    """拉普拉斯滤波趋势信号。

    每根 t:
      price > L[t] 且 L[t]-L[t-1] > min_slope     → "买"
      price < L[t] 或 L[t]-L[t-1] < -min_slope    → "卖"
      其余                                          → "持"

    前 2 根无法算斜率,一律 "持"(无未来函数,只用 t 及以前数据)。
    """
    closes = _closes(kline_df)
    n = len(closes)
    out = ["持"] * n
    if n < 3:
        return out
    P = np.asarray(closes, dtype=float)
    L = _laplace_filter(P, s)
    for t in range(2, n):
        slope = L[t] - L[t - 1]
        if P[t] > L[t] and slope > min_slope:
            out[t] = "买"
        elif P[t] < L[t] or slope < -min_slope:
            out[t] = "卖"
    return out


# ————————————————————————————————————————————————
# 算子 3:BBI 站上(信号)
# ————————————————————————————————————————————————
def _bbi(prices: np.ndarray) -> np.ndarray:
    """BBI = (MA3+MA6+MA12+MA24)/4;前 23 根不足以算 → NaN。"""
    n = len(prices)
    bbi = np.full(n, np.nan)
    if n < 24:
        return bbi
    s = pd.Series(prices)
    ma3 = s.rolling(3).mean()
    ma6 = s.rolling(6).mean()
    ma12 = s.rolling(12).mean()
    ma24 = s.rolling(24).mean()
    return ((ma3 + ma6 + ma12 + ma24) / 4.0).to_numpy()


@strategy("BBI站上", "信号",
          params_schema={"kline_df": "含 close 列的 DataFrame 或收盘价序列",
                         "tolerance": "站上容差(默认 0.98,即 close≥BBI×0.98 视为站上)"})
def bbi_signal(kline_df, tolerance: float = 0.98) -> list[str]:
    """BBI 多空线信号。

    close ≥ BBI × tolerance     → "买"(允许贴近 BBI 视为站上)
    close < BBI × tolerance     → "卖"
    BBI 不可算(数据 <24 根)     → "持"

    tolerance 沿用原脚本 0.98:BBI 通常滞后,给个 2% 的贴近容差避免误杀。
    """
    closes = _closes(kline_df)
    n = len(closes)
    out = ["持"] * n
    if n < 24:
        return out
    P = np.asarray(closes, dtype=float)
    B = _bbi(P)
    for t in range(23, n):
        if np.isnan(B[t]):
            continue
        threshold = B[t] * tolerance
        if P[t] >= threshold:
            out[t] = "买"
        else:
            out[t] = "卖"
    return out


# ————————————————————————————————————————————————
# 算子 4:N 日绝对动量(评分)
# ————————————————————————————————————————————————
@strategy("N日绝对动量", "评分",
          params_schema={"kline_df": "含 close 列的 DataFrame 或收盘价序列",
                         "n": "回看天数(默认 30,原脚本创业板腿用 31 根含端点=30 日区间)"})
def n_day_momentum(kline_df, n: int = 30) -> dict:
    """N 日绝对动量:score = close[-1] / close[-1-n] - 1(与原脚本 31 根切片等价)。

    数据不足返回 score=0.0。
    """
    closes = _closes(kline_df)
    if len(closes) < n + 1:
        return {"score": 0.0, "依据": [f"数据不足({len(closes)}<{n + 1})"]}
    ret = closes[-1] / closes[-1 - n] - 1.0
    return {
        "score": float(ret),
        "依据": [f"{n}日累计收益={ret:.3%}"],
        "period_return": float(ret),
    }


# ————————————————————————————————————————————————
# 中心记录 → 收盘序列(组合选股用)
# 只读 store,不采集;缺 kline 返回 None(诚实降级)。
# ————————————————————————————————————————————————
def _load_closes_from_record(code: str) -> np.ndarray | None:
    try:
        from tools.collectors import market
        kdf = market.load_kline_recent(code)
    except (FileNotFoundError, ImportError):
        return None
    except Exception:
        return None
    if kdf is None or len(kdf) == 0 or "close" not in kdf.columns:
        return None
    return kdf["close"].astype(float).to_numpy()


# ————————————————————————————————————————————————
# 组合选股 A:加权对数动量 + 拉普拉斯闸门 + R² 过滤
# ————————————————————————————————————————————————
@strategy("策略A_动量组合", "选股",
          params_schema={"records": "dict[code, 中心记录]",
                         "lookback_days": "动量回看(默认 25)",
                         "r2_min": "R² 门槛(默认 0.4)",
                         "top_k": "取前 K 只(默认 3;原脚本 A 单持 1,但选股层放宽)",
                         "s": "拉普拉斯 s(默认 0.07)",
                         "min_slope": "拉普拉斯斜率下限(默认 0.002)"})
def combo_momentum_screen(records: dict[str, dict],
                          lookback_days: int = 25,
                          r2_min: float = 0.4,
                          top_k: int = 3,
                          s: float = 0.07,
                          min_slope: float = 0.002,
                          closes_loader=None,
                          return_scored: bool = False):
    """策略 A 提炼版(改跑 A 股):加权对数动量打分 → R² + 拉普拉斯闸门过滤 → 排序取 TopK。

    原脚本对 ETF 池:动量粗筛 Top10 → 拉普拉斯/R²/均线/量能/短期风控/溢价率多重闸门 → Top1。
    本项目 A 股票池 32 只 + 无 ETF 数据,只保留**动量 + R² + 拉普拉斯**三重(其余闸门本项目
    另有 predict 层与 screener 覆盖,不重复)。

    closes_loader: 可选 `code -> np.ndarray|None` 的注入。缺省走 market.load_kline(采集层),
    web 层可传 store-only 版本以守分层(见 web/data_access._store_closes_loader)。

    return_scored=True:返回**过 R²+拉普拉斯闸门的全部候选**(按动量分降序的 [(code, score)]),
    不截断 top_k——供上层(如 screen_momentum 高位超买抑制层)重排序后再取 TopK。缺省 False 时返回
    [code] TopK,行为与旧版逐字节一致(向后兼容)。
    """
    loader = closes_loader or _load_closes_from_record
    scored: list[tuple[str, float]] = []
    for code, rec in (records or {}).items():
        closes = loader(code)
        if closes is None or len(closes) < max(lookback_days + 1, 3):
            continue
        mom = weighted_log_momentum(closes, lookback_days=lookback_days)
        if mom.get("r_squared", 0.0) < r2_min:
            continue
        sig = laplace_trend_signal(closes, s=s, min_slope=min_slope)
        if not sig or sig[-1] != "买":
            continue
        scored.append((code, float(mom["score"])))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    if return_scored:
        return scored
    return [c for c, _ in scored[:top_k]]


# ————————————————————————————————————————————————
# 组合选股 B:质地 + BBI 闸门 + 24 日动量排序
# ————————————————————————————————————————————————
def _get(rec: dict, *path):
    cur = rec
    for k in path:
        cur = (cur or {}).get(k) if isinstance(cur, dict) else None
    return cur


def _pass_quality(rec: dict, roe_min: float, rev_growth_min: float,
                  profit_growth_min: float) -> bool:
    """质地过滤(替代原脚本股息率+PE/ROE/营收/净利增速的组合)。

    本项目 fundamental 字段口径:ROE/营收增速/净利增速 均为百分数(数值型)。
    None 视为不通过(信息缺失不假设优质)。
    """
    roe = _get(rec, "fundamental", "ROE")
    rev = _get(rec, "fundamental", "营收增速")
    prof = _get(rec, "fundamental", "净利增速")
    if not all(isinstance(v, (int, float)) for v in (roe, rev, prof)):
        return False
    return roe >= roe_min and rev >= rev_growth_min and prof >= profit_growth_min


@strategy("策略B_红利动量组合", "选股",
          params_schema={"records": "dict[code, 中心记录]",
                         "top_k": "取前 K 只(默认 6,对应原脚本红利腿仓位)",
                         "roe_min": "ROE 下限(%,默认 1.0,与 config.选股.质地_ROE下限 同量级)",
                         "rev_growth_min": "营收增速下限(%,默认 5)",
                         "profit_growth_min": "净利增速下限(%,默认 10)",
                         "bbi_tolerance": "BBI 站上容差(默认 0.98)",
                         "momentum_days": "动量回看(默认 23,对应原脚本 24 根切片)"})
def combo_dividend_momentum_screen(records: dict[str, dict],
                                   top_k: int = 6,
                                   roe_min: float = 1.0,
                                   rev_growth_min: float = 5.0,
                                   profit_growth_min: float = 10.0,
                                   bbi_tolerance: float = 0.98,
                                   momentum_days: int = 23,
                                   closes_loader=None) -> list[str]:
    """策略 B 红利腿提炼版:质地过滤 → BBI 闸门 → N 日动量排序 TopK。

    原脚本红利腿:PE(5-50)+ROE 增速+营收/净利增速+股息率>3% 基本面筛 → BBI 站上过滤 →
    24 根切片累计收益排序 → Top6。本项目**无股息率数据**,用现有 fundamental 三项
    (ROE/营收增速/净利增速)替代,阈值可调;其余核心逻辑(BBI 闸门 + 24 根动量)保留。

    momentum_days=23 对应原脚本 24 根切片的 (last/first - 1),含端点计算等价。
    closes_loader:同 combo_momentum_screen(依赖倒置,方便 web 传 store-only 加载器)。
    """
    loader = closes_loader or _load_closes_from_record
    scored: list[tuple[str, float]] = []
    for code, rec in (records or {}).items():
        if not _pass_quality(rec, roe_min, rev_growth_min, profit_growth_min):
            continue
        closes = loader(code)
        if closes is None or len(closes) < 24:
            continue
        bbi_vals = _bbi(closes)
        if np.isnan(bbi_vals[-1]) or closes[-1] < bbi_vals[-1] * bbi_tolerance:
            continue
        mom = n_day_momentum(closes, n=momentum_days)
        if "period_return" not in mom:
            continue
        scored.append((code, float(mom["period_return"])))
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return [c for c, _ in scored[:top_k]]
