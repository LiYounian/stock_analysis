"""H1 驱动:续选/继续持有 vs 无状态重挑,持续型排名视图上的绝对收益回测。

用法:
  python -m tools.backtest.reselection.run_h1 --data-root <主仓> \
      --score momentum --start 2019-01-01 --end 2026-09-15 \
      --topn 10 --json out.json

⚠️ 测试环境研究模拟,非投资建议。防未来:排名/入场/基准全部只用 ≤D(及 D+1 OHLC)数据。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from . import data as D
from . import rank as R
from . import portfolio as P

logger = logging.getLogger("backtest.reselection.run_h1")

DEFAULT_ROOT = "/Users/yqg/Documents/projects/stock_analysis"


def _year_windows(start: str, end: str) -> list[tuple[str, str, str]]:
    y0, y1 = int(start[:4]), int(end[:4])
    out = []
    for y in range(y0, y1 + 1):
        s = max(start, f"{y}-01-01")
        e = min(end, f"{y}-12-31")
        out.append((str(y), s, e))
    return out


def run(data_root: str, score: str, start: str, end: str, topn: int, N: int,
        entry_rules: list[str], json_path: str | None, oos_split: str,
        universe_sample: int = 0, min_amount: float = 0.0, min_liq: float = 0.0):
    print("\n===== H1 续选 vs 无状态重挑(持续型排名视图·绝对收益 Model A)=====")
    print(f"(score={score} 区间={start}~{end} TopN={topn} 槽N={N} 成交=marketable 成本=10bps)")
    print("(⚠️ 测试环境研究模拟,非投资建议;防未来:排名/入场/基准只用 ≤D 数据)\n")

    codes = D.universe_codes(data_root)
    if min_amount > 0 or universe_sample:
        codes = _liquidity_universe(data_root, codes, start, min_amount, universe_sample)
    print(f"票池 {len(codes)} 只;加载特征(min_date 缓冲 ~ {start[:4]}-01 前一年)...")
    min_date = f"{int(start[:4]) - 1}-06-01"
    with_mom = (score == "momentum")
    feats = D.build_feats(data_root, codes, min_date, with_momentum=with_mom)
    if score == "council":
        _add_council_scores(feats)
    market = D.build_market(feats)
    ranks = R.build_daily_ranks(feats, score_key=("mom" if score == "momentum" else "council"),
                                cap=max(60, topn * 3), min_liq=min_liq)
    print(f"排名视图决策日 {len(ranks)} 个;开始模拟 {len(entry_rules)} 档入场口径 × 2 臂\n")

    res = {"config": dict(score=score, start=start, end=end, topn=topn, N=N, min_liq=min_liq,
                          entry_rules=entry_rules, oos_split=oos_split,
                          n_universe=len(codes), n_feats=len(feats), n_decision_days=len(ranks)),
           "预注册": _PREREG, "免责": "历史回测≠未来保证,非投资建议。"}

    per_rule = {}
    for rule in entry_rules:
        block = {}
        # 全样本
        base = P.simulate(feats, ranks, market, arm="baseline", N=N, topn=topn,
                          entry_rule=rule, start=start, end=end)
        trt = P.simulate(feats, ranks, market, arm="treatment", N=N, topn=topn,
                         entry_rule=rule, start=start, end=end)
        block["overall"] = {"baseline": P.summarize(base), "treatment": P.summarize(trt),
                            "trade_diff_cluster_t": P.cluster_t_diff(trt.trades, base.trades)}
        _print_pair(f"[{rule}] 全样本", block["overall"])
        # walk-forward 分年
        yearly = {}
        for name, s, e in _year_windows(start, end):
            b = P.simulate(feats, ranks, market, arm="baseline", N=N, topn=topn,
                           entry_rule=rule, start=s, end=e)
            t = P.simulate(feats, ranks, market, arm="treatment", N=N, topn=topn,
                           entry_rule=rule, start=s, end=e)
            yearly[name] = {"baseline": P.summarize(b), "treatment": P.summarize(t)}
        block["walk_forward_yearly"] = yearly
        _print_yearly(f"[{rule}] 分年 walk-forward", yearly)
        # OOS 早/晚段
        oos = {}
        for seg, s, e in [("早段", start, oos_split), ("晚段", oos_split, end)]:
            b = P.simulate(feats, ranks, market, arm="baseline", N=N, topn=topn,
                           entry_rule=rule, start=s, end=e)
            t = P.simulate(feats, ranks, market, arm="treatment", N=N, topn=topn,
                           entry_rule=rule, start=s, end=e)
            oos[seg] = {"baseline": P.summarize(b), "treatment": P.summarize(t)}
        block["oos"] = oos
        _print_yearly(f"[{rule}] OOS 早/晚段(split={oos_split})", oos)
        per_rule[rule] = block
    res["by_entry_rule"] = per_rule

    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n结果已落盘:{json_path}")
    return res


def _liquidity_universe(data_root, codes, start, min_amount, sample):
    """按 start 前一段近20日均成交额过滤(降 council 计算量);sample>0 再随机截断。"""
    import numpy as np
    keep = []
    for c in codes:
        df = D.load_kline(data_root, c, min_date=f"{int(start[:4]) - 1}-01-01")
        if df is None or len(df) < 30 or "amount" not in df.columns:
            continue
        amt = df["amount"].tail(20).to_numpy(float)
        if np.nanmean(amt) >= min_amount:
            keep.append(c)
    if sample and len(keep) > sample:
        import random
        keep = sorted(random.Random(7).sample(keep, sample))
    logger.info("流动性票池 %d 只(min_amount=%.0f)", len(keep), min_amount)
    return keep


def _add_council_scores(feats):
    """给每票补 council 综合分序列(复用 backtest_rank._score_council,逐日重算,慢)。"""
    import numpy as np
    import pandas as pd
    from tools.backtest.backtest_rank import _score_council
    n_done = 0
    for code, f in feats.items():
        c = f["c"]
        dates = f["dates"]
        s = np.full(len(c), np.nan)
        df = pd.DataFrame({"date": pd.to_datetime(dates), "open": f["o"], "high": f["h"],
                           "low": f["lo"], "close": c, "volume": np.nan_to_num(
                               f.get("turnover", np.zeros(len(c))))})
        for t in range(60, len(c)):
            val = _score_council(df.iloc[:t + 1], code)
            if val is not None and np.isfinite(val):
                s[t] = val
        f["council"] = s
        n_done += 1
        if n_done % 200 == 0:
            logger.info("council 打分 %d/%d", n_done, len(feats))


_PREREG = {
    "续选规则": "已选票到期(D+2)时,若仍在最新决策日 TopN 且当日收盘≥入场价(未破卖出线)→ 续持;"
                "否则收盘卖出。续持者逐日复用同一判据。baseline=到期无条件卖出、每日从 TopN 重挑。",
    "入场口径": "Model A:D 选→D+1 限价回踩 marketable 成交(主口径 limit_pc_0.01=昨收×0.99)→"
                "涨停不可买剔除→10bps round-trip 成本。",
    "成功判据": "treatment 绝对收益(cum/annualized net)≥ baseline 且最大回撤不显著抬高;"
                "walk-forward 分年一致 + OOS 早晚段同向 + 逐笔按 exec_date 聚类 t。",
    "防偷看": "规则先于跑写死;绝不对通鼎/双星/奥士康调参;参数高原查 TopN∈{5,10,20}、入场口径三档。",
}


def _print_pair(title, blk):
    b, t = blk["baseline"], blk["treatment"]
    print(f"—— {title} ——")
    print(f"   baseline : cum_net={b['cum_net']} ann={b['annualized_net']} mdd={b['max_drawdown']} "
          f"sharpe={b['sharpe']} winr={b['trade_win_rate']} turn={b['turnover_daily']} n={b['n_trades']}")
    print(f"   treatment: cum_net={t['cum_net']} ann={t['annualized_net']} mdd={t['max_drawdown']} "
          f"sharpe={t['sharpe']} winr={t['trade_win_rate']} turn={t['turnover_daily']} n={t['n_trades']} "
          f"avg_hold={t['avg_hold_days']}")
    ct = blk.get("trade_diff_cluster_t")
    if ct:
        print(f"   逐笔差(trt−base) mean_net_diff={ct['diff']} cluster_t={ct['cluster_t']} "
              f"(n {ct['n_a']}/{ct['n_b']})")


def _print_yearly(title, yearly):
    print(f"—— {title} ——")
    for name, blk in yearly.items():
        b, t = blk["baseline"], blk["treatment"]
        print(f"   {name}: base cum={b['cum_net']} mdd={b['max_drawdown']} | "
              f"trt cum={t['cum_net']} mdd={t['max_drawdown']} | Δcum={round(t['cum_net'] - b['cum_net'], 4)}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=DEFAULT_ROOT)
    ap.add_argument("--score", choices=["momentum", "council"], default="momentum")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--topn", type=int, default=10)
    ap.add_argument("--N", type=int, default=10)
    ap.add_argument("--entry-rules", default="limit_pc_0.01,limit_pc_0.0,open")
    ap.add_argument("--oos-split", default="2023-01-01")
    ap.add_argument("--min-amount", type=float, default=0.0)
    ap.add_argument("--min-liq", type=float, default=0.0,
                    help="排名视图每日近20日均成交额(元)下限,防微盘幻觉;如 2e8")
    ap.add_argument("--universe-sample", type=int, default=0)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    run(a.data_root, a.score, a.start, a.end, a.topn, a.N,
        [r for r in a.entry_rules.split(",") if r], a.json or None, a.oos_split,
        universe_sample=a.universe_sample, min_amount=a.min_amount, min_liq=a.min_liq)
