"""统一打分层:预测记录表(live 或 replay)+ PriceBook → 逐 (记录 × horizon) 打分明细。

对每条预测记录定位 T+1 入场,算各 horizon 的 T+1 基准实现收益 + 双口径命中 + 隔夜跳空,
并回填 rank_score / stype / source 供上层聚合(方向型走命中/收益质量/超额;排序型走 rank-IC)。

产出的 scored 长表列:
  strategy_id, strategy, source, stype, pred_date, code, direction, rank_score, h,
  matured, r(T+1基准实现收益%), gap(隔夜跳空%), hit_end, hit_intra, entry_fallback
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import prices as _pr

SCORED_COLUMNS = [
    "strategy_id", "strategy", "source", "stype", "pred_date", "code",
    "direction", "rank_score", "h", "matured", "r", "gap",
    "hit_end", "hit_intra", "entry_fallback",
]


def score_predictions(preds: pd.DataFrame, book: _pr.PriceBook,
                      horizons=(1, 5)) -> pd.DataFrame:
    """对统一预测记录表逐条 × 每 horizon 打分(T+1 入场口径)。空表 → 空 scored 表。"""
    if preds is None or preds.empty:
        return pd.DataFrame(columns=SCORED_COLUMNS)
    rows = []
    for r in preds.itertuples(index=False):
        rec = book.get(r.code)
        idx = book.idx_of(r.code, r.pred_date)
        fr = _pr.realized(rec, idx, int(r.direction), horizons) if idx is not None else {
            h: {"matured": False, "r": None, "hit_end": None, "hit_intra": None,
                "gap": None, "entry_fallback": None} for h in horizons}
        for h in horizons:
            c = fr[h]
            rows.append({
                "strategy_id": r.strategy_id, "strategy": r.strategy,
                "source": r.source, "stype": r.stype, "pred_date": r.pred_date,
                "code": r.code, "direction": int(r.direction),
                "rank_score": getattr(r, "rank_score", np.nan), "h": h,
                "matured": bool(c["matured"]), "r": c["r"], "gap": c["gap"],
                "hit_end": c["hit_end"], "hit_intra": c["hit_intra"],
                "entry_fallback": c.get("entry_fallback"),
            })
    return pd.DataFrame(rows, columns=SCORED_COLUMNS)


def universe_returns(pred_dates, horizons, universe, book: _pr.PriceBook) -> dict:
    """全市场 T+1 基准实现收益池:{(date, h): np.array([各票 r%])}。用于超额均值 + 随机 bootstrap。

    防未来函数 + T+1 口径与策略侧**完全一致**:每票仅当能 T+1 入场且 idx+h<len 才计入。
    某 (date,h) 全市场都未到期 → 空数组(超额/基准不可算)。
    """
    out: dict = {(d, h): [] for d in pred_dates for h in horizons}
    dset = set(str(d)[:10] for d in pred_dates)
    for code in universe:
        rec = book.get(code)
        if rec is None:
            continue
        dmap = rec[4]
        for d in dset:
            idx = dmap.get(d)
            if idx is None:
                continue
            fr = _pr.realized(rec, idx, 1, horizons)   # 方向记 +1 只为拿 r(基准不关心命中)
            for h in horizons:
                if fr[h]["matured"]:
                    out[(d, h)].append(fr[h]["r"])
    return {k: np.asarray(v, float) for k, v in out.items()}
