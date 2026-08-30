"""融合基线在『带离场』口径下翻不翻案的探针(eval_v3.1 附录)。

Phase A 结论(docs/策略/技术合议融合基线_回测结论.md):融合分 rank-IC 最强(F2 +0.065),但
Top-N **扣成本后**净超额被交易成本淹没 → 未达标。本探针把融合 Top-N 每日选票喂进离场模拟器,
看『带离场可实现净收益』能否翻案。

方法:fusion_panel(76017 行 / 1618 交易日,as-of 无未来函数)→ 逐日横截面 zscore 复合融合分
(F0 核心正 / F2 核心正+结构态)→ 每日按分降序取 Top5/Top10 → 构造 preds(rank_score=融合分)→
复用 exit_sim/aggregate_exit 三口径。买入持有全市场基准=同 horizon 全市场等权(与主报告一致)。

用法:python -m tools.backtest.eval_v3.exit_fusion_probe [--panel PATH] [--cost 0.2]
"""
from __future__ import annotations

import argparse
import logging

import numpy as np
import pandas as pd

from tools.backtest import fusion_lab as FL
from tools.config import settings

from . import aggregate_exit as ae
from . import exit_sim as ex
from . import prices, replay_source, schema, scoring

logger = logging.getLogger("backtest.eval_v3.exit_fusion_probe")

_PANEL = str(settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "fusion_panel.parquet")
_CONFIGS = ("F0_核心正(os+rev)", "F2_核心正+结构态")


def build_preds(pdf, cfg_name, topn):
    """逐日融合分 Top-N 选票 → 统一预测记录表(rank_score=融合分)。"""
    FL.compute_fusion(pdf, FL.CONFIGS[cfg_name], weights=None, score_col="fuse")
    recs = []
    for d, g in pdf.groupby("date"):
        g = g[np.isfinite(g["fuse"])].sort_values("fuse", ascending=False).head(topn)
        for row in g.itertuples(index=False):
            recs.append({"strategy_id": f"FUSE_{cfg_name.split('_')[0]}",
                         "strategy": f"融合{cfg_name}", "pred_date": str(d)[:10],
                         "code": str(row.code), "direction": 1, "rank_score": float(row.fuse),
                         "source": "replay", "stype": schema.RANKABLE, "replayable": True})
    return schema.make_frame(recs)


def run(panel=_PANEL, cost=0.2):
    pdf = FL.load_panel(panel)
    logger.info("panel %s dates %d", pdf.shape, pdf["date"].nunique())
    book = prices.PriceBook()
    uni = replay_source.sample_universe(800)
    results = []
    for cfg in _CONFIGS:
        for topn in (5, 10):
            preds = build_preds(pdf.copy(), cfg, topn)
            if preds.empty:
                continue
            membership, pred_days = ae.build_membership(preds)
            records = ae.iter_matured_records(preds, book)
            rows = ae.evaluate(records, membership, pred_days, danger_variants=(False,))
            pred_dates = sorted(preds["pred_date"].unique().tolist())
            uni_ret = scoring.universe_returns(pred_dates, ex.TIME_STOP_GRID, uni, book)
            agg = ae.aggregate_exit(rows, uni_ret, danger_variants=(False,))
            sid = f"FUSE_{cfg.split('_')[0]}"
            best = ae.best_combos(agg, level="全部", danger=0, prefer_cost=cost)
            if sid in best:
                b = best[sid]["cell"]
                results.append({"config": cfg, "topn": topn, "best_key": best[sid]["最优key"],
                                **b})
                logger.info("%s Top%d 最优%s ①%.3f ②%.3f ③%.3f(p%s) ②超额%.4f(p%s)",
                            cfg, topn, best[sid]["最优key"], b["①固定持有均收益%"],
                            b["②带离场净收益%"], b["③离场增量%"], b["③增量_聚类p值"],
                            b["②超额vs买入持有全市场%"], b["②超额_聚类p值"])
    return results


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default=_PANEL)
    ap.add_argument("--cost", type=float, default=0.2)
    a = ap.parse_args(argv)
    res = run(a.panel, a.cost)
    print("\n===== 融合基线×带离场三口径(成本%.1f%%严档,按②挑最优组)=====" % a.cost)
    for r in res:
        print(f"{r['config']} Top{r['topn']}: ①固定持有{r['①固定持有均收益%']}% "
              f"②带离场净{r['②带离场净收益%']}% ③增量{r['③离场增量%']}%(p{r['③增量_聚类p值']}) "
              f"②超额{r['②超额vs买入持有全市场%']}%(p{r['②超额_聚类p值']})")
    neg_inc = all(r["③离场增量%"] < 0 for r in res)
    neg_exc = all((r["②超额vs买入持有全市场%"] or 0) < 0 for r in res)
    print(f"\n判定:离场增量③全负={neg_inc};②超额全负={neg_exc} → "
          f"{'带离场口径下不翻案(离场反而伤收益、净超额仍负)' if (neg_inc and neg_exc) else '需细看'}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
