"""龙虎榜风控轴「剂量标定」实验室(WI-6 Phase 3 —— dose calibration)。

把龙虎榜轴的三个旋钮从「工程占位」标定成「有据值」,回答三问:

  ① **触发罚分**该多大:降权罚分作用在**合议综合分**尺度上(经验实测 top≈0.53、中位=0、
     仅 ~31% 为正)。罚分要**足以把命中票挤出选股前段**、又**与财报红旗轴(0.5/面)协调、
     不过度**(不越两轴合成封顶 1.5)。本 lab 在真实综合分分布上扫候选罚分,量化每档的
     "命中票被挤出 top-N 比例 / 排名下沉幅度 / 是否压到 0 以下",给协调有据的值。
  ② **最小净买占比门槛**该多高:命题曾假设"只惩罚高净买占比的追高票"。本 lab 读
     lhb_veto_lab 的净买占比分档证据检验该假设——若 H5/H10 各档一致显著负(而非只有高档),
     则门槛应保持 0.0(否则会漏掉低档见光死)。用分档区分度定门槛,**证据说了算**。
  ③ **模式(降权 vs 否决)**:结合稳健性给建议(软降权沉底 vs 硬否决剔除)。

数据源(两路,均无未来函数):
  · **综合分尺度**:offline 跑 screen_council(只读本地 K 线,list_date 无关),得真实综合分分布;
  · **收益侧证据**:读既有 lhb_veto_lab.json(净买腿 H5 −1.68%、分档单调性、逐年一致),
    该 lab 入场=T+1开盘、退出=T+H收盘,防未来函数已坐实。本模块不重算收益,只做尺度标定。

⚠️ 罚分作用在**排序展示层**,不构成买卖建议;历史回测≠未来保证,非投资建议。

用法:
  python -m tools.backtest.lhb_dose_lab calibrate \
      [--dates 2026-08-18,2026-08-19,2026-08-20] [--universe 2000] \
      [--candidates 0.3,0.4,0.5,0.6,0.8,1.0] [--out PATH]
  缺 --dates 时自动取最近若干有本地 K 线的交易日。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger("backtest.lhb_dose")

# 与 config 现状对齐的参照量(标定协调性判据的锚点)
REDFLAG_PER_FACE = 0.5      # 财报红旗单面罚分(协调锚:一次净买上榜 ≈ 一面红旗的风险事件量级)
AGG_CAP = 1.5               # 两轴合成罚分封顶(标定值 + 财报叠加不得越顶)
TOP_N = 20                  # 选股前段口径(策略0 Top N)
LAB_JSON = "data/analysis/backtest/lhb_veto_lab.json"
OUT_DEFAULT = "data/analysis/backtest/lhb_dose_calibration.json"
DEFAULT_CANDIDATES = (0.3, 0.4, 0.5, 0.6, 0.8, 1.0)
_DISCLAIMER = ("罚分作用在合议综合分排序尺度上(经验实测 top≈0.53、中位0);标定协调锚=财报红旗0.5/面、"
               "合成封顶1.5;收益侧见光死证据引自 lhb_veto_lab(T+1开盘入、防未来函数)。"
               "仅改排序展示、非买卖建议;历史回测≠未来保证。")


# ═════════════════════ ① 综合分分布(真实 screen_council,offline) ═════════════════════
def collect_score_dist(dates: list[str], universe_limit: int | None) -> dict:
    """对每个 as_of offline 跑 screen_council,收全票综合分(不止 top)。

    返回 {date: sorted_scores(np.ndarray 升序)};无本地 K 线的日期自然产空池被跳过。
    """
    from tools.pipeline import screen_council as sc
    codes = sc._offline_universe_codes(limit=universe_limit)
    per_date: dict[str, np.ndarray] = {}
    for d in dates:
        v = sc.run_council_screen(codes, as_of=d, fetch=False, top_n=10_000_000)
        sco = [t["综合分"] for t in v.get("top", [])
               if isinstance(t.get("综合分"), (int, float)) and not isinstance(t.get("综合分"), bool)]
        if sco:
            per_date[d] = np.sort(np.asarray(sco, dtype=float))
            logger.info("综合分分布 %s:n=%d top20cut=%.4f", d, len(sco),
                        np.sort(per_date[d])[-TOP_N] if len(sco) >= TOP_N else float("nan"))
    return per_date


def _dist_summary(per_date: dict) -> dict:
    """池化所有日期的综合分,给分位数 / top-N 切分 / 正分占比(尺度锚)。"""
    pool = np.concatenate(list(per_date.values())) if per_date else np.asarray([])
    if pool.size == 0:
        return {"n": 0}
    cuts = {}
    for d, arr in per_date.items():
        if arr.size >= TOP_N:
            cuts.setdefault("top20", []).append(float(arr[-TOP_N]))
        if arr.size >= 30:
            cuts.setdefault("top30", []).append(float(arr[-30]))
    return {
        "n": int(pool.size), "n_dates": len(per_date),
        "pct": {f"p{p}": round(float(np.percentile(pool, p)), 4)
                for p in (50, 75, 90, 95, 99, 100)},
        "min": round(float(pool.min()), 4),
        "frac_positive": round(float((pool > 0).mean()), 4),
        "top20_cut_mean": round(float(np.mean(cuts.get("top20", [np.nan]))), 4),
        "top30_cut_mean": round(float(np.mean(cuts.get("top30", [np.nan]))), 4),
    }


# ═════════════════════ ② 罚分扫描(在真实分布上量化"命中即挤出") ═════════════════════
def _sweep_penalty(per_date: dict, penalty: float) -> dict:
    """假设 top 段每只票都被命中(降权 penalty),量化其被挤出前段的力度。

    对每个日期:取该日 top-N 的票,减 penalty 后在该日**全票分布**里重新定位,统计:
      · ejected_from_topN:跌出该日 top-N 的比例(挤出前段的核心指标);
      · ejected_from_top30:跌出 top-30 的比例;
      · below_zero:降权后分 <0 的比例(压到"非正、无操作意义"区);
      · median_rank_drop_pct:排名下沉的中位百分位跌幅(0~1,占全票数比例);
      · landing_pct_of_max:该日最高分票被命中后落到的分位(检验最强票也能被压下)。
    汇总时各日等权平均。penalty 与红旗单面 0.5、合成封顶 1.5 的协调关系另在 summary 判。
    """
    ej_n, ej_30, blw0, drop, land = [], [], [], [], []
    for arr in per_date.values():
        n = arr.size
        if n < TOP_N:
            continue
        asc = arr                                   # 升序
        topn_cut = asc[-TOP_N]
        top30_cut = asc[-30] if n >= 30 else asc[0]
        top_scores = asc[-TOP_N:]                    # 该日 top-N 票的原分
        new_scores = top_scores - penalty
        ej_n.append(float(np.mean(new_scores < topn_cut)))
        ej_30.append(float(np.mean(new_scores < top30_cut)))
        blw0.append(float(np.mean(new_scores < 0)))
        # 排名下沉:原分位 vs 新分位(searchsorted 在该日升序分布里的名次占比)
        old_rank = np.searchsorted(asc, top_scores, side="left") / n
        new_rank = np.searchsorted(asc, new_scores, side="left") / n
        drop.append(float(np.median(old_rank - new_rank)))
        # 最高分票落点
        land.append(float(np.searchsorted(asc, asc[-1] - penalty, side="left") / n))
    if not ej_n:
        return {"note": "无足够 top-N 样本"}
    return {
        "penalty": penalty,
        "ejected_from_topN": round(float(np.mean(ej_n)), 3),
        "ejected_from_top30": round(float(np.mean(ej_30)), 3),
        "below_zero": round(float(np.mean(blw0)), 3),
        "median_rank_drop_pct": round(float(np.mean(drop)), 3),
        "landing_pct_of_max": round(float(np.mean(land)), 3),
        # 协调判据
        "vs_redflag_face": round(penalty / REDFLAG_PER_FACE, 2),   # =1.0 即与一面红旗等量
        "headroom_under_cap": round(AGG_CAP - penalty, 2),         # 与财报叠加的剩余空间(应>0)
    }


# ═════════════════════ ③ 门槛标定(读 lab 分档证据) ═════════════════════
def _threshold_analysis(lab: dict) -> dict:
    """读 lhb_veto_lab 净买占比分档,检验"只有高档见光死"假设是否成立。

    对每个视界报各档 net_excess/net_p;判定:
      · H1 是否单调(仅高档显著负)→ 若纯即时否决用 H1,门槛可抬到高档区间起点;
      · H5/H10 是否**各档一致显著负**→ 若成立,持有多日的选股场景门槛应保持 0.0
        (抬门槛会漏掉低档见光死)。
    """
    buckets = lab.get("net_buy_ratio_buckets", {}) or {}
    view = {}
    for h in ("H1", "H5", "H10"):
        bl = buckets.get(h)
        if not isinstance(bl, list):
            view[h] = {"note": "无分档"}
            continue
        rows = [{"ratio_range": b.get("ratio_range"), "net_excess": b.get("net_excess"),
                 "net_p": b.get("net_p")} for b in bl]
        sig_neg = [(b.get("net_excess") or 0) < 0 and (b.get("net_p") if b.get("net_p") is not None
                   else 1) < 0.1 for b in bl]
        # 单调性:net_excess 随档位递减(越高档越负)
        nes = [b.get("net_excess") for b in bl if b.get("net_excess") is not None]
        monotone_worse = all(nes[i] >= nes[i + 1] for i in range(len(nes) - 1)) if len(nes) >= 2 else None
        view[h] = {
            "buckets": rows,
            "all_buckets_sig_neg": bool(sig_neg) and all(sig_neg),
            "only_high_sig_neg": bool(sig_neg) and sig_neg[-1] and not sig_neg[0],
            "monotone_worse_with_ratio": monotone_worse,
        }
    # 门槛建议:选股持有多日 → 以 H5 为准
    h5 = view.get("H5", {})
    if h5.get("all_buckets_sig_neg"):
        reco = ("门槛=0.0:H5/H10 各净买占比档一致显著负(低档甚至更负),"
                "'只惩罚高档追高'假设被否;抬门槛会漏掉低档见光死。")
        thr = 0.0
    elif h5.get("only_high_sig_neg"):
        hi = (h5.get("buckets") or [{}])[-1].get("ratio_range") or [None, None]
        reco = f"门槛≈{hi[0]}:仅高净买占比档显著负,可只惩罚追高票。"
        thr = float(hi[0] or 0.0)
    else:
        reco = "证据不足以抬门槛,保守保持 0.0。"
        thr = 0.0
    return {"by_horizon": view, "recommended_min_net_buy_ratio": thr, "rationale": reco}


# ═════════════════════ ④ 综合建议 ═════════════════════
def _recommend(dist: dict, sweep: list[dict], thr: dict, lab: dict) -> dict:
    """挑罚分:满足(与红旗协调 ≤ ~1 倍量级、封顶下有余量、能把命中票有效挤出 top-N)的最小值。

    "有效挤出" = ejected_from_topN ≥ 0.9(命中票几乎必出前段);在满足该条件的候选里,
    优先取**与红旗轴最协调**(vs_redflag_face 最接近 1.0)且不过度的那个。
    """
    ok = [s for s in sweep if isinstance(s.get("ejected_from_topN"), float)
          and s["ejected_from_topN"] >= 0.9 and s.get("headroom_under_cap", -1) > 0]
    if ok:
        # 与红旗 0.5 协调优先(|vs_redflag_face −1| 最小),同分取较小罚分
        ok.sort(key=lambda s: (abs(s["vs_redflag_face"] - 1.0), s["penalty"]))
        pen = ok[0]["penalty"]
        pen_reason = (f"罚分={pen}:在真实综合分分布上使命中票挤出 top-N 比例="
                      f"{ok[0]['ejected_from_topN']:.0%}、下沉中位 "
                      f"{ok[0]['median_rank_drop_pct']:.0%} 分位;与财报红旗 0.5/面协调"
                      f"(={ok[0]['vs_redflag_face']}倍)、封顶 1.5 下留 {ok[0]['headroom_under_cap']} 余量。")
    else:
        pen = 0.5
        pen_reason = "候选均未达'挤出 top-N≥90%'或均越顶,回退与红旗等量的 0.5。"
    concl = (lab.get("conclusion", {}) or {})
    h5_avoid = concl.get("净买H5避免跑输(净,%)")
    mode_reason = (
        "模式=降权(软沉底):见光死是**分布性**均值效应(净买腿 H5 净超额 "
        f"{h5_avoid}、逐年一致但非每票必跌),软降权把命中票压出前段即够,"
        "保留展示与可追溯,误伤小于硬剔除;'否决/剔除'留给未来'极高净买占比+近日多次上榜'高置信场景。")
    return {
        "触发罚分_前": 0.6, "触发罚分_后": pen, "触发罚分_依据": pen_reason,
        "最小净买占比_前": 0.0, "最小净买占比_后": thr["recommended_min_net_buy_ratio"],
        "最小净买占比_依据": thr["rationale"],
        "模式_前": "降权", "模式_后": "降权", "模式_依据": mode_reason,
        "按条数加权": False, "按条数加权_依据":
            "保持关闭:单次上榜给固定剂量;n_recent 的分级罚分缺独立标定,留作未来工作(避免越顶)。",
    }


def calibrate(dates: list[str], universe_limit: int | None,
              candidates=DEFAULT_CANDIDATES, out: str | None = OUT_DEFAULT,
              lab_path: str = LAB_JSON) -> dict:
    """编排:综合分分布 → 罚分扫描 → 门槛证据 → 综合建议 → 落 JSON。"""
    per_date = collect_score_dist(dates, universe_limit)
    dist = _dist_summary(per_date)
    sweep = [_sweep_penalty(per_date, float(p)) for p in candidates]
    lab = {}
    lp = Path(lab_path)
    if lp.exists():
        lab = json.loads(lp.read_text(encoding="utf-8"))
    else:
        logger.warning("缺 %s;门槛/收益侧证据不可用,请先跑 lhb_veto_lab", lab_path)
    thr = _threshold_analysis(lab)
    reco = _recommend(dist, sweep, thr, lab)
    rep = {
        "dates": dates, "universe_limit": universe_limit,
        "disclaimer": _DISCLAIMER,
        "score_dist": dist,
        "penalty_sweep": sweep,
        "threshold_analysis": thr,
        "recommendation": reco,
        "lab_source": {"path": lab_path, "window": lab.get("window"),
                       "n_events": lab.get("n_events"),
                       "conclusion": lab.get("conclusion")},
    }
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("标定报告已写 %s", out)
    return rep


def _recent_local_dates(n: int = 3) -> list[str]:
    """取最近 n 个有本地 K 线的交易日(用 000001 的日期序列近似交易日历)。"""
    try:
        from tools.collectors import market
        df = market.load_kline_recent("000001")
        ds = [str(x)[:10] for x in df["date"].tail(n + 6)]
        return ds[-(n + 5):-5][-n:] if len(ds) > n + 5 else ds[:n]
    except Exception:                                  # noqa: BLE001
        return []


def main(argv=None):
    ap = argparse.ArgumentParser(description="龙虎榜风控轴剂量标定(防未来函数)")
    sub = ap.add_subparsers(dest="cmd")
    c = sub.add_parser("calibrate", help="跑标定")
    c.add_argument("--dates", help="逗号分隔 as_of(缺=最近若干本地交易日)")
    c.add_argument("--universe", type=int, default=2000, help="票池前 N 只(默认 2000)")
    c.add_argument("--candidates", default=",".join(str(x) for x in DEFAULT_CANDIDATES))
    c.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "calibrate":
        dates = ([d.strip() for d in args.dates.split(",") if d.strip()]
                 if args.dates else _recent_local_dates(3))
        cands = [float(x) for x in args.candidates.split(",") if x.strip()]
        rep = calibrate(dates, args.universe, cands, args.out)
        print(json.dumps({"score_dist": rep["score_dist"],
                          "recommendation": rep["recommendation"]},
                         ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
