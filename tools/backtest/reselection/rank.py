"""持续型排名视图:把逐票逐日 score 汇成每个决策日 D 的横截面排名(TopN)。

排名只用 ≤D 的 score(防未来:score 本身仅用 ≤D 数据算)。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.reselection.rank")


def build_daily_ranks(feats: dict[str, dict], score_key: str = "mom",
                      cap: int = 60, min_cross: int = 30,
                      min_liq: float = 0.0) -> dict[str, list[str]]:
    """→ {决策日 D: [按 score 降序的 code, 最多 cap 只]}。

    cap 只需 ≥ 最大 TopN(默认 60 足够 N≤20 的续选判定)。剔除横截面票数 < min_cross 的日。
    min_liq>0:按当日近20日均成交额(元,as-of)剔除不可交易的低流动性票(防微盘幻觉)。
    """
    rows_date: list = []
    rows_code: list = []
    rows_score: list = []
    for code, f in feats.items():
        s = f.get(score_key)
        if s is None:
            continue
        dates = f["dates"]
        ok = np.isfinite(s)
        if min_liq > 0:
            liq = f.get("liq")
            if liq is None:
                continue
            ok = ok & (np.nan_to_num(liq, nan=-1.0) >= min_liq)
        idx = np.nonzero(ok)[0]
        if len(idx) == 0:
            continue
        rows_date.append(dates[idx])
        rows_code.append(np.full(len(idx), code))
        rows_score.append(s[idx])
    if not rows_date:
        return {}
    panel = pd.DataFrame({
        "date": np.concatenate(rows_date),
        "code": np.concatenate(rows_code),
        "score": np.concatenate(rows_score),
    })
    ranks: dict[str, list[str]] = {}
    for d, g in panel.groupby("date"):
        if len(g) < min_cross:
            continue
        top = g.sort_values("score", ascending=False)["code"].head(cap).tolist()
        ranks[str(d)] = top
    logger.info("排名视图:%d 个决策日(cap=%d, min_cross=%d)", len(ranks), cap, min_cross)
    return ranks
