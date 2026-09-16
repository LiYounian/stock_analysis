"""全A 按申万一级聚合的**单次加载器**——一次 load 同喂"板块面板"与"角色识别"。

数据单一真源:`tools.store.repo`(= data/master/kline 全A主档,与 market_breadth 同源)。
行业归属:`code_industry.load()` → `industry_map.to_sw`(申万一级,单一真源)。
涨停判定:`breadth.is_limit_hit`(与历史广度/大盘口径同一启发式,不二次实现)。

**防未来函数**:只取 ≤date 的 K线行;`date` 那天无行(停牌/未上市)→ 该票不进当日截面。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from tools.analysis import industry_map
from tools.analysis.market_forecast import breadth as B

logger = logging.getLogger("sector_forecast.universe")

FRAME_COLS = ["code", "sw", "board", "close", "high", "low",
              "amount", "turnover", "pct_chg", "limit_up"]


def _membership() -> dict[str, str]:
    """{code: 申万一级}。无归属/映射不到的票丢弃(不猜行业)。"""
    from tools.collectors import code_industry
    snap = code_industry.load()
    out = {}
    for c, raw in snap.items():
        sw = industry_map.to_sw(raw) if raw else None
        if sw:
            out[c] = sw
    return out


def load_sector_frame(date: str, *, codes: Optional[list[str]] = None) -> pd.DataFrame:
    """全A 在 `date` 的单日截面(按申万一级打标)。列见 FRAME_COLS。

    一次加载,同时供:①板块广度聚合(sector_breadth) ②角色识别当日量(龙头涨幅/中军成交)。
    历史补跑安全:只读本地 master kline 的 `date` 行,绝不用今天实时价冒充历史。
    """
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()
    from tools.store import repo as store

    mem = _membership()
    codes = codes if codes is not None else store.list_master_codes()
    rows = []
    miss = 0
    for c in codes:
        sw = mem.get(c)
        if not sw:                       # 无申万一级归属 → 不进截面
            continue
        try:
            df = store.get_master_kline(c)
        except Exception:
            miss += 1
            continue
        d = df[df["date"].astype(str).str.slice(0, 10) == date]
        if d.empty:
            continue
        r = d.iloc[-1]
        pct = _num(r.get("pct_chg"))
        close, high, low = _num(r.get("close")), _num(r.get("high")), _num(r.get("low"))
        lu = B.is_limit_hit(c, pct, close, high, low, up=True) if pct is not None else False
        rows.append({
            "code": c, "sw": sw, "board": B.board_of(c),
            "close": close, "high": high, "low": low,
            "amount": _num(r.get("amount")), "turnover": _num(r.get("turnover")),
            "pct_chg": pct, "limit_up": bool(lu),
        })
    frame = pd.DataFrame(rows, columns=FRAME_COLS)
    logger.info("load_sector_frame %s: %d 只入截面(有申万归属+当日有K线),读失败 %d",
                date, len(frame), miss)
    return frame


def sector_breadth(frame: pd.DataFrame) -> pd.DataFrame:
    """按申万一级聚合广度:上涨家数占比 / 涨停数 / 板块成交额 / 板块成交占比 / 家数。

    上涨口径:pct_chg > 0(平盘不计涨,与 cross_section_stats 一致)。
    """
    if frame.empty:
        return pd.DataFrame(columns=["sw", "n", "上涨家数占比", "涨停数",
                                     "板块成交额", "板块成交占比", "板块均涨幅"])
    total_amount = float(frame["amount"].fillna(0).sum()) or np.nan
    out = []
    for sw, g in frame.groupby("sw"):
        n = len(g)
        adv = int((g["pct_chg"] > 0).sum())
        amt = float(g["amount"].fillna(0).sum())
        out.append({
            "sw": sw, "n": n,
            "上涨家数占比": round(adv / n, 4) if n else None,
            "涨停数": int(g["limit_up"].sum()),
            "板块成交额": amt,
            "板块成交占比": round(amt / total_amount, 6) if total_amount and not np.isnan(total_amount) else None,
            "板块均涨幅": round(float(g["pct_chg"].dropna().mean()), 4) if g["pct_chg"].notna().any() else None,
        })
    return pd.DataFrame(out).sort_values("板块成交额", ascending=False).reset_index(drop=True)


def _num(v):
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except Exception:
        return None
