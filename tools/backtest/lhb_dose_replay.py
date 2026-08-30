"""龙虎榜风控轴「历史回放·准前向」评估(WI-6 Phase 3 —— quasi-forward replay)。

在**真实选股流程**(screen_council 全A策略0)上,用**标定后**的龙虎榜轴配置回放最近一段,
量化该轴对选股清单的**实际影响**,回答:

  · 命中票(近日净买上榜)降权前后**排名怎么变**、有没有被挤出 Top-N;
  · 对整体清单的**扰动**(Top-N 成分被替换了几只);
  · **有效性**:被挤出的命中票后续 T+1/T+5 是否确实跑输(=避开见光死,沉底是对的);
  · **误伤**:被降权的命中票里,后续实际上涨的比例(软降权的代价)。

⚠️ 这是**准前向**不是实盘前向:选股综合分=真实 screen_council 现算,龙虎榜命中=真实历史
上榜事件(网络拉取,list_date<as_of 严格防未来),前向收益=真实 T+1开盘→T+H收盘。但回放
用的是**已知历史窗**,非"当日闭环滚存"的真前向;真前向须靠 lhb_forward_scorecard 逐日攒。

防未来函数(红线):
  · 综合分只用 as_of 及之前的本地 K 线(screen_council offline);
  · 龙虎榜命中只用 list_date < as_of 的上榜(verdict_from_events 内部严格过滤);
  · 前向收益仅作**结果标签**:入场=T+1开盘、退出=T+H收盘,绝不回灌进 as_of 决策。

非投资建议;历史回测≠未来保证。

用法:
  python -m tools.backtest.lhb_dose_replay run \
      [--dates 2026-08-11,...,2026-08-21] [--universe 2000] [--horizons 1,5] [--out PATH]
  缺 --dates 时自动取最近若干个"前向可算"(其后≥maxH交易日有本地K线)的交易日。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest import lhb_veto as VETO
from tools.collectors import lhb, market
from tools.config import strategy as cfg

logger = logging.getLogger("backtest.lhb_dose_replay")

TOP_N = 20
OUT_DEFAULT = "data/analysis/backtest/lhb_dose_replay.json"
_DISCLAIMER = ("准前向:综合分=真实screen_council现算,龙虎榜命中=真实历史上榜(list_date<as_of严格防未来),"
               "前向收益=真实T+1开盘→T+H收盘(仅作结果标签)。回放用已知历史窗、非实盘滚存前向;"
               "真前向须靠 lhb_forward_scorecard 逐日累积。仅改排序、非投资建议;历史回测≠未来保证。")


# ═════════════════════ 事件源(网络拉取一次,内存复用) ═════════════════════
def _fetch_events(start: str, end: str) -> dict[str, list[dict]]:
    """拉 [start,end] 全市场上榜事件,归成 {code: [ {list_date,direction,net_buy_ratio}, ... ]}。

    网络失败/空 → 返回空 dict(回放降级为"无命中",诚实报告而非编造)。
    """
    try:
        df = lhb.fetch_range_df(start, end)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("龙虎榜事件拉取失败,回放降级无命中:%s", str(exc)[:120])
        return {}
    if df is None or df.empty:
        return {}
    out: dict[str, list[dict]] = {}
    for r in df.itertuples(index=False):
        out.setdefault(str(r.code), []).append({
            "list_date": str(r.list_date)[:10],
            "direction": int(r.direction),
            "net_buy_ratio": float(r.net_buy_ratio) if pd.notna(r.net_buy_ratio) else None,
        })
    return out


def _verdict(events_by_code: dict, code: str, as_of: str, axis: dict) -> dict | None:
    """用标定后的轴参数对单票做 as-of 入选否决裁决(list_date<as_of 严格)。"""
    evs = events_by_code.get(code)
    if not evs:
        return None
    v = VETO.verdict_from_events(
        evs, as_of, mode=VETO.MODE_ENTRY_VETO,
        window_days=int(axis.get("窗口天数", VETO.VETO_WINDOW_DAYS)),
        min_net_buy_ratio=float(axis.get("最小净买占比", 0.0)))
    return v.to_dict()


# ═════════════════════ 前向收益(结果标签,T+1开盘→T+H收盘) ═════════════════════
def _fwd_ret(pb, code: str, as_of: str, horizons: tuple[int, ...]) -> dict:
    """该票 as_of(=决策日,T+1入场)后各视界实现收益;越界/无价 → 该视界 None。"""
    rec = pb.get(code)
    if rec is None:
        return {}
    op, _hi, _lo, cl, dmap = rec
    idx = dmap.get(str(as_of)[:10])
    if idx is None:
        return {}
    entry_j = idx + 1
    if entry_j >= len(cl):
        return {}
    entry_px = float(op[entry_j]) if float(op[entry_j]) > 0 else float(cl[entry_j])
    if not (entry_px > 0):
        return {}
    out = {}
    for h in horizons:
        j = idx + h
        out[f"r_{h}"] = (round(float(cl[j]) / entry_px - 1.0, 4)
                        if j < len(cl) and float(cl[j]) > 0 else None)
    return out


# ═════════════════════ 单日回放 ═════════════════════
def _replay_day(codes: list[str], as_of: str, events_by_code: dict, pb,
                axis: dict, horizons: tuple[int, ...]) -> dict:
    """跑一天:screen_council 综合分 → 命中裁决 → 标定罚分重排 → 排名变化 + 前向标签。"""
    from tools.pipeline import screen_council as sc
    from tools.analysis.financial import flags as fin_flags
    v = sc.run_council_screen(codes, as_of=as_of, fetch=False, top_n=10_000_000)
    rows = v.get("top", [])
    if not rows:
        return {"as_of": as_of, "note": "空池"}

    # 基线排名(仅综合分降序,不含龙虎榜轴)
    base = sorted(rows, key=lambda x: (x.get("综合分") if isinstance(x.get("综合分"),
                  (int, float)) else -1e9), reverse=True)
    base_rank = {x["code"]: i for i, x in enumerate(base)}
    base_top = {x["code"] for x in base[:TOP_N]}

    # 加龙虎榜轴(财报轴保持:用 run_council_screen 已算的综合分作 base,dose 单独取)
    enriched = []
    for x in rows:
        code = x["code"]
        verdict = _verdict(events_by_code, code, as_of, axis)
        # dose(财报高危红旗数)从 council 记录的财报风险回读(无则0),使两轴 OR 合成口径与生产一致
        dose = int(((x.get("财报风险") or {}).get("高危数")) or 0)
        adj = cfg.risk_veto_adjust(x.get("综合分", 0.0), dose, verdict)
        enriched.append({"code": code, "综合分": x.get("综合分"),
                         "排序分": adj["排序分"], "lhb_hit": bool(
                             (adj.get("各轴") or {}).get("龙虎榜", {}).get("应用")),
                         "verdict": verdict})
    # 标定后排名(与生产 screen_council 同键:排序分降序)
    after = sorted(enriched, key=lambda x: (x["排序分"] if x["排序分"] is not None else -1e9),
                   reverse=True)
    after_rank = {x["code"]: i for i, x in enumerate(after)}
    after_top = {x["code"] for x in after[:TOP_N]}

    hits = [x for x in enriched if x["lhb_hit"]]
    n = len(enriched)
    hit_rows = []
    for h in hits:
        code = h["code"]
        fr = _fwd_ret(pb, code, as_of, horizons)
        hit_rows.append({
            "code": code, "综合分": h["综合分"], "排序分": h["排序分"],
            "rank_before": base_rank.get(code), "rank_after": after_rank.get(code),
            "in_top20_before": code in base_top, "in_top20_after": code in after_top,
            "net_buy_ratio": (h["verdict"] or {}).get("net_buy_ratio"),
            "reason": (h["verdict"] or {}).get("reason"),
            **fr,
        })
    # 扰动:Top-20 成分被替换了几只
    churn = len(base_top - after_top)
    hits_in_top_before = [r for r in hit_rows if r["in_top20_before"]]
    ejected = [r for r in hits_in_top_before if not r["in_top20_after"]]
    return {
        "as_of": as_of, "n_scored": n,
        "n_hits": len(hits),
        "n_hits_in_top20_before": len(hits_in_top_before),
        "n_hits_ejected_from_top20": len(ejected),
        "top20_churn": churn,
        "hit_rows": hit_rows,
    }


# ═════════════════════ 汇总 ═════════════════════
def _aggregate(days: list[dict], horizons: tuple[int, ...]) -> dict:
    """跨日汇总:命中量、挤出率、排名下沉、前向有效性(挤出票 T+H 跑输?)、误伤率。"""
    all_hits = [r for d in days for r in d.get("hit_rows", [])]
    in_top = [r for r in all_hits if r["in_top20_before"]]
    ejected = [r for r in in_top if not r["in_top20_after"]]
    rank_drops = [(r["rank_after"] - r["rank_before"]) for r in all_hits
                  if r["rank_before"] is not None and r["rank_after"] is not None]
    agg = {
        "n_days": len(days),
        "n_hits_total": len(all_hits),
        "n_hits_in_top20_before": len(in_top),
        "n_hits_ejected_from_top20": len(ejected),
        "eject_rate_of_top20_hits": (round(len(ejected) / len(in_top), 3) if in_top else None),
        "median_rank_drop": (int(np.median(rank_drops)) if rank_drops else None),
        "total_top20_churn": sum(d.get("top20_churn", 0) for d in days),
    }
    # 前向有效性 + 误伤(用命中票整体,样本更足;分"挤出票"与"全命中票"两口径)
    for label, subset in (("all_hits", all_hits), ("ejected_top20_hits", ejected)):
        for h in horizons:
            vals = [r.get(f"r_{h}") for r in subset if r.get(f"r_{h}") is not None]
            if vals:
                arr = np.asarray(vals, float)
                agg[f"{label}_H{h}_mean_ret"] = round(float(arr.mean()), 4)
                agg[f"{label}_H{h}_n"] = int(arr.size)
                agg[f"{label}_H{h}_frac_up(误伤)"] = round(float((arr > 0).mean()), 3)
    return agg


def _recent_forward_dates(n: int, max_h: int) -> list[str]:
    """取最近 n 个"前向可算"交易日:其后 ≥max_h 交易日仍有本地 K 线(用 000001 日历)。"""
    try:
        df = market.load_kline_recent("000001")
        cal = [str(x)[:10] for x in df["date"].tolist()]
    except Exception:                                  # noqa: BLE001
        return []
    usable = cal[:-max_h] if len(cal) > max_h else []
    return usable[-n:]


def run(dates: list[str], universe_limit: int | None, horizons=(1, 5),
        out: str | None = OUT_DEFAULT) -> dict:
    """编排:拉事件 → 逐日回放 → 汇总 → 落 JSON。"""
    from tools.pipeline import screen_council as sc
    codes = sc._offline_universe_codes(limit=universe_limit)
    axis = (cfg.risk_veto_cfg().get("龙虎榜", {}) or {})
    max_h = max(horizons)
    # 事件窗:覆盖最早 as_of 前 窗口天数 到最晚 as_of
    lo = (pd.Timestamp(min(dates)) - pd.Timedelta(days=int(axis.get("窗口天数", 7)) + 3))
    hi = pd.Timestamp(max(dates))
    events = _fetch_events(lo.strftime("%Y%m%d"), hi.strftime("%Y%m%d"))
    logger.info("事件覆盖 %d 只票(窗口 %s~%s)", len(events), lo.date(), hi.date())
    pb = _price_book()
    days = []
    for d in dates:
        rep = _replay_day(codes, d, events, pb, axis, tuple(horizons))
        days.append(rep)
        logger.info("回放 %s:命中 %d(前段 %d,挤出 %d),扰动 %d", d, rep.get("n_hits", 0),
                    rep.get("n_hits_in_top20_before", 0), rep.get("n_hits_ejected_from_top20", 0),
                    rep.get("top20_churn", 0))
    agg = _aggregate(days, tuple(horizons))
    rep = {
        "dates": dates, "universe_limit": universe_limit, "horizons": list(horizons),
        "axis_used": axis, "disclaimer": _DISCLAIMER,
        "aggregate": agg, "by_day": days,
    }
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("回放报告已写 %s", out)
    return rep


def _price_book():
    """离线 PriceBook:loader 用 load_kline_recent(只读本地缓存,不触网)。"""
    from tools.backtest.eval_v3.prices import PriceBook
    return PriceBook(loader=market.load_kline_recent)


def main(argv=None):
    ap = argparse.ArgumentParser(description="龙虎榜轴历史回放·准前向(防未来函数)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="跑回放")
    r.add_argument("--dates", help="逗号分隔 as_of(缺=最近若干前向可算交易日)")
    r.add_argument("--universe", type=int, default=2000)
    r.add_argument("--horizons", default="1,5")
    r.add_argument("--n-dates", type=int, default=8, help="缺 --dates 时自动取最近 N 个交易日")
    r.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "run":
        hs = tuple(int(x) for x in args.horizons.split(",") if x.strip())
        dates = ([d.strip() for d in args.dates.split(",") if d.strip()]
                 if args.dates else _recent_forward_dates(args.n_dates, max(hs)))
        rep = run(dates, args.universe, hs, args.out)
        print(json.dumps(rep["aggregate"], ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
