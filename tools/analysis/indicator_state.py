"""四指标 + KDJ 离散状态向量(计划文档1 F2 / F2b)。

纯函数、只用 ≤t 数据(传入的 kline 即截至当日的切片)→ 防未来函数。分两层:

  主维度(3 个,F3 的 A2 相似度量只用这 3 个,防维度爆炸):
     趋势方向(MA排列) × 动量状态(MACD+RSI) × BOLL位置(%B分档)

  二级维度(F2b,**不进主相似度量**,作条件过滤 / 断点佐证(文档2) / 展示):
     KDJ(超买超卖 / 钝化 / 交叉) · RSI背离 · MACD背离 · BOLL形态(开口/缩口/沿轨)
     背离仅作"警告/佐证",不单独定买卖(背离可长期持续)。

口径参数真源:THRESHOLDS['指标状态'] 与 THRESHOLDS['BOLL']。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis import technical as ta
from tools.config.strategy import THRESHOLDS


# ---------- 主维度 ----------
def _momentum(tech: dict, cfg: dict) -> str:
    """动量状态:强/中/弱。MACD 多头/金叉 且 RSI12≥强 → 强;MACD 空头/死叉 且 RSI12≤弱 → 弱。"""
    macd_state = (tech.get("macd") or {}).get("状态")
    rsi12 = (tech.get("rsi") or {}).get("rsi12")
    bull = macd_state in ("金叉", "多头")
    bear = macd_state in ("死叉", "空头")
    r_strong = rsi12 is not None and rsi12 >= cfg["动量RSI强"]
    r_weak = rsi12 is not None and rsi12 <= cfg["动量RSI弱"]
    if bull and r_strong:
        return "强"
    if bear and r_weak:
        return "弱"
    return "中"


# ---------- 二级维度(F2b)----------
def _divergence(price: pd.Series, ind: pd.Series, lookback: int, recent: int) -> str:
    """通用背离:近段(recent)相对前段(lookback-recent)的波峰/谷 vs 指标。

    底背离:价创新低(近段最低 < 前段最低)但指标未创新低(指标抬高)。
    顶背离:价创新高但指标未创新高。价指标同索引对齐;数据不足返回'无'。
    """
    price = price.dropna()
    ind = ind.reindex(price.index)
    if len(price) < lookback or ind.isna().all():
        return "无"
    r = price.iloc[-recent:]
    p = price.iloc[-lookback:-recent]
    if len(p) == 0 or len(r) == 0:
        return "无"
    ri_lo, pi_lo = r.idxmin(), p.idxmin()
    if price[ri_lo] < price[pi_lo] and not pd.isna(ind[ri_lo]) and not pd.isna(ind[pi_lo]) \
            and ind[ri_lo] > ind[pi_lo]:
        return "底背离"
    ri_hi, pi_hi = r.idxmax(), p.idxmax()
    if price[ri_hi] > price[pi_hi] and not pd.isna(ind[ri_hi]) and not pd.isna(ind[pi_hi]) \
            and ind[ri_hi] < ind[pi_hi]:
        return "顶背离"
    return "无"


def _kdj_dullness(k: pd.Series, n: int) -> str:
    """KDJ 钝化:K 连续 n 日 >80(高位钝化,强趋势超买失效)或 <20(低位钝化)。"""
    tail = k.dropna().iloc[-n:]
    if len(tail) < n:
        return "无"
    if (tail > 80).all():
        return "高位钝化"
    if (tail < 20).all():
        return "低位钝化"
    return "无"


def _kdj_cross(k: pd.Series, d: pd.Series, recent_bars: int) -> dict:
    """KDJ 交叉:最近一次金叉/死叉 + 位置(低位/高位)+ 交叉点逐波抬高/降低 + 距今。

    低位金叉(K<50)较可靠;高位死叉是顶部警报;交叉点抬高=动能增强,降低=衰减。
    """
    k = k.dropna()
    d = d.reindex(k.index)
    if len(k) < 2:
        return {"最近交叉": "无", "位置": None, "交叉点趋势": None, "距今": None, "新近": False}
    sign = np.sign((k - d).to_numpy())
    crosses = []  # (下标, 类型, 该处K值)
    for i in range(1, len(sign)):
        if sign[i - 1] <= 0 and sign[i] > 0:
            crosses.append((i, "金叉", float(k.iloc[i])))
        elif sign[i - 1] >= 0 and sign[i] < 0:
            crosses.append((i, "死叉", float(k.iloc[i])))
    if not crosses:
        return {"最近交叉": "无", "位置": None, "交叉点趋势": None, "距今": None, "新近": False}
    last_i, last_type, last_kv = crosses[-1]
    bars_ago = len(k) - 1 - last_i
    same = [c for c in crosses if c[1] == last_type]
    趋势 = None
    if len(same) >= 2:
        趋势 = ("抬高" if same[-1][2] > same[-2][2]
                else ("降低" if same[-1][2] < same[-2][2] else "持平"))
    return {"最近交叉": last_type, "位置": ("低位" if last_kv < 50 else "高位"),
            "交叉点趋势": 趋势, "距今": int(bars_ago), "新近": bool(bars_ago <= recent_bars)}


def _boll_form(bl: pd.DataFrame, squeeze: bool) -> str:
    """BOLL 形态:沿上轨/沿下轨(riding)> 缩口(挤压)> 开口(带宽扩张)> 常态。"""
    t = THRESHOLDS["BOLL"]
    bw = bl["bandwidth"].dropna()
    pb = bl["percent_b"].dropna()
    n = t["沿轨连续天数"]
    if len(pb) >= n:
        tail = pb.iloc[-n:]
        if (tail > t["触轨上_percentB"]).all():
            return "沿上轨"
        if (tail < t["触轨下_percentB"]).all():
            return "沿下轨"
    if squeeze:
        return "缩口"
    look = t["开口回看"]
    if len(bw) >= look + 1:
        cur, prev = bw.iloc[-1], bw.iloc[-1 - look]
        if prev and cur >= prev * t["开口扩张比"]:
            return "开口"
    return "常态"


# ---------- 组装 ----------
def state_vector(kline: pd.DataFrame, tech: dict) -> dict:
    """四指标+KDJ 离散状态向量。kline=截至当日切片(≤t),tech=technical.compute(kline) 输出。"""
    cfg = THRESHOLDS["指标状态"]
    close = kline["close"]
    kd = ta.kdj(kline)
    md = ta.macd(close)
    bl = ta.boll(close)
    rsi12 = ta.rsi(close, 12)
    squeeze = bool((tech.get("boll") or {}).get("挤压", False))

    return {
        "主维度": {
            "趋势方向": (tech.get("ma") or {}).get("排列", "数据不足"),
            "动量状态": _momentum(tech, cfg),
            "BOLL位置": (tech.get("boll") or {}).get("位置", "数据不足"),
        },
        "二级维度": {   # F2b,不进主相似度量
            "KDJ": {
                "超买超卖": (tech.get("kdj") or {}).get("状态", "-"),
                "钝化": _kdj_dullness(kd["k"], cfg["KDJ钝化连续天数"]),
                "交叉": _kdj_cross(kd["k"], kd["d"], cfg["KDJ交叉新近_bars"]),
            },
            "RSI背离": _divergence(close, rsi12, cfg["背离回看"], cfg["背离近段"]),
            "MACD背离": _divergence(close, md["macd"], cfg["背离回看"], cfg["背离近段"]),
            "BOLL形态": _boll_form(bl, squeeze),
        },
    }


def primary_key(sv: dict) -> tuple:
    """抽 A2 主相似度量用的 3 元组(趋势方向, 动量状态, BOLL位置)。二级维度不入。"""
    m = sv["主维度"]
    return (m["趋势方向"], m["动量状态"], m["BOLL位置"])
