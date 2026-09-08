"""午盘 Q · Q1/Q2/Q3 三条 screener + 派单器(策略层)。

方案见 docs/每日分析_午盘Q/Q1/Q2/Q3.md · 契约见 M2_契约稿.md §四。

三个策略主入口签名相同:
    screen_q{1,2,3}(date, as_of, *, universe=None, top_n=5, confirm_from=None, extras=None) → list[dict]

extras 是"外部预算好的辅助数据"字典(避免每个 screener 里重复读:
    · sector_ranks:      板块 → 日内涨幅排名 int(1=最强)
    · listing_days:       {code: 上市天数 int}
    · t1_klines:          {code: pd.DataFrame(T-1 及以前日线,含 close)}
    · am_quote:           str slot → {code: quote}(如 "1030" → 上午快照 dict);Q1 AmPmRatio 用
    · fundflow:           {code: pd.DataFrame} 分时资金流 5min;Q3 用
上层 pipeline 预取一次,三 screener 共用。

candidate universe 通过 quotes 层过基础卫生(not_st_not_new / liquidity_ok / 涨跌停排除)。
"""
from __future__ import annotations

import logging
from typing import Callable, Mapping

import pandas as pd

from tools.analysis.market_forecast.breadth import board_of
from tools.strategy import midday_q_signals as S

logger = logging.getLogger("strategy.midday_q_screener")


def _rank_score_from(pairs: dict[str, float | None], weights: dict[str, float]) -> float:
    """加权求和,None 值视为 0 但把该项权重扣除(避免缺数据的项目均摊放大其他项)。"""
    total = 0.0
    for k, w in weights.items():
        v = pairs.get(k)
        if v is None:
            continue
        total += w * v
    return total


def _base_universe(quotes: dict[str, Mapping],
                    extras: dict | None) -> list[str]:
    """基础卫生过滤:非 ST、非次新、流动性达标。

    ⚠️ 涨跌停附近的判定由各策略自己收紧(Q1 关注 NotLimitUp,Q2 关注 NotDownLimit),
    _base_universe 只保留三策略共有的排除项(ST/次新/流动性)。
    """
    listing_days_map = (extras or {}).get("listing_days") or {}
    out: list[str] = []
    for code, q in quotes.items():
        if not S.not_st_not_new(q, listing_days=listing_days_map.get(code)):
            continue
        if not S.liquidity_ok(q):
            continue
        out.append(code)
    return out


def _apply_universe_scope(
    quotes: dict[str, Mapping],
    universe: list[str] | None,
    confirm_from: list[str] | None,
) -> dict[str, Mapping]:
    """筛出待跑的报价子集:优先 confirm_from(复核) > universe(限定池) > 全量。

    ⚠️ universe=[] (空 list) 视为"明确限定为空池"→ 返回空 dict;
       universe=None → 无限定,取全量 quotes。
    """
    if confirm_from is not None:
        return {c: quotes[c] for c in confirm_from if c in quotes}
    if universe is not None:
        return {c: quotes[c] for c in universe if c in quotes}
    return dict(quotes)


# ────────────────────────────── Q1 强势接力 ──────────────────────────────

def screen_q1(
    date: str,
    as_of: str,
    quotes: dict[str, Mapping],
    *,
    universe: list[str] | None = None,
    top_n: int = 5,
    confirm_from: list[str] | None = None,
    extras: dict | None = None,
) -> list[dict]:
    """Q1 强势接力 screener(方案 Q1_强势接力.md §信号)。

    过滤:
        IntradayReturn ∈ [0.03, 0.06]  (温和强势带)
        AmPmRatio       ≥ 0.8           (下午量能不衰减,vol_ratio 或 pm/am)
        DistanceToDayHigh ≤ 0.015       (距日高 <1.5%)
        MA5>MA10>MA20 且 open > MA5      (短均线多头)
        SectorRank                       (板块日内前 5,数据缺失时跳过此项)
        NotLimitUp                       (排除接近涨停)
    """
    extras = extras or {}
    scope = _apply_universe_scope(quotes, universe or _base_universe(quotes, extras), confirm_from)

    am_quote = None
    if isinstance(extras.get("am_quote"), dict):
        am_quote = extras["am_quote"].get("1030") or next(iter(extras["am_quote"].values()), None)

    sector_of = extras.get("sector_of") or {}          # {code: 申万一级}
    sector_ranks = extras.get("sector_ranks") or {}
    t1_klines = extras.get("t1_klines") or {}

    hits: list[dict] = []
    for code, q in scope.items():
        ir = S.intraday_return(q)
        if ir is None or not (0.03 <= ir <= 0.06):
            continue
        am_pm = S.am_pm_vol_ratio(q, am_quote.get(code) if am_quote else None)
        if am_pm is None or am_pm < 0.8:
            continue
        dh = S.distance_to_day_high(q)
        if dh is None or dh > 0.015:
            continue
        if not S.ma_stacked_bullish(t1_klines.get(code), S._f(q.get("open"))):
            continue
        if not S.not_limit_up(q, code):
            continue
        sector = sector_of.get(code)
        sector_hit = S.sector_rank_in_top(sector, sector_ranks, top_n=5)
        # SectorRank 缺板块数据时不当强制条件,只影响 rank

        rank_pairs = {
            "intraday_return": ir,
            "am_pm_ratio": am_pm,
            "distance_to_high": 1.0 - dh,
            "sector_rank": 1.0 if sector_hit else 0.0,
        }
        hits.append({
            "code": code,
            "name": q.get("name"),
            "rank_score": _rank_score_from(rank_pairs, S.Q1_RANK_WEIGHTS),
            "signals": {
                "IntradayReturn": round(ir, 4),
                "AmPmRatio": round(am_pm, 4),
                "DistanceToDayHigh": round(dh, 4),
                "SectorHit": sector_hit,
                "sector": sector,
            },
        })

    hits.sort(key=lambda x: x["rank_score"], reverse=True)
    return hits[:top_n]


# ────────────────────────────── Q2 弱转强反抽 ──────────────────────────────

def screen_q2(
    date: str,
    as_of: str,
    quotes: dict[str, Mapping],
    *,
    universe: list[str] | None = None,
    top_n: int = 5,
    confirm_from: list[str] | None = None,
    extras: dict | None = None,
) -> list[dict]:
    """Q2 弱转强反抽 screener(方案 Q2_弱转强反抽.md §信号)。

    过滤:
        IntradayLow            ≤ -0.03   (早盘急跌 ≥3%)
        Rebound                ≥ 0.02    (从最低点反弹 ≥2%)
        CurrentReturn          ∈ [-0.01, 0.02]   (修复到平盘附近)
        AfternoonVolExpand      ≥ 1.2   (下午放量,用 am_pm_vol_ratio 近似)
        NotLongTermDowntrend           (open > MA60×0.9)
        NotDownLimit                    (排除跌停附近)

    ⚠️ LowTimeInMorning(argmin 在早盘)与 AfternoonVolExpand 精确口径需要分时序列,
       快照层无法精确判定;这里用 IntradayLow 存在(low 明显低于 open)作为"早跌"代理,
       AfternoonVolExpand 用 am_pm_vol_ratio 近似。M3.b 引入分时序列后可收紧。
    """
    extras = extras or {}
    scope = _apply_universe_scope(quotes, universe or _base_universe(quotes, extras), confirm_from)

    am_quote = None
    if isinstance(extras.get("am_quote"), dict):
        am_quote = extras["am_quote"].get("1030") or next(iter(extras["am_quote"].values()), None)

    t1_klines = extras.get("t1_klines") or {}

    hits: list[dict] = []
    for code, q in scope.items():
        il = S.intraday_low_return(q)
        if il is None or il > -0.03:
            continue
        rb = S.rebound_from_low(q)
        if rb is None or rb < 0.02:
            continue
        cr = S.intraday_return(q)
        if cr is None or not (-0.01 <= cr <= 0.02):
            continue
        vol_ratio = S.am_pm_vol_ratio(q, am_quote.get(code) if am_quote else None)
        if vol_ratio is None or vol_ratio < 1.2:
            continue
        if not S.not_long_term_downtrend(t1_klines.get(code), S._f(q.get("open"))):
            continue
        if not S.not_down_limit(q, code):
            continue

        rank_pairs = {
            "rebound": rb,
            "afternoon_vol_expand": vol_ratio,
            "intraday_low_abs": abs(il),
            "current_return_inverse": -cr,        # 越低(接近平盘)得分越高
        }
        hits.append({
            "code": code,
            "name": q.get("name"),
            "rank_score": _rank_score_from(rank_pairs, S.Q2_RANK_WEIGHTS),
            "signals": {
                "IntradayLow": round(il, 4),
                "Rebound": round(rb, 4),
                "CurrentReturn": round(cr, 4),
                "VolRatio": round(vol_ratio, 4),
            },
        })

    hits.sort(key=lambda x: x["rank_score"], reverse=True)
    return hits[:top_n]


# ────────────────────────────── Q3 资金流前瞻 ──────────────────────────────

def screen_q3(
    date: str,
    as_of: str,
    quotes: dict[str, Mapping],
    *,
    universe: list[str] | None = None,
    top_n: int = 5,
    confirm_from: list[str] | None = None,
    extras: dict | None = None,
) -> list[dict]:
    """Q3 资金流前瞻 screener(方案 Q3_资金流前瞻.md §信号)。

    过滤:
        MainNetPM(下午主力净流入求和)  ≥ 0     (下午净流入不为负)
        LargeOrderPct                    ≥ 0.30 (大单占比 ≥30%)
        IntradayReturn                    ∈ [-0.02, 0.05]  (温和区间)
        Liquidity 加强:成交额 ≥ 8000 万(Q3 大单需要更高流动性)

    fundflow_intraday 数据由上层 pipeline 通过 extras['fundflow']={code: df} 传入。
    数据缺失 → 该票不入选。
    """
    extras = extras or {}
    scope = _apply_universe_scope(quotes, universe or _base_universe(quotes, extras), confirm_from)
    ff_map: dict[str, pd.DataFrame] = extras.get("fundflow") or {}

    hits: list[dict] = []
    for code, q in scope.items():
        # Q3 加强的流动性下限
        if not S.liquidity_ok(q, min_amount_wan=8000.0):
            continue

        cr = S.intraday_return(q)
        if cr is None or not (-0.02 <= cr <= 0.05):
            continue

        ffdf = ff_map.get(code)
        if ffdf is None or ffdf.empty:
            continue

        mnp = S.main_net_since(ffdf, "13:00")
        if mnp is None or mnp < 0:
            continue

        lop = S.large_order_pct(ffdf)
        if lop is None or lop < 0.30:
            continue

        if not S.not_limit_up(q, code):
            continue

        rank_pairs = {
            "main_net_pm": mnp / 1e8,             # 归一到亿,防不同市值淹没
            "large_order_pct": lop,
            "northbound_net": 0.0,                # G6 M1 跳过,权重降 0
            "lhb_proxy": 0.0,                     # M2 阶段不做 LHB 前瞻
        }
        hits.append({
            "code": code,
            "name": q.get("name"),
            "rank_score": _rank_score_from(rank_pairs, S.Q3_RANK_WEIGHTS),
            "signals": {
                "MainNetPM_yi": round(mnp / 1e8, 4),
                "LargeOrderPct": round(lop, 4),
                "CurrentReturn": round(cr, 4),
            },
        })

    hits.sort(key=lambda x: x["rank_score"], reverse=True)
    return hits[:top_n]


# ────────────────────────────── 派单器 ──────────────────────────────

_SCREENER_MAP: dict[str, Callable] = {
    "Q1": screen_q1,
    "Q2": screen_q2,
    "Q3": screen_q3,
}


def dispatch(
    date: str,
    as_of: str,
    quotes: dict[str, Mapping],
    gate_final: dict,
    *,
    top_n_per_strategy: int = 5,
    confirm_from: dict[str, list[str]] | None = None,
    extras: dict | None = None,
) -> dict:
    """按 gate.allowed_strategies 触发对应 screener,合并结果。

    gate_final 结构:见 gate.compute_gate 返回,至少含 allowed_strategies / position_pct / state。

    确保未来函数:如果 gate.allowed_strategies 空(未知/弱势/崩盘/flipped)→ 空清单。
    """
    allowed = list(gate_final.get("allowed_strategies") or [])
    result: dict = {
        "date": date,
        "as_of": as_of,
        "gate_state": gate_final.get("state"),
        "position_pct": float(gate_final.get("position_pct", 0.0)),
        "selections": {},
        "final_codes": [],
    }
    if not allowed:
        return result

    all_hits: dict[str, dict] = {}   # code → 最好那一次的 hit(取 rank_score 高的)
    for strategy in allowed:
        screener = _SCREENER_MAP.get(strategy)
        if screener is None:
            logger.warning("未知策略 %s,跳过", strategy)
            continue
        confirm = (confirm_from or {}).get(strategy)
        hits = screener(date, as_of, quotes,
                         top_n=top_n_per_strategy,
                         confirm_from=confirm,
                         extras=extras)
        result["selections"][strategy] = hits
        for h in hits:
            code = h["code"]
            prev = all_hits.get(code)
            if prev is None or h["rank_score"] > prev["rank_score"]:
                all_hits[code] = {**h, "from_strategy": strategy}

    result["final_codes"] = sorted(
        all_hits.values(), key=lambda x: x["rank_score"], reverse=True
    )
    return result
