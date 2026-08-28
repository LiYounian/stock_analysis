"""策略0 合议「专家拆分 + 权重组合」横截面回测实验室(分析·非投资建议)。

动机:expert_scorecard 只测单专家/合议综合**方向命中率与超额**,不测**综合分作为排序器**的
横截面选择性。用户要求"卡死单权重不严谨,应拆开重建组合再回测"。本模块把三个在场技术专家
(技术趋势/超买超卖/拐点)的信封(强度/置信度/方向)在**同一横截面面板**上采出来,然后按**多套
权重方案**重算合议综合分 S=Σ(强度×置信度×w)/Σ(w×置信度)(与 council.convene 完全同口径),
对每套方案做横截面评测:

  ① rank-IC / ICIR:每交易日截面 Spearman(综合分, 未来5日收益),跨日均值 + t/p(eval_v3.stats.rank_ic)
  ② Top-N(前5/10/20)精度:每日按综合分降序取 Top-N,均5日收益、命中率、相对当日全宇宙的超额、
     **按日聚类** bootstrap 超额 p(eval_v3.stats.cluster_bootstrap_excess)
  ③ 每专家单独当排序器的 rank-IC(用 强度×置信度 作分):看哪个专家的信号最值得抽取(供融合参考)

防未来函数:与 expert_scorecard 同口径——信号日 idx 只用 ≤idx 的 K 线算专家方向,前向收益取 idx 之后价,
h 仅当 idx+h<len 才计。横截面按**真实交易日日期字符串**对齐(不同上市日的票在同一 idx 是不同日历日,
用日期对齐自然聚成截面);全宇宙基线=当日面板内所有票 5日收益等权。

权重方案不改 strategy.json / 专家默认权重;只在本模块内临时重算 S,产出比较报告。

用法:python -m tools.backtest.council_weight_lab [--sample N] [--step K] [--start-idx S]
                                               [--seed SEED] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import random

import numpy as np

from tools.analysis import experts
from tools.backtest.eval_v3 import stats as _st
from tools.collectors import market
from tools.pipeline.screen_council import build_min_record
from tools.store import repo as store

logger = logging.getLogger("backtest.council_weight_lab")

_DIR = {"看多": 1, "看空": -1, "中性": 0}
ACTIVE_EXPERTS = ["技术趋势", "超买超卖", "拐点"]
MIN_BARS = 60
TAU = 0.2   # 与 THRESHOLDS['合议']['tau'] 一致(方向判定用;排序用连续 S 不涉阈值)

# 权重方案:每套 {专家: 权重}。缺省专家权重按方案给(0=删除该专家)。
WEIGHT_SCHEMES: dict[str, dict] = {
    "V0_等权基线(1/1/1)":       {"技术趋势": 1.0, "超买超卖": 1.0, "拐点": 1.0},
    "V1_只超买超卖":            {"技术趋势": 0.0, "超买超卖": 1.0, "拐点": 0.0},
    "V2_超卖+拐点(0/1/1)":      {"技术趋势": 0.0, "超买超卖": 1.0, "拐点": 1.0},
    "V3_趋势降权0.5":           {"技术趋势": 0.5, "超买超卖": 1.0, "拐点": 1.0},
    "V4_趋势降权0.25":          {"技术趋势": 0.25, "超买超卖": 1.0, "拐点": 1.0},
    "V5_趋势删除(0/1/1)":       {"技术趋势": 0.0, "超买超卖": 1.0, "拐点": 1.0},  # == V2,保留命名对照
    "V6_反转倾斜(0.3/2.0/1.2)": {"技术趋势": 0.3, "超买超卖": 2.0, "拐点": 1.2},
}


# ───────────────────────── 前向收益 ─────────────────────────
def _fwd_ret(close: np.ndarray, idx: int, h: int) -> float | None:
    if idx + h >= len(close) or close[idx] <= 0:
        return None
    return float(close[idx + h] / close[idx] - 1.0) * 100.0


# ───────────────────────── ① 采面板 ─────────────────────────
def collect_panel(sample: int, step: int, start_idx: int, seed: int,
                  horizons=(1, 5)) -> list[dict]:
    """采横截面面板:每 (票, 信号日) 一行,存三专家信封(有符号强度/置信度)+ 前向1/5日收益。

    行结构:{date, code, str_技术趋势, conf_技术趋势, ..., fwd1, fwd5}。
    弃权专家(置信度0/中性)其 str/conf 记 0,0(合议里天然不入分子分母)。
    """
    codes = sorted(store.list_master_codes())
    rng = random.Random(seed)
    picks = rng.sample(codes, min(sample, len(codes)))
    rows: list[dict] = []
    used = 0
    for c in picks:
        try:
            df = market.load_kline(c).reset_index(drop=True)
        except Exception:                                       # noqa: BLE001
            continue
        if len(df) < start_idx + max(horizons) + 5:
            continue
        close = df["close"].to_numpy(float)
        dates = df["date"].astype(str).str.slice(0, 10).tolist()
        used += 1
        for idx in range(start_idx, len(df) - max(horizons), step):
            sub = df.iloc[:idx + 1].reset_index(drop=True)
            if len(sub) < MIN_BARS:
                continue
            rec = build_min_record(c, sub)
            if rec is None:
                continue
            f1 = _fwd_ret(close, idx, 1)
            f5 = _fwd_ret(close, idx, 5)
            if f5 is None:
                continue
            row = {"date": dates[idx], "code": c, "fwd1": f1, "fwd5": f5}
            for name in ACTIVE_EXPERTS:
                v = experts.build(name, rec, sub)
                d = _DIR.get(v.方向, 0)
                s = float(v.强度)          # 已带符号(看空为负;拐点仅看多≥0)
                conf = float(v.置信度)
                # 中性/弃权:贡献置0(与 convene 一致,置信度0 不入分母)
                if d == 0 or conf <= 0:
                    s, conf = 0.0, (conf if v.方向 == "中性" and conf > 0 else 0.0)
                    # 中性但有置信度(超买超卖中性态)仍不贡献强度,但会进分母;
                    # convene 用 强度×置信度=0 分子、权重×置信度 分母。这里保 conf 供分母。
                row[f"str_{name}"] = s
                row[f"conf_{name}"] = conf
            rows.append(row)
    logger.info("面板:抽样 %d 票 / 可用 %d 票 / 面板行 %d", len(picks), used, len(rows))
    return rows


# ───────────────────────── ② 综合分 ─────────────────────────
def composite_score(row: dict, wmap: dict) -> float | None:
    """S=Σ(强度×置信度×w)/Σ(w×置信度),与 council.convene 同口径。分母0→None(该票无有效发声)。"""
    num = den = 0.0
    for name in ACTIVE_EXPERTS:
        w = float(wmap.get(name, 1.0))
        if w == 0:
            continue
        s = row.get(f"str_{name}", 0.0)
        conf = row.get(f"conf_{name}", 0.0)
        num += s * conf * w
        den += w * conf
    if den <= 0:
        return None
    return num / den


# ───────────────────────── ③ 评测 ─────────────────────────
def _by_day(rows: list[dict], score_key: str):
    """按日期聚成截面。返回 {date: [(score, fwd5, fwd1)]}(仅 score 非 None)。"""
    day: dict[str, list] = {}
    for r in rows:
        s = r.get(score_key)
        if s is None:
            continue
        day.setdefault(r["date"], []).append((s, r["fwd5"], r["fwd1"]))
    return day


def eval_ranker(rows: list[dict], score_key: str, topns=(5, 10, 20),
                seed: int = 20260828) -> dict:
    """对某个排序分(综合分或单专家分)评 rank-IC + Top-N 精度(按日聚类超额 p)。"""
    day = _by_day(rows, score_key)
    # rank-IC:每日 (score, fwd5)
    pairs = []
    for _dt, lst in day.items():
        if len(lst) < 3:
            continue
        sc = np.array([x[0] for x in lst], float)
        fw = np.array([x[1] for x in lst], float)
        pairs.append((sc, fw))
    ic = _st.rank_ic(pairs)

    # 全宇宙基线:每日面板内所有有分票的 5日收益均值(选中 vs 当日全体)
    out_topn = {}
    for N in topns:
        strat_day, mkt_day = [], []
        sel_all, hit, n_sel = [], 0, 0
        days_used = 0
        for _dt, lst in day.items():
            if len(lst) < N:
                continue
            days_used += 1
            lst_sorted = sorted(lst, key=lambda x: x[0], reverse=True)
            top = lst_sorted[:N]
            top_fwd = np.array([x[1] for x in top], float)
            uni_fwd = np.array([x[1] for x in lst], float)
            strat_day.append(top_fwd)
            mkt_day.append(float(uni_fwd.mean()))
            sel_all.extend(top_fwd.tolist())
            hit += int((top_fwd > 0).sum())
            n_sel += len(top_fwd)
        if n_sel == 0:
            out_topn[f"Top{N}"] = {"预测日数": 0}
            continue
        ex = _st.cluster_bootstrap_excess(strat_day, mkt_day, seed=seed)
        out_topn[f"Top{N}"] = {
            "预测日数": days_used,
            "选中样本": n_sel,
            "均5日收益%": round(float(np.mean(sel_all)), 3),
            "命中率%": round(hit / n_sel * 100, 1),
            "超额%_vs全宇宙": ex.get("excess"),
            "超额_聚类CI%": ([ex.get("lo"), ex.get("hi")] if ex.get("lo") is not None else None),
            "超额_聚类p值": ex.get("p_value"),
            "聚类交易日数": ex.get("n_days"),
        }
    return {"rank_ic": ic, "topn": out_topn}


def run(sample=600, step=15, start_idx=250, seed=7, out=None) -> dict:
    rows = collect_panel(sample, step, start_idx, seed)
    n_days = len({r["date"] for r in rows})
    logger.info("面板行=%d 覆盖交易日=%d", len(rows), n_days)

    # 每套权重方案:综合分排序评测
    scheme_out = {}
    for sname, wmap in WEIGHT_SCHEMES.items():
        key = f"S::{sname}"
        for r in rows:
            r[key] = composite_score(r, wmap)
        scheme_out[sname] = eval_ranker(rows, key)

    # 每专家单独当排序器(强度×置信度 作分)
    for name in ACTIVE_EXPERTS:
        key = f"solo::{name}"
        for r in rows:
            s = r.get(f"str_{name}", 0.0)
            conf = r.get(f"conf_{name}", 0.0)
            r[key] = (s * conf) if conf > 0 else None
    solo_out = {name: eval_ranker(rows, f"solo::{name}") for name in ACTIVE_EXPERTS}

    result = {
        "生成于": __import__("pandas").Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "口径": "横截面:每日按综合分排序 → rank-IC + Top-N 超额(按日聚类p);防未来函数;非投资建议",
        "参数": {"抽样票数": sample, "step": step, "start_idx": start_idx, "seed": seed,
                 "面板行": len(rows), "覆盖交易日": n_days},
        "在场专家": ACTIVE_EXPERTS,
        "权重方案对比": scheme_out,
        "单专家排序器": solo_out,
    }
    if out:
        import os
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    # 控制台摘要
    print("\n===== 合议权重方案 横截面记分卡 =====")
    print("面板:", result["参数"])
    print("\n[方案对比:rank-IC(5日) + Top-N 5日超额(按日聚类p)]")
    hdr = f"{'方案':<26} {'rankIC':>8} {'ICt':>6} {'ICp':>7} {'IC日':>5} | " \
          f"{'T5超额':>7} {'T5p':>6} | {'T10超额':>7} {'T10p':>6} | {'T20超额':>7} {'T20p':>6}"
    print(hdr)
    for sname, o in scheme_out.items():
        ic = o["rank_ic"]
        t5 = o["topn"].get("Top5", {})
        t10 = o["topn"].get("Top10", {})
        t20 = o["topn"].get("Top20", {})
        print(f"{sname:<26} {str(ic.get('mean_ic')):>8} {str(ic.get('t_stat')):>6} "
              f"{str(ic.get('p_value')):>7} {str(ic.get('n_days')):>5} | "
              f"{str(t5.get('超额%_vs全宇宙')):>7} {str(t5.get('超额_聚类p值')):>6} | "
              f"{str(t10.get('超额%_vs全宇宙')):>7} {str(t10.get('超额_聚类p值')):>6} | "
              f"{str(t20.get('超额%_vs全宇宙')):>7} {str(t20.get('超额_聚类p值')):>6}")
    print("\n[单专家排序器 rank-IC(5日)]")
    for name, o in solo_out.items():
        ic = o["rank_ic"]
        t10 = o["topn"].get("Top10", {})
        print(f"  {name:<10} IC={ic.get('mean_ic')} ICIR={ic.get('icir')} t={ic.get('t_stat')} "
              f"p={ic.get('p_value')} 日={ic.get('n_days')} 正占比={ic.get('pos_ratio')} | "
              f"Top10超额={t10.get('超额%_vs全宇宙')}% p={t10.get('超额_聚类p值')}")
    if out:
        print("→", out)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略0 合议专家权重方案横截面回测")
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--start-idx", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    run(sample=a.sample, step=a.step, start_idx=a.start_idx, seed=a.seed, out=a.out)
