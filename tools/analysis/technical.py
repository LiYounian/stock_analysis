"""技术指标分析(纯本地计算,不触网)。

口径对齐国内看盘软件(通达信):KDJ/RSI 用 SMA(X,N,M) 递推,MACD 用 EMA。
输入 K线 DataFrame,输出均线/MACD/KDJ/RSI/量价 + 综合评级。
契约见 docs/计划/P1_技术面打通.md Step 2。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS


# ---------- 基础算子 ----------
def _sma_cn(x: pd.Series, n: int, m: int = 1) -> pd.Series:
    """通达信 SMA(X,N,M):y_t = (m*x_t + (n-m)*y_{t-1}) / n。首个有效值自身作种子。"""
    arr = x.to_numpy(dtype=float)
    out = np.full_like(arr, np.nan)
    prev = None
    for i, v in enumerate(arr):
        if np.isnan(v):
            out[i] = prev if prev is not None else np.nan
            continue
        out[i] = v if prev is None else (m * v + (n - m) * prev) / n
        prev = out[i]
    return pd.Series(out, index=x.index)


def ma(close: pd.Series, window: int) -> pd.Series:
    """简单移动均线。"""
    return close.rolling(window).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD(EMA 口径)。返回 dif / dea / macd(柱=2*(dif-dea))。"""
    dif = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"dif": dif, "dea": dea, "macd": (dif - dea) * 2})


def kdj(kline: pd.DataFrame, n: int = 9, m1: int = 3, m2: int = 3) -> pd.DataFrame:
    """KDJ(通达信口径)。K=SMA(RSV,m1,1),D=SMA(K,m2,1),J=3K-2D。"""
    low_n = kline["low"].rolling(n).min()
    high_n = kline["high"].rolling(n).max()
    rng = (high_n - low_n).replace(0, np.nan)
    rsv = ((kline["close"] - low_n) / rng * 100).clip(0, 100)
    k = _sma_cn(rsv, m1, 1)
    d = _sma_cn(k, m2, 1)
    return pd.DataFrame({"k": k, "d": d, "j": 3 * k - 2 * d})


def rsi(close: pd.Series, window: int) -> pd.Series:
    """RSI(通达信口径,SMA 平滑)。"""
    diff = close.diff()
    up = _sma_cn(diff.clip(lower=0), window, 1)
    down = _sma_cn((-diff).clip(lower=0), window, 1)
    denom = up + down
    out = 100 * up / denom
    out[denom == 0] = 50.0
    return out


def boll(close: pd.Series, window: int = None, k: float = None) -> pd.DataFrame:
    """布林带(BOLL,通达信口径)。中轨=window 日 SMA;上/下轨=中轨 ± k×总体标准差(ddof=0)。

    返回 mid/upper/lower/bandwidth/percent_b:
      - bandwidth = (upper − lower) / mid,量化带口宽窄(缩口/开口用)。
      - percent_b = (close − lower) / (upper − lower),价在带内相对位置;
        >1 破上轨、<0 破下轨、0.5=中轨。上/下轨等宽时(std=0)带宽为0、percent_b 记 NaN。
    """
    t = THRESHOLDS["BOLL"]
    window = window or t["周期"]
    k = k if k is not None else t["倍数"]
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)          # 总体标准差,对齐通达信 STD
    upper = mid + k * std
    lower = mid - k * std
    width = upper - lower
    bandwidth = (width / mid).where(mid != 0)
    percent_b = (close - lower) / width.where(width != 0)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower,
                         "bandwidth": bandwidth, "percent_b": percent_b})


def _boll_state(bl: pd.DataFrame) -> dict:
    """BOLL 快照:上/中/下轨、带宽、%B、%B 位置分档、挤压(带宽处于近 N 日低分位)。

    位置分档(regime 依赖,方向须结合趋势判读,见计划 §F1b):破上轨/触上轨/中性/触下轨/破下轨。
    挤压=缩口=当前带宽 ≤ 近 N 日带宽第 q 百分位 → 波动率压缩、常预示变盘。
    """
    t = THRESHOLDS["BOLL"]
    last = bl.iloc[-1]
    pctb, bw = last["percent_b"], last["bandwidth"]

    def _r(x, nd=2):
        return None if pd.isna(x) else round(float(x), nd)

    if pd.isna(pctb):
        pos = "数据不足"
    elif pctb > 1:
        pos = "破上轨"
    elif pctb >= t["触轨上_percentB"]:
        pos = "触上轨"
    elif pctb < 0:
        pos = "破下轨"
    elif pctb <= t["触轨下_percentB"]:
        pos = "触下轨"
    else:
        pos = "中性"

    hist = bl["bandwidth"].iloc[-t["挤压回看"]:].dropna()
    squeeze = bool((not pd.isna(bw)) and len(hist) >= 20
                   and bw <= np.percentile(hist, t["挤压分位"]))
    return {"上轨": _r(last["upper"]), "中轨": _r(last["mid"]), "下轨": _r(last["lower"]),
            "带宽": _r(bw, 4), "percent_b": _r(pctb, 4), "位置": pos, "挤压": squeeze}


# ---------- 汇总画像 ----------
def _ma_arrangement(ma5, ma10, ma20, ma60) -> str:
    vals = [ma5, ma10, ma20, ma60]
    if any(pd.isna(v) for v in vals):
        return "数据不足"
    if ma5 >= ma10 >= ma20 >= ma60:
        return "多头排列"
    if ma5 <= ma10 <= ma20 <= ma60:
        return "空头排列"
    return "纠缠"


def compute(kline: pd.DataFrame) -> dict:
    """对单票 K线算全套技术指标 + 综合评级。K线不足时相关字段标 '数据不足',不报错。"""
    if kline is None or len(kline) < 2:
        return {"error": "数据不足", "n": 0 if kline is None else len(kline)}
    close = kline["close"]
    n = len(kline)

    ma5, ma10, ma20, ma60 = (ma(close, w).iloc[-1] for w in (5, 10, 20, 60))
    arr = _ma_arrangement(ma5, ma10, ma20, ma60)

    md = macd(close)
    dif, dea, bar = md.iloc[-1]["dif"], md.iloc[-1]["dea"], md.iloc[-1]["macd"]
    prev_bar = md.iloc[-2]["macd"] if n >= 2 else np.nan
    if prev_bar <= 0 < bar:
        macd_state = "金叉"
    elif prev_bar >= 0 > bar:
        macd_state = "死叉"
    else:
        macd_state = "多头" if bar > 0 else "空头"

    kd = kdj(kline)
    k, d, j = kd.iloc[-1]["k"], kd.iloc[-1]["d"], kd.iloc[-1]["j"]
    if pd.isna(k):
        kdj_state = "数据不足"
    elif k > 80:
        kdj_state = "超买"
    elif k < 20:
        kdj_state = "超卖"
    else:
        kdj_state = "-"

    rsi6, rsi12, rsi24 = (rsi(close, w).iloc[-1] for w in (6, 12, 24))

    bl = boll(close)

    bias20 = ((close.iloc[-1] - ma20) / ma20 * 100) if not pd.isna(ma20) and ma20 else np.nan

    vol = kline["volume"]
    vol_ma5_prev = vol.iloc[-6:-1].mean() if n >= 6 else np.nan
    vol_ratio = vol.iloc[-1] / vol_ma5_prev if vol_ma5_prev and not pd.isna(vol_ma5_prev) else np.nan
    if pd.isna(vol_ratio):
        vol_state = "数据不足"
    elif vol_ratio > 1.5:
        vol_state = "放量"
    elif vol_ratio < 0.7:
        vol_state = "缩量"
    else:
        vol_state = "平量"

    signal = _score(arr, macd_state, dif, dea, close.iloc[-1], ma20, rsi12, kdj_state)
    reversal = _reversal(kline, kd, md, rsi(close, 6), ma(close, 5), vol_ratio)
    ob_os = _overbought_oversold(k, j, rsi12, bias20)

    def _f(x, nd=2):
        return None if pd.isna(x) else round(float(x), nd)

    return {
        "n": n,
        "last": {"close": _f(close.iloc[-1]), "pct_chg": _f(kline["pct_chg"].iloc[-1])},
        "ma": {"ma5": _f(ma5), "ma10": _f(ma10), "ma20": _f(ma20), "ma60": _f(ma60), "排列": arr},
        "macd": {"dif": _f(dif, 3), "dea": _f(dea, 3), "macd": _f(bar, 3), "状态": macd_state},
        "kdj": {"k": _f(k), "d": _f(d), "j": _f(j), "状态": kdj_state},
        "rsi": {"rsi6": _f(rsi6), "rsi12": _f(rsi12), "rsi24": _f(rsi24)},
        "boll": _boll_state(bl),
        "bias": {"bias20": _f(bias20)},
        "vol": {"量比": _f(vol_ratio), "状态": vol_state},
        "signal": signal,
        "reversal": reversal,
        "ob_os": ob_os,
    }


def _score(arr, macd_state, dif, dea, last_close, ma20, rsi12, kdj_state) -> dict:
    """综合评级:各子信号规则打分求和(透明可测)。得分 -100~100 → 偏多/中性/偏空。"""
    score = 0
    reasons = []
    if arr == "多头排列":
        score += 30; reasons.append("均线多头+30")
    elif arr == "空头排列":
        score -= 30; reasons.append("均线空头-30")

    if macd_state == "金叉":
        score += 25; reasons.append("MACD金叉+25")
    elif macd_state == "死叉":
        score -= 25; reasons.append("MACD死叉-25")
    elif not pd.isna(dif) and not pd.isna(dea):
        if dif > dea:
            score += 10; reasons.append("DIF在DEA上+10")
        else:
            score -= 10; reasons.append("DIF在DEA下-10")

    if not pd.isna(ma20):
        if last_close > ma20:
            score += 10; reasons.append("价在MA20上+10")
        else:
            score -= 10; reasons.append("价在MA20下-10")

    if not pd.isna(rsi12):
        if rsi12 > 80:
            score -= 10; reasons.append("RSI过热-10")
        elif rsi12 > 55:
            score += 10; reasons.append("RSI偏强+10")
        elif rsi12 < 20:
            score += 10; reasons.append("RSI超跌反弹+10")
        elif rsi12 < 45:
            score -= 10; reasons.append("RSI偏弱-10")

    score = max(-100, min(100, score))
    rating = "偏多" if score >= 30 else ("偏空" if score <= -30 else "中性")
    return {"评级": rating, "得分": score, "依据": reasons}


def _overbought_oversold(k, j, rsi12, bias20) -> dict:
    """超买超卖判定(多指标共振;KDJ 单指标降权,防 J=3K−2D 假信号)。

    逐 KDJ/RSI12/BIAS20 判方向;共振数 ≥ 阈值(2)才下结论,否则中性。
    KDJ 命中记入共振但不单独定论;J<次级阈值仅作提示,不计入共振。
    """
    t = THRESHOLDS["超买超卖"]
    kt, rt, bt = t["KDJ"], t["RSI12"], t["BIAS20"]
    per = {}
    ob, os_ = [], []

    if not pd.isna(k) and not pd.isna(j):
        kdj_ob = k > kt["超买_K"] or j > kt["超买_J"]
        kdj_os = k < kt["超卖_K"] or j < kt["超卖_J"]
        per["kdj"] = "超买" if kdj_ob else ("超卖" if kdj_os else "-")
        if j < kt["次级超卖_J"] and not kdj_os:
            per["kdj"] += "(J濒临超卖提示)"
        ob.append(kdj_ob); os_.append(kdj_os)

    if not pd.isna(rsi12):
        rsi_ob, rsi_os = rsi12 > rt["超买"], rsi12 < rt["超卖"]
        per["rsi"] = "超买" if rsi_ob else ("超卖" if rsi_os else "-")
        ob.append(rsi_ob); os_.append(rsi_os)

    if not pd.isna(bias20):
        bias_ob, bias_os = bias20 > bt["超买"], bias20 < bt["超卖"]
        per["bias"] = f"{round(float(bias20), 2)}" + (
            "(超买)" if bias_ob else ("(超卖)" if bias_os else "(中性)"))
        if bias20 < bt["超卖极端"]:
            per["bias"] += "极端"
        ob.append(bias_ob); os_.append(bias_os)

    n_ob, n_os = sum(ob), sum(os_)
    need = t["共振数阈值"]
    if n_os >= need and n_os >= n_ob:
        verdict, resonance = "超卖", n_os
    elif n_ob >= need and n_ob > n_os:
        verdict, resonance = "超买", n_ob
    else:
        verdict, resonance = "中性", max(n_ob, n_os)
    return {"verdict": verdict, "resonance": int(resonance), "per_indicator": per}


def _bottom_divergence(close: pd.Series, hist: pd.Series) -> bool:
    """简化底背离:价创近低但 MACD 柱未创新低(近段 vs 前段波谷比较)。"""
    if len(close) < 30:
        return False
    recent, prior = close.iloc[-10:], close.iloc[-30:-10]
    ri, pi = recent.idxmin(), prior.idxmin()
    price_lower_low = close[ri] < close[pi]
    macd_higher_low = hist[ri] > hist[pi]
    return bool(price_lower_low and macd_higher_low)


def _reversal(kline: pd.DataFrame, kd: pd.DataFrame, md: pd.DataFrame,
              rsi6: pd.Series, ma5: pd.Series, vol_ratio) -> dict:
    """拐点信号:超跌反弹/启动。与趋势评级并列,独立打分(不并进趋势得分)。"""
    close, low, open_ = kline["close"], kline["low"], kline["open"]
    n = len(kline)
    k, d, j = kd.iloc[-1]["k"], kd.iloc[-1]["d"], kd.iloc[-1]["j"]
    pk, pd_ = (kd.iloc[-2]["k"], kd.iloc[-2]["d"]) if n >= 2 else (k, d)

    low20 = low.iloc[-20:].min() if n >= 1 else low.min()
    near_low = bool(low20 and (close.iloc[-1] - low20) / low20 <= 0.03)
    oversold = bool((not pd.isna(k) and k < 20) or (not pd.isna(j) and j < 0)
                    or (not pd.isna(rsi6.iloc[-1]) and rsi6.iloc[-1] < 20) or near_low)

    ma5_last = ma5.iloc[-1]
    up_today = (close.iloc[-1] > close.iloc[-2]) if n >= 2 else (close.iloc[-1] > open_.iloc[-1])
    vol_reclaim = bool(not pd.isna(vol_ratio) and vol_ratio > 1.5
                       and up_today
                       and not pd.isna(ma5_last) and close.iloc[-1] > ma5_last)

    low_golden = bool(not pd.isna(pk) and not pd.isna(k)
                      and pk <= pd_ and k > d and k < 50)

    divergence = _bottom_divergence(close, md["macd"])

    score = (25 * oversold + 30 * vol_reclaim + 25 * low_golden + 20 * divergence)
    reasons = []
    if oversold:
        reasons.append("超跌区(KDJ/RSI/近低点)+25")
    if vol_reclaim:
        reasons.append("放量反包站上MA5+30")
    if low_golden:
        reasons.append("KDJ低位金叉+25")
    if divergence:
        reasons.append("MACD底背离+20")
    label = "反弹启动" if score >= 50 else ("超跌待反弹" if score >= 25 else "无")
    return {"超跌": oversold, "放量反包": vol_reclaim, "低位金叉": low_golden,
            "底背离": divergence, "拐点评分": min(100, score), "拐点标签": label,
            "依据": reasons}
