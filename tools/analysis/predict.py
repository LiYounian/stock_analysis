"""预测/推荐引擎(统计版,纯本地)。

产出(全部百分比,与买入手数/金额无关):
- 近三次放量定位、支撑位/压力位
- 各持有期(1/5/10日)止盈止损位 + 最大亏损% + 目标盈利% + 风险收益比
- 各持有期情景预测:上涨概率 + 悲观/中位/乐观收益%(历史频率,非预言)
- 买卖倾向推荐

诚实性:概率是历史频率,区间基于波动率,均非未来保证。仅供参考,非投资建议。
参数见 tools/config/strategy.py THRESHOLDS['预测']。契约见 docs/计划/P3_Web展示与预测引擎.md P3-C。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

_P = THRESHOLDS["预测"]
DISCLAIMER = "概率基于历史频率、区间基于波动率,均非未来保证。仅供参考,非投资建议,风险自负。"


# ---------- 基础量 ----------
def atr(kline: pd.DataFrame, period: int = None) -> pd.Series:
    """ATR(真实波幅均值)。"""
    period = period or _P["ATR周期"]
    h, l, c = kline["high"], kline["low"], kline["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def recent_volume_spikes(kline: pd.DataFrame, n: int = None, ratio: float = None) -> list[dict]:
    """近 n 次放量日(量比>ratio),从最近往前找。返回 {date, close, 量比}。"""
    n = n or _P["近N次放量"]
    ratio = ratio or _P["放量_量比"]
    vol = kline["volume"]
    vol_ma5_prev = vol.rolling(5).mean().shift(1)
    vr = vol / vol_ma5_prev
    out = []
    for i in range(len(kline) - 1, -1, -1):
        if pd.notna(vr.iloc[i]) and vr.iloc[i] > ratio:
            out.append({"date": str(kline["date"].iloc[i])[:10],
                        "close": round(float(kline["close"].iloc[i]), 2),
                        "量比": round(float(vr.iloc[i]), 2)})
            if len(out) >= n:
                break
    return out


def support_resistance(kline: pd.DataFrame, window: int = None, keep: int = None) -> dict:
    """结构性支撑/压力位:swing 高低点 pivot 中,现价下方最近 keep 个为支撑、上方为压力。"""
    window = window or _P["swing窗口"]
    keep = keep or _P["支撑压力_取几档"]
    highs, lows = kline["high"], kline["low"]
    price = float(kline["close"].iloc[-1])
    piv_hi, piv_lo = [], []
    for i in range(window, len(kline) - window):
        seg_h = highs.iloc[i - window:i + window + 1]
        seg_l = lows.iloc[i - window:i + window + 1]
        if highs.iloc[i] == seg_h.max():
            piv_hi.append(round(float(highs.iloc[i]), 2))
        if lows.iloc[i] == seg_l.min():
            piv_lo.append(round(float(lows.iloc[i]), 2))
    supports = sorted({p for p in piv_lo if p < price}, reverse=True)[:keep]
    resistances = sorted({p for p in piv_hi if p > price})[:keep]
    return {"支撑位": supports, "压力位": resistances}


# ---------- 止盈止损(百分比,按持有期)----------
def stop_targets(price: float, atr_pct: float) -> dict:
    """各持有期止损/止盈位 + 最大亏损%/目标盈利% + 风险收益比。"""
    sk, tk = _P["止损_ATR倍数"], _P["止盈_ATR倍数"]
    out = {}
    for N in _P["持有期"]:
        band = atr_pct * math.sqrt(N)          # N 日预期波动(%)
        loss_pct = round(sk * band, 2)
        gain_pct = round(tk * band, 2)
        out[f"{N}日"] = {
            "止损位": round(price * (1 - loss_pct / 100), 2),
            "最大亏损%": loss_pct,
            "止盈位": round(price * (1 + gain_pct / 100), 2),
            "目标盈利%": gain_pct,
            "风险收益比": round(tk / sk, 2),
        }
    return out


# ---------- 情景预测(历史频率)----------
def scenarios(kline: pd.DataFrame) -> dict:
    """各持有期历史 N 日前瞻收益经验分布:上涨概率 + 悲观/中位/乐观分位(%)。"""
    close = kline["close"]
    ql, qm, qh = _P["情景分位"]
    out = {}
    for N in _P["持有期"]:
        fwd = (close.shift(-N) / close - 1).dropna() * 100
        if len(fwd) < 20:
            out[f"{N}日"] = {"上涨概率%": None, "样本数": int(len(fwd))}
            continue
        out[f"{N}日"] = {
            "上涨概率%": round(float((fwd > 0).mean() * 100), 1),
            f"悲观%(q{ql})": round(float(np.percentile(fwd, ql)), 2),
            f"中位%(q{qm})": round(float(np.percentile(fwd, qm)), 2),
            f"乐观%(q{qh})": round(float(np.percentile(fwd, qh)), 2),
            "样本数": int(len(fwd)),
        }
    return out


# ---------- 买卖倾向 ----------
def bias_recommendation(tech: dict, fundflow: dict | None, sentiment: dict | None = None) -> dict:
    """综合超买超卖/拐点/趋势/资金流/情绪打分 → 偏买入/偏卖出/观望。

    sentiment 为 record 的 sentiment 块 dict(含 净情绪分/样本数);None 或样本数为0时不计分,
    保证向后兼容。情绪仅一维输入并入,依据可追溯,非决定项。
    """
    score, reasons = 0, []
    ob = (tech.get("ob_os") or {}).get("结论")
    if ob == "超卖":
        score += 2; reasons.append("超卖+2")
    elif ob == "超买":
        score -= 2; reasons.append("超买-2")

    rev = (tech.get("reversal") or {}).get("拐点标签")
    if rev == "反弹启动":
        score += 2; reasons.append("拐点反弹启动+2")
    elif rev == "超跌待反弹":
        score += 1; reasons.append("超跌待反弹+1")

    rating = (tech.get("signal") or {}).get("评级")
    if rating == "偏多":
        score += 1; reasons.append("趋势偏多+1")
    elif rating == "偏空":
        score -= 1; reasons.append("趋势偏空-1")

    if fundflow:
        zhu = fundflow.get("今日主力净流入")
        streak = fundflow.get("主力连续净流入天数") or 0
        if isinstance(zhu, (int, float)):
            if zhu > 0:
                score += 1; reasons.append("主力净流入+1")
                if streak >= 2:
                    score += 1; reasons.append(f"主力连续{streak}天流入+1")
            elif zhu < 0:
                score -= 1; reasons.append("主力净流出-1")

    if sentiment:
        net = sentiment.get("净情绪分")
        n = sentiment.get("样本数") or 0
        if isinstance(net, (int, float)) and n > 0:
            w = _P["情绪权重"]
            if net >= _P["情绪偏多阈值"]:
                score += w; reasons.append(f"情绪偏多+{w}")
            elif net <= _P["情绪偏空阈值"]:
                score -= w; reasons.append(f"情绪偏空-{w}")

    conclusion = "偏买入" if score >= 2 else ("偏卖出" if score <= -2 else "观望")
    return {"结论": conclusion, "得分": score, "依据": reasons}


# ---------- 汇总 ----------
def predict(kline: pd.DataFrame, tech: dict, fundflow: dict | None = None,
            sentiment: dict | None = None) -> dict:
    """汇总预测/推荐。kline 需含 date/high/low/close/volume;tech=technical.compute 输出。

    sentiment 为 record 的 sentiment 块(可选),透传给买卖倾向作一维并入;None 时行为不变。
    """
    if kline is None or len(kline) < 30:
        return {"error": "数据不足", "n": 0 if kline is None else len(kline)}
    price = float(kline["close"].iloc[-1])
    atr_val = float(atr(kline).iloc[-1])
    atr_pct = atr_val / price * 100 if price else float("nan")

    return {
        "现价": round(price, 2),
        "atr": round(atr_val, 3),
        "atr_pct": round(atr_pct, 2),
        "近三次放量": recent_volume_spikes(kline),
        **support_resistance(kline),
        "持有期建议": stop_targets(price, atr_pct),
        "情景预测": scenarios(kline),
        "买卖倾向": bias_recommendation(tech, fundflow, sentiment),
        "免责": DISCLAIMER,
    }
