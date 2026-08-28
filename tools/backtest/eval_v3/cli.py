"""eval_v3 入口:双轨(live/replay)统一打分 + 六维聚合 + 报告/JSON 产物。

用法:
  python -m tools.backtest.eval_v3 [--analysis-dir DIR] [--horizon 1,5]
      [--track live|replay|both] [--replay-universe-n 800] [--replay-days 250]
      [--replay-stride 1] [--live-baseline-n 1500]
      [--out-md docs/策略成绩报告.md] [--out-json data/analysis/backtest/eval_v3.json]

产物只写 worktree 本地。非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import pandas as pd

from tools.config import settings

from . import aggregate, live_source, prices, replay_source, report, scoring

logger = logging.getLogger("backtest.eval_v3.cli")

_DEFAULT_ANALYSIS = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
_DEFAULT_MD = str(settings.PROJECT_ROOT / "docs" / "策略成绩报告.md")
_DEFAULT_JSON = str(settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "eval_v3.json")


def _run_live(analysis_dir, horizons, baseline_n, book):
    preds = live_source.load_live_predictions(analysis_dir)
    scored = scoring.score_predictions(preds, book, horizons)
    if scored.empty:
        return {"窗口": {}}, scored
    pred_dates = sorted(scored["pred_date"].unique().tolist())
    uni = replay_source.sample_universe(baseline_n)
    ur = scoring.universe_returns(pred_dates, horizons, uni, book)
    cal = replay_source.build_calendar(uni)
    agg = aggregate.aggregate(scored, ur, cal, horizons, track="live")
    agg["宇宙"] = f"全市场基准抽样 {len(uni)} 票"
    return agg, scored


def _run_replay(horizons, universe_n, lookback_days, stride, book):
    preds, meta = replay_source.replay_predictions(
        universe_n=universe_n, lookback_days=lookback_days, stride=stride)
    scored = scoring.score_predictions(preds, book, horizons)
    if scored.empty:
        return {"窗口": {}}, scored, meta
    pred_dates = sorted(scored["pred_date"].unique().tolist())
    uni = replay_source.sample_universe(universe_n)   # 同一抽样宇宙作等权基准
    ur = scoring.universe_returns(pred_dates, horizons, uni, book)
    cal = replay_source.build_calendar(uni)
    agg = aggregate.aggregate(scored, ur, cal, horizons, track="replay")
    agg["宇宙"] = f"回放抽样 {len(uni)} 票"
    return agg, scored, meta


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis-dir", default=_DEFAULT_ANALYSIS)
    ap.add_argument("--horizon", default="1,5")
    ap.add_argument("--track", default="both", choices=["live", "replay", "both"])
    ap.add_argument("--replay-universe-n", type=int, default=800)
    ap.add_argument("--replay-days", type=int, default=250)
    ap.add_argument("--replay-stride", type=int, default=1)
    ap.add_argument("--live-baseline-n", type=int, default=1500)
    ap.add_argument("--out-md", default=_DEFAULT_MD)
    ap.add_argument("--out-json", default=_DEFAULT_JSON)
    a = ap.parse_args(argv)
    horizons = tuple(int(x) for x in a.horizon.split(","))
    book = prices.PriceBook()

    live_agg, replay_agg, replay_meta = {"窗口": {}}, {"窗口": {}}, {}
    if a.track in ("live", "both"):
        logger.info("live 轨评测…")
        live_agg, _ = _run_live(a.analysis_dir, horizons, a.live_baseline_n, book)
    if a.track in ("replay", "both"):
        logger.info("replay 轨评测(抽样 %d 票 × 近 %d 交易日,stride=%d)…",
                    a.replay_universe_n, a.replay_days, a.replay_stride)
        replay_agg, _, replay_meta = _run_replay(
            horizons, a.replay_universe_n, a.replay_days, a.replay_stride, book)

    lag = report.flag_laggards(replay_agg, live_agg, horizons)
    done = ("①双轨分明 ②T+1入场 ③收益质量(均值/中位/盈亏比/P10P90) "
            "④超额(全市场等权+随机bootstrap) ⑤按日聚类显著性(CI+p) ⑥rank-IC(排序型)")
    undone = ("指数基准(HS300/中证1000)未接,用全市场等权代理(见假设);回放仅覆盖5个纯技术方向型"
              "screener(S01/S02/箱体/S03/S04),动量/SEPA/条件化回放待接;live轨长窗数据不足如实标注")
    assumptions = [
        "无 open 的历史 bar 用 close[T+1] 当入场价(极少数;单元格'用close入场占比%'量化)",
        "对标指数基准未接入,④超额暂用『全市场等权平均收益』作基准代理(口径可复现);随机同数量 bootstrap 已做",
        "交易日历用高流动参考票 kline date 列并集近似",
        "回放宇宙用全 master 均匀抽样(默认 800 票)控制运行时长,均匀抽样无选择偏差、可复现",
    ]
    md = report.render(live_agg, replay_agg, replay_meta,
                       generated=pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
                       horizons=horizons, laggards=lag, done=done, undone=undone,
                       assumptions=assumptions)

    os.makedirs(os.path.dirname(a.out_json) or ".", exist_ok=True)
    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump({"live": live_agg, "replay": replay_agg, "replay_meta": replay_meta,
                   "差生名单": lag}, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(a.out_md) or ".", exist_ok=True)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(md)

    print("\n===== eval_v3 双轨记分卡 =====")
    print(f"live 策略数 {len(live_agg.get('窗口', {}).get('全史', {}).get('策略', {}))} | "
          f"replay 策略数 {len(replay_agg.get('窗口', {}).get('全史', {}).get('策略', {}))}")
    print(f"回放元信息: {replay_meta}")
    print(f"差生名单(坐实显著负): {[l['strategy_id'] for l in lag if l['显著负']]}")
    print(f"→ {a.out_md}\n→ {a.out_json}")
    return {"live": live_agg, "replay": replay_agg, "laggards": lag}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
