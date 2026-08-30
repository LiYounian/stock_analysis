"""龙虎榜风控轴「前向观察记分卡」(WI-6 Phase 3 —— forward scorecard)。

痛点:剂量标定 + 历史回放给的是"标定值 + 准前向",**真前向**结论须靠每日闭环后逐日**滚存**
才攒得出。本模块就是那台滚存机:每个交易日闭环后记一行/多行——当日 screen_council 里被龙虎榜
轴命中(近日净买上榜)的选股票、降权前后排名、是否被挤出 Top-N;等 K 线走出 T+1/T+5 后**自动
回填**这些命中票的实际前向收益与"是否确实跑输"(见光死证否/证实)。跑几周就攒出**真前向**样本。

与 lhb_dose_replay 的区别:回放是**已知历史窗**的一次性准前向;本记分卡是**逐日增量滚存**,
每天只重跑当日 screen(便宜),历史行只用 K 线**廉价回填**前向收益,天然无未来函数、可每日重跑。

幂等设计(可每天重跑):
  · 每次运行**重跑当日**(--date,缺=最近本地交易日)的 screen + 命中裁决,覆盖该日行(去重键
    =(date, code)),不重复堆积;
  · 对 CSV 里**所有**行,按当前 K 线**能算多少前向收益算多少**(已到期→填实际,未到期→留空 pending);
  · 全量覆盖写出,故重复运行只会把"新到期"的前向收益补上,不产生脏数据。

防未来函数(红线):命中裁决只用 list_date < as_of 的上榜(verdict_from_events 严格过滤);
排名/综合分只用 as_of 及之前的本地 K 线(screen_council offline);前向收益 close[T+H] 仅作
**结果标签**,入场=T+1开盘,绝不回灌进 as_of 决策。

列:date, code, 综合分, 排序分(降权后), rank_before, rank_after, in_top20_before, in_top20_after,
    ejected(在前段且被挤出), net_buy_ratio, reason, r_1, r_5(未到期=空), underperf_1, underperf_5
    (前向收益<0=见光死证实 1 / ≥0=误伤 0;未到期=空)。

**未接 cron**:脚本/入口就位,接入每日闭环由上层择时调用(如 run.py 闭环尾部),不在此自动启用定时。

用法:python -m tools.backtest.lhb_forward_scorecard [--date D] [--universe N]
                                                   [--horizon 1,5] [--out CSV]
非投资建议;历史回测≠未来保证;真前向结论须滚存足够交易日后才成立。
"""
from __future__ import annotations

import argparse
import logging
import os

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.config import strategy as cfg

logger = logging.getLogger("backtest.lhb_forward_scorecard")

TOP_N = 20
DEFAULT_HORIZONS = (1, 5)
_SCRATCH = ("/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
            "c4815bd5-1307-454f-8c30-8e64377b1bf3/scratchpad")
_DEFAULT_OUT = os.path.join(_SCRATCH, "lhb_forward_scorecard.csv")
# 部署到每日闭环时由上层传持久路径(如 data/analysis/backtest/lhb_forward_scorecard.csv)
PERSIST_OUT = "data/analysis/backtest/lhb_forward_scorecard.csv"

_COLS = ["date", "code", "综合分", "排序分", "rank_before", "rank_after",
         "in_top20_before", "in_top20_after", "ejected", "net_buy_ratio", "reason"]


def _record_day(date: str, universe_limit: int | None) -> pd.DataFrame:
    """重跑当日 screen_council + 龙虎榜命中裁决 → 当日命中票行(前向收益留空,后续回填)。"""
    from tools.backtest import lhb_dose_replay as RP
    from tools.pipeline import screen_council as sc
    codes = sc._offline_universe_codes(limit=universe_limit)
    axis = (cfg.risk_veto_cfg().get("龙虎榜", {}) or {})
    # 事件窗:命中日前 窗口天数 到当日(list_date<as_of 内部再严格过滤)
    lo = (pd.Timestamp(date) - pd.Timedelta(days=int(axis.get("窗口天数", 7)) + 3)).strftime("%Y%m%d")
    events = RP._fetch_events(lo, pd.Timestamp(date).strftime("%Y%m%d"))

    v = sc.run_council_screen(codes, as_of=date, fetch=False, top_n=10_000_000)
    rows = v.get("top", [])
    if not rows:
        return pd.DataFrame(columns=_COLS)
    base = sorted(rows, key=lambda x: (x.get("综合分") if isinstance(x.get("综合分"),
                  (int, float)) else -1e9), reverse=True)
    base_rank = {x["code"]: i for i, x in enumerate(base)}
    base_top = {x["code"] for x in base[:TOP_N]}

    enriched = []
    for x in rows:
        code = x["code"]
        verdict = RP._verdict(events, code, date, axis)
        dose = int(((x.get("财报风险") or {}).get("高危数")) or 0)
        adj = cfg.risk_veto_adjust(x.get("综合分", 0.0), dose, verdict)
        enriched.append({"code": code, "综合分": x.get("综合分"), "排序分": adj["排序分"],
                         "lhb_hit": bool((adj.get("各轴") or {}).get("龙虎榜", {}).get("应用")),
                         "verdict": verdict})
    after = sorted(enriched, key=lambda x: (x["排序分"] if x["排序分"] is not None else -1e9),
                   reverse=True)
    after_rank = {x["code"]: i for i, x in enumerate(after)}
    after_top = {x["code"] for x in after[:TOP_N]}

    out = []
    for h in [e for e in enriched if e["lhb_hit"]]:
        code = h["code"]
        in_b = code in base_top
        in_a = code in after_top
        out.append({
            "date": date, "code": code, "综合分": h["综合分"], "排序分": h["排序分"],
            "rank_before": base_rank.get(code), "rank_after": after_rank.get(code),
            "in_top20_before": in_b, "in_top20_after": in_a,
            "ejected": bool(in_b and not in_a),
            "net_buy_ratio": (h["verdict"] or {}).get("net_buy_ratio"),
            "reason": (h["verdict"] or {}).get("reason"),
        })
    logger.info("记分卡 %s:命中 %d(前段 %d,挤出 %d)", date, len(out),
                sum(r["in_top20_before"] for r in out), sum(r["ejected"] for r in out))
    return pd.DataFrame(out, columns=_COLS)


def _backfill_forward(df: pd.DataFrame, horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """对每行按当前本地 K 线回填前向收益 r_h + 见光死证实标记 underperf_h(未到期留空)。

    入场=T+1开盘,退出=T+H收盘(与 lhb_veto_lab / replay 同口径)。code→kline 缓存一次多用。
    """
    from tools.backtest.eval_v3.prices import PriceBook
    pb = PriceBook(loader=market.load_kline_recent)
    for h in horizons:
        df[f"r_{h}"] = np.nan
        df[f"underperf_{h}"] = np.nan
    for i, row in df.iterrows():
        rec = pb.get(str(row["code"]))
        if rec is None:
            continue
        op, _hi, _lo, cl, dmap = rec
        idx = dmap.get(str(row["date"])[:10])
        if idx is None:
            continue
        entry_j = idx + 1
        if entry_j >= len(cl):
            continue
        entry_px = float(op[entry_j]) if float(op[entry_j]) > 0 else float(cl[entry_j])
        if not (entry_px > 0):
            continue
        for h in horizons:
            j = idx + h
            if j < len(cl) and float(cl[j]) > 0:
                r = float(cl[j]) / entry_px - 1.0
                df.at[i, f"r_{h}"] = round(r, 4)
                df.at[i, f"underperf_{h}"] = 1.0 if r < 0 else 0.0
    return df


def update(date: str, out: str = _DEFAULT_OUT, universe_limit: int | None = 2000,
           horizons=DEFAULT_HORIZONS) -> pd.DataFrame:
    """滚存一次:覆盖当日命中行 → 合并历史 → 全量回填前向收益 → 写 CSV。幂等。"""
    prev = pd.DataFrame(columns=_COLS)
    if os.path.exists(out):
        try:
            prev = pd.read_csv(out, dtype={"code": str})
        except Exception:                              # noqa: BLE001
            prev = pd.DataFrame(columns=_COLS)
    fresh = _record_day(date, universe_limit)
    # 去重键 (date, code):丢掉历史里同日行,换成本次重算(幂等)
    if not prev.empty:
        prev = prev[[c for c in _COLS if c in prev.columns]]
        prev = prev[prev["date"].astype(str) != str(date)]
    merged = pd.concat([prev, fresh], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "code"], keep="last").reset_index(drop=True)
    merged = _backfill_forward(merged, horizons)
    merged = merged.sort_values(["date", "rank_before"]).reset_index(drop=True)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    merged.to_csv(out, index=False, encoding="utf-8-sig")
    _log_summary(merged, horizons, out)
    return merged


def _log_summary(df: pd.DataFrame, horizons, out: str) -> None:
    """打印滚存进度 + 已到期前向有效性(样本够才有意义)。"""
    n = len(df)
    matured = {h: int(df[f"r_{h}"].notna().sum()) if f"r_{h}" in df else 0 for h in horizons}
    logger.info("记分卡累积 %d 行(命中票)→ %s;已到期:%s", n, out,
                {f"H{h}": matured[h] for h in horizons})
    for h in horizons:
        col = f"underperf_{h}"
        if col in df and df[col].notna().any():
            sub = df[df[col].notna()]
            logger.info("  H%d 已到期 %d:见光死证实率(前向<0)=%.0f%%,均值前向 %.4f",
                        h, len(sub), 100 * sub[col].mean(), float(sub[f"r_{h}"].mean()))


def _latest_local_date() -> str:
    try:
        df = market.load_kline_recent("000001")
        return str(df["date"].iloc[-1])[:10]
    except Exception:                                  # noqa: BLE001
        return pd.Timestamp.today().strftime("%Y-%m-%d")


def main(argv=None):
    ap = argparse.ArgumentParser(description="龙虎榜轴前向观察记分卡(逐日滚存,防未来函数)")
    ap.add_argument("--date", help="记录哪个交易日(缺=最近本地交易日)")
    ap.add_argument("--out", default=_DEFAULT_OUT, help=f"CSV 路径(闭环持久建议 {PERSIST_OUT})")
    ap.add_argument("--universe", type=int, default=2000)
    ap.add_argument("--horizon", default="1,5")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    hs = tuple(int(x) for x in args.horizon.split(",") if x.strip())
    date = args.date or _latest_local_date()
    update(date, out=args.out, universe_limit=args.universe, horizons=hs)


if __name__ == "__main__":
    main()
