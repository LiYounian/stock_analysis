"""排序型策略回测器(策略4 动量A腿 / 策略0 合议 共用)。

排序型 ≠ 信号型:不问"买不买",问"谁更强"。故不测胜率/盈亏比,测**预测力与分层收益**:
  1. IC —— 每个交易日横截面上「打分 vs 前瞻 N 日收益」的 Spearman 相关;时序均值 + ICIR
     (=均值/标准差)+ t 检验。IC 显著>0 才说明打分有预测力。
  2. 分层 —— 每日按打分分十档(横截面分位),池化后各档前瞻 N 日均收益;单调递增 = 有效。
  3. TopK 组合 —— 每日取分数最高 K 只等权,前瞻 N 日均收益 vs 全样本均值(≈基准)的超额。

打分函数(可插拔,测的就是策略本身):
  · 动量A腿:`momentum.weighted_log_momentum(kdf[:t+1]).score`
  · 合议:  `council.build_council_block(build_min_record(code,kdf[:t+1]), kdf[:t+1]).default.综合分`

防未来函数:打分只用 kdf.iloc[:t+1];前瞻收益 close[t+N]/close[t]-1 用 t 之后价,仅作被预测标签。
横截面对齐用**真实日期**(不同票历史长度不同,整数 t 不对齐)。
用法:python -m tools.backtest.backtest_rank [--score momentum|council] [--sample N] [--step K] [--horizon 5,10,20]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.rank")

_WARMUP = 40
_DISCLAIMER = "历史回测≠未来保证,非投资建议。"


# ————————————————————————— 打分函数(可插拔)—————————————————————————
def _score_momentum(kdf_slice: pd.DataFrame, code: str) -> float | None:
    from tools.strategy.momentum import weighted_log_momentum
    try:
        return float(weighted_log_momentum(kdf_slice).get("score", 0.0))
    except Exception:
        return None


def _rev_k(k: int):
    """构造"过去 k 日反转"打分:−(close[t]/close[t-k]−1)。买近期跌得多的(反转假设)。"""
    def _s(kdf_slice: pd.DataFrame, code: str) -> float | None:
        c = kdf_slice["close"].to_numpy(float)
        if len(c) < k + 1 or c[-(k + 1)] <= 0:
            return None
        return -float(c[-1] / c[-(k + 1)] - 1.0)
    return _s


def _score_rev_mom(kdf_slice: pd.DataFrame, code: str) -> float | None:
    """反做加权对数动量:−weighted_log_momentum.score(测"反转=动量镜像"是否成立)。"""
    from tools.strategy.momentum import weighted_log_momentum
    try:
        return -float(weighted_log_momentum(kdf_slice).get("score", 0.0))
    except Exception:
        return None


def _score_council(kdf_slice: pd.DataFrame, code: str) -> float | None:
    from tools.analysis import council
    from tools.pipeline.screen_council import build_min_record
    try:
        rec = build_min_record(code, kdf_slice)
        if rec is None:
            return None
        cblk = council.build_council_block(rec, kdf_slice)
        d = (cblk or {}).get("default") or {}
        s = d.get("综合分")
        return float(s) if s is not None else None
    except Exception:
        return None


_SCORERS = {"momentum": _score_momentum, "council": _score_council,
            "rev5": _rev_k(5), "rev10": _rev_k(10), "rev20": _rev_k(20),
            "rev_mom": _score_rev_mom}


# ————————————————————————— 建横截面 panel —————————————————————————
def build_rank_panel(codes, scorer, horizons=(5, 10, 20), step=1, warmup=_WARMUP) -> pd.DataFrame:
    """逐票逐日打分 + 前瞻收益,落长表 (date, code, score, r_5, r_10, r_20)。无未来函数。"""
    maxN = max(horizons)
    rows = []
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < warmup + maxN + 5:
            continue
        df = df.reset_index(drop=True)
        close = df["close"].to_numpy(float)
        vol = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
        amt = close * vol                                    # 成交额代理(流动性)
        dates = [str(x)[:10] for x in df["date"].tolist()]
        n = len(df)
        used += 1
        for t in range(warmup, n - maxN, step):
            s = scorer(df.iloc[: t + 1], code)
            if s is None or not np.isfinite(s):
                continue
            liq = float(np.mean(amt[max(0, t - 19): t + 1]))  # 近20日均成交额
            row = {"date": dates[t], "code": code, "score": s, "liq": liq}
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


# ————————————————————————— 指标 —————————————————————————
def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """无 scipy 依赖的 Spearman:对排名做 Pearson。"""
    if len(a) < 3:
        return float("nan")
    # copy=True:pandas 3.0 的 rank().to_numpy() 返回只读视图,原地 -= 会 ValueError(只读);
    # 拷成可写副本,语义不变(修 pandas 3.0 下 rank-IC 引擎的隐性破裂)。
    ra = pd.Series(a).rank().to_numpy(copy=True)
    rb = pd.Series(b).rank().to_numpy(copy=True)
    ra -= ra.mean(); rb -= rb.mean()
    d = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / d) if d > 0 else float("nan")


def ic_metrics(panel: pd.DataFrame, N: int, min_cross=10) -> dict:
    """横截面 IC:每日 spearman(score, r_N) → 时序均值/ICIR/t/胜率。"""
    col = f"r_{N}"
    ics = []
    for _, g in panel.groupby("date"):
        if len(g) >= min_cross:
            ic = _spearman(g["score"].to_numpy(), g[col].to_numpy())
            if np.isfinite(ic):
                ics.append(ic)
    if len(ics) < 5:
        return {"IC均值": None, "有效交易日": len(ics)}
    arr = np.array(ics)
    mean, sd = float(arr.mean()), float(arr.std(ddof=1))
    icir = mean / sd if sd > 0 else float("nan")
    tstat = icir * np.sqrt(len(arr))
    return {"IC均值": round(mean, 4), "ICIR": round(icir, 3), "t": round(tstat, 2),
            "IC>0占比%": round(float((arr > 0).mean()) * 100, 1), "有效交易日": len(arr)}


def decile_metrics(panel: pd.DataFrame, N: int, min_cross=10) -> list:
    """每日按 score 横截面分十档,池化各档前瞻 N 日均收益(单调=有效)。"""
    col = f"r_{N}"
    buckets = {i: [] for i in range(10)}
    for _, g in panel.groupby("date"):
        if len(g) < min_cross:
            continue
        q = g["score"].rank(pct=True)
        dec = (q * 10).clip(upper=9.999).astype(int)
        for i, r in zip(dec.to_numpy(), g[col].to_numpy()):
            buckets[int(i)].append(r)
    return [{"档": i, "均收益%": round(float(np.mean(v)), 2) if v else None, "n": len(v)}
            for i, v in buckets.items()]


def topk_metrics(panel: pd.DataFrame, N: int, k=20, min_cross=10, cost_bps=5.0) -> dict:
    """每日取 score Top K 等权前瞻收益 vs 全样本均值超额 + Top−Bottom 多空 + **净成本年化**。

    净成本:多空组合每 N 日换一次,进出各扣一次、多空两腿 → 4×cost_bps 每轮。
    年化按非重叠持有 250/N 轮。反转类换手高,净成本这关最能证伪"看着能赚"。
    """
    col = f"r_{N}"
    top, allm, bot = [], [], []
    for _, g in panel.groupby("date"):
        if len(g) < min_cross:
            continue
        gs = g.sort_values("score", ascending=False)
        kk = min(k, len(gs) // 2)
        top.append(float(gs[col].head(kk).mean()))
        bot.append(float(gs[col].tail(kk).mean()))
        allm.append(float(g[col].mean()))
    if not top:
        return {"n日": 0}
    t, a, b = np.mean(top), np.mean(allm), np.mean(bot)
    ls_gross = t - b
    cost_round = 4 * cost_bps / 100.0                        # % per N日轮
    ls_net = ls_gross - cost_round
    ann_net = ls_net * (250.0 / N)
    return {"交易日": len(top), f"Top{k}均收益%": round(t, 2), "全样本均值%": round(a, 2),
            "Top超额%": round(t - a, 2), "多空Top-Bottom毛%": round(ls_gross, 2),
            "多空净%(每N日)": round(ls_net, 2), "多空年化净%": round(ann_net, 1)}


def _liq_filter(panel: pd.DataFrame, min_liq_pct: float) -> pd.DataFrame:
    """每个交易日剔除成交额分位 < min_liq_pct 的票(测反转 edge 在可交易票里还在不在)。"""
    if not min_liq_pct or "liq" not in panel.columns:
        return panel
    keep = panel.groupby("date")["liq"].transform(lambda s: s.rank(pct=True)) >= min_liq_pct
    return panel[keep]


def run(score="momentum", codes=None, horizons=(5, 10, 20), step=1, json_path=None,
        topk=20, min_liq_pct=0.0):
    scorer = _SCORERS[score]
    panel = build_rank_panel(codes, scorer, horizons, step=step)
    if panel.empty:
        print("!! panel 为空"); return
    used_stocks = int(panel.attrs.get("used", 0))
    if min_liq_pct:
        panel = _liq_filter(panel, min_liq_pct)
    res = {"打分": score, "流动性过滤分位": min_liq_pct, "样本股数": used_stocks,
           "总观测": int(len(panel)), "交易日数": int(panel["date"].nunique()), "免责": _DISCLAIMER}
    print(f"\n===== 排序型回测 · 打分={score} · 流动性过滤≥{min_liq_pct} · 样本 {used_stocks} 只 · "
          f"观测 {len(panel)} · {res['交易日数']} 个交易日 =====\n(非投资建议;横截面·无未来函数)\n")
    for N in horizons:
        ic = ic_metrics(panel, N)
        dec = decile_metrics(panel, N)
        tk = topk_metrics(panel, N, k=topk)
        res[f"{N}日"] = {"IC": ic, "分层": dec, "TopK": tk}
        print(f"—— {N} 交易日 ——")
        print(f"  [IC] " + "  ".join(f"{k}={v}" for k, v in ic.items()))
        d0 = dec[0]["均收益%"]; d9 = dec[9]["均收益%"]
        print(f"  [分层] 最低档 D0={d0}%  最高档 D9={d9}%  单调差(D9-D0)="
              f"{round((d9 - d0), 2) if (d0 is not None and d9 is not None) else None}pp")
        print(f"        各档: " + " ".join(f"{d['均收益%']}" for d in dec))
        print(f"  [TopK] " + "  ".join(f"{k}={v}" for k, v in tk.items()))
        print()
    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", choices=list(_SCORERS), default="momentum")
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--horizon", default="5,10,20")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--min-liq-pct", type=float, default=0.0, help="每日剔除成交额分位<此值的票(0~1)")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    codes = [c for c in a.codes.split(",") if c] or None
    if a.sample:
        allc = sorted(store.list_master_codes())
        import random
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    run(score=a.score, codes=codes, horizons=tuple(int(x) for x in a.horizon.split(",")),
        step=a.step, json_path=a.json or None, topk=a.topk, min_liq_pct=a.min_liq_pct)
