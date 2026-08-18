"""VCP 波段轮次:高点→低点,只比当前轮 vs 上一轮。

一轮 = 确认(或进行中)的波段高点回撤到波段低点,持续 ≥3 交易日。
收缩硬条件:当前轮回撤% < 上一轮 且 当前轮低点 > 上一轮低点。
时间压缩 / 量缩只标记。无末轮绝对阈值。

洗盘:当日 low 刺破前轮低点但 close 收回。
失效:close 较前轮低点跌破 >1.5% 且收不回。日 K 即可,不需分钟线。

⚠️ 非投资建议。pivot 窗口为工程占位。
"""
from __future__ import annotations

import math

import pandas as pd

from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["SEPA_VCP"]


def _dates(kline: pd.DataFrame) -> list[str]:
    return [str(d)[:10] for d in kline["date"]]


def _confirmed_pivots(high, low, t: int, n: int) -> list[tuple[int, str, float]]:
    """左右各 n 根确认的高低点,索引 ≤ t-n。同根既高又低则跳过。"""
    out: list[tuple[int, str, float]] = []
    last = t - n
    if last < n:
        return out
    for i in range(n, last + 1):
        seg_h = high[i - n: i + n + 1]
        seg_l = low[i - n: i + n + 1]
        is_h = high[i] == max(seg_h)
        is_l = low[i] == min(seg_l)
        if is_h and is_l:
            continue
        if is_h:
            out.append((i, "H", float(high[i])))
        elif is_l:
            out.append((i, "L", float(low[i])))
    return out


def _alternate(pivots: list[tuple[int, str, float]]) -> list[tuple[int, str, float]]:
    """连续同向保留更极端者(高取更高、低取更低)。"""
    alt: list[tuple[int, str, float]] = []
    for p in pivots:
        if not alt:
            alt.append(p)
            continue
        i, kind, px = p
        li, lk, lp = alt[-1]
        if kind == lk:
            if kind == "H" and px >= lp:
                alt[-1] = (i, kind, px)
            elif kind == "L" and px <= lp:
                alt[-1] = (i, kind, px)
        else:
            alt.append(p)
    return alt


def _round(kline, hi_i: int, lo_i: int, ongoing: bool) -> dict | None:
    min_d = int(_CFG["最少轮天数"])
    days = lo_i - hi_i + 1
    if days < min_d:
        return None
    high = float(kline["high"].iloc[hi_i])
    # 轮内最低用区间真实 low(进行中=迄今最低),不只用确认 pivot 价
    low = float(kline["low"].iloc[hi_i: lo_i + 1].min())
    if high <= 0:
        return None
    retr = (high - low) / high * 100.0
    vol = kline["volume"].iloc[hi_i: lo_i + 1]
    avg_vol = float(vol.mean()) if len(vol) else float("nan")
    ds = _dates(kline)
    return {
        "start_date": ds[hi_i], "end_date": ds[lo_i],
        "high_idx": hi_i, "low_idx": lo_i,
        "high": round(high, 4), "low": round(low, 4),
        "回撤%": round(retr, 2), "天数": int(days),
        "段均量": None if math.isnan(avg_vol) else round(avg_vol, 1),
        "进行中": bool(ongoing),
    }


def segment_rounds(kline: pd.DataFrame, t: int | None = None, cfg: dict | None = None) -> list[dict]:
    """切出高→低轮次。末段可以是进行中(最后高点 → 迄今最低)。"""
    c = cfg or _CFG
    n = len(kline)
    if n == 0:
        return []
    if t is None:
        t = n - 1
    win = int(c["pivot窗口"])
    high = kline["high"].to_numpy(dtype=float)
    low = kline["low"].to_numpy(dtype=float)
    piv = _alternate(_confirmed_pivots(high, low, t, win))

    rounds: list[dict] = []
    i = 0
    while i < len(piv) - 1:
        if piv[i][1] == "H" and piv[i + 1][1] == "L":
            r = _round(kline, piv[i][0], piv[i + 1][0], False)
            if r:
                rounds.append(r)
            i += 2
        else:
            i += 1

    # 进行中:最后确认高点之后尚未形成确认低点,或确认低点之后又走出未确认的新高回撤
    last_h = None
    for p in reversed(piv):
        if p[1] == "H":
            last_h = p[0]
            break
    if last_h is not None and last_h < t:
        already = rounds and rounds[-1]["high_idx"] == last_h and not rounds[-1]["进行中"]
        if not already:
            r = _round(kline, last_h, t, True)
            if r:
                # 若已有以同一高点收尾的完成轮,用进行中覆盖(低点可能更深)
                if rounds and rounds[-1]["high_idx"] == last_h:
                    rounds[-1] = r
                else:
                    rounds.append(r)
    return rounds


def contraction(curr: dict, prev: dict) -> dict:
    """只比当前轮 vs 上一轮。硬收缩 = 振幅↓ + higher low。"""
    amp = curr["回撤%"] < prev["回撤%"]
    hl = curr["low"] > prev["low"]
    time_ok = curr["天数"] <= prev["天数"]
    cv, pv = curr.get("段均量"), prev.get("段均量")
    vol_ok = cv is not None and pv is not None and cv < pv
    return {
        "振幅缩小": bool(amp), "higher_low": bool(hl),
        "时间压缩": bool(time_ok), "量缩": bool(vol_ok),
        "硬收缩": bool(amp and hl),
    }


def structure_status(kline: pd.DataFrame, t: int, prev_low: float,
                     fail_pct: float | None = None) -> dict:
    """日 K:刺破收回=洗盘;收盘跌破 fail_pct 且收不回=失效。"""
    pct = (fail_pct if fail_pct is not None else float(_CFG["失效跌破%"])) / 100.0
    close = float(kline["close"].iloc[t])
    low = float(kline["low"].iloc[t])
    fail = close < prev_low * (1.0 - pct)
    wash = (low < prev_low) and (close >= prev_low) and not fail
    return {"失效": bool(fail), "洗盘刺破": bool(wash),
            "close": round(close, 4), "prev_low": round(float(prev_low), 4)}


def _vol_ma50(kline: pd.DataFrame, t: int) -> float | None:
    if t + 1 < 50:
        return None
    v = kline["volume"].iloc[t - 49: t + 1].mean()
    return None if math.isnan(float(v)) else float(v)


def analyze_vcp(kline: pd.DataFrame, t: int | None = None, cfg: dict | None = None) -> dict:
    """单票 VCP 画像:轮次、末对收缩、洗盘/失效、枢纽占位。"""
    c = cfg or _CFG
    n = len(kline)
    if n == 0:
        return {"轮次": [], "进行中": False}
    if t is None:
        t = n - 1
    rounds = segment_rounds(kline, t, c)
    v50 = _vol_ma50(kline, t)
    for r in rounds:
        r["五十日均量"] = None if v50 is None else round(v50, 1)
        r["量枯"] = bool(r["段均量"] is not None and v50 is not None and r["段均量"] < v50)

    last = rounds[-1] if rounds else None
    prev = rounds[-2] if len(rounds) >= 2 else None
    pair = contraction(last, prev) if last and prev else None
    broken = wash = False
    if prev is not None:
        st = structure_status(kline, t, prev["low"], float(c["失效跌破%"]))
        broken, wash = st["失效"], st["洗盘刺破"]
    elif last is not None and len(rounds) >= 1:
        # 仅一轮时用该轮低点作参照(破自身低点)
        st = structure_status(kline, t, last["low"], float(c["失效跌破%"]))
        broken, wash = st["失效"], st["洗盘刺破"]

    near = False
    dry = False
    ongoing = bool(last and last["进行中"])
    if last:
        px = float(kline["close"].iloc[t])
        near = last["high"] > 0 and (last["high"] - px) / last["high"] * 100.0 <= float(c["距前高近%"])
        dry = bool(last.get("量枯"))
        last["距前高%"] = round((last["high"] - px) / last["high"] * 100.0, 2) if last["high"] else None

    return {
        "轮次": rounds, "轮数": len(rounds),
        "进行中": ongoing,
        "末对收缩": pair,
        "VCP进行中": bool(pair and pair["硬收缩"] and not broken),
        "结构更健康": bool(pair and pair["硬收缩"] and len(rounds) >= 3 and not broken),
        "接近枢纽": bool(ongoing and dry and near and not broken),
        "结构破坏": bool(broken),
        "洗盘刺破": bool(wash),
        "回撤链": [r["回撤%"] for r in rounds],
    }


def build_chart_payload(kline: pd.DataFrame, vcp: dict, limit: int | None = None) -> dict:
    """收缩结构参考图数据。标题固定,不含「VCP 完成」。"""
    lim = int(limit or _CFG["图表根数"])
    df = kline.tail(lim).reset_index(drop=True)
    keep_from = max(0, len(kline) - lim)
    rounds = []
    for r in vcp.get("轮次") or []:
        if r["low_idx"] < keep_from and r["high_idx"] < keep_from:
            continue
        rounds.append({
            "start_date": r["start_date"], "end_date": r["end_date"],
            "high": r["high"], "low": r["low"],
            "回撤%": r["回撤%"], "天数": r["天数"],
            "段均量": r["段均量"], "五十日均量": r.get("五十日均量"),
            "进行中": r["进行中"],
        })

    def col(name):
        return [None if pd.isna(v) else round(float(v), 2) for v in df[name]]

    close = df["close"]
    return {
        "title": "收缩结构参考",
        "dates": _dates(df),
        "open": col("open"), "high": col("high"), "low": col("low"),
        "close": col("close"),
        "volume": [None if pd.isna(v) else float(v) for v in df["volume"]],
        "ma50": [None if pd.isna(v) else round(float(v), 2)
                 for v in close.rolling(50).mean()],
        "ma150": [None if pd.isna(v) else round(float(v), 2)
                  for v in close.rolling(150).mean()],
        "ma200": [None if pd.isna(v) else round(float(v), 2)
                  for v in close.rolling(200).mean()],
        "rounds": rounds,
    }
