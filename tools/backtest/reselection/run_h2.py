"""H2 驱动:gap-fade 是「入场纪律(怎么买)」还是该「降权(要不要选)」?

对 winner_rate>99 的强势票(S05 C1∧C2∧C3 通过 + 筹码获利比例 as-of D):比
  (a) 被 gap-fade 当降权丢弃 —— proxy = 追高开(open 口径)买入并持有到 D+2;
  (b) 用回踩限价(limit_pc_0.01=昨收×0.99)买入并持有到 D+2。
KPI = 绝对净收益(扣 10bps)。判据:若 (b) 绝对收益显著优于 (a),则 gap-fade 应回退为
「入场提示(怎么买·回踩限价)」、不该降权(怎么选)。

复用:backtest_strong(C1/C2/C3 向量化 + chip_at winner_rate as-of)、nextday_kernel(成交/成本)、
data.build_market(全A等权 close→close 基准)。防未来:C123/wr 只用 ≤D;前瞻仅作标签。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest import backtest_strong as S
from tools.research.selection_alpha import nextday_kernel as K
from tools.backtest.reselection import data as D

logger = logging.getLogger("backtest.reselection.run_h2")
DEFAULT_ROOT = "/Users/yqg/Documents/projects/stock_analysis"

WR_BUCKETS = [("wr>99", 99.0, 1e9), ("95-99", 95.0, 99.0),
              ("80-95", 80.0, 95.0), ("≤80", -1.0, 80.0)]


def _bucket(wr):
    if wr is None or not np.isfinite(wr):
        return None
    for name, lo, hi in WR_BUCKETS:
        if lo < wr <= hi:
            return name
    return None


def _cluster_t(vals, clusters):
    v = np.asarray(vals, float); c = np.asarray(clusters)
    m = np.isfinite(v); v, c = v[m], c[m]
    if len(v) < 3:
        return {"n": int(len(v)), "mean": None, "cluster_t": None}
    mu = float(v.mean()); u = v - mu
    g = {}
    for ui, gi in zip(u, c):
        g[gi] = g.get(gi, 0.0) + ui
    G = len(g); corr = G / (G - 1) if G > 1 else 1.0
    var = corr * sum(s * s for s in g.values()) / (len(v) ** 2)
    t = mu / np.sqrt(var) if var > 0 else None
    return {"n": int(len(v)), "n_clusters": int(G), "mean": round(mu, 6),
            "cluster_t": round(float(t), 3) if t else None}


def run(data_root: str, start: str, end: str, cost_bps: float, json_path: str | None,
        limit: int | None = None):
    print("\n===== H2 gap-fade:入场纪律 vs 降权(wr>99 强势票·绝对收益 Model A)=====")
    print(f"(区间={start}~{end} 持有 D+1→D+2 成本={cost_bps}bps;⚠️ 非投资建议;防未来:C123/wr 只用 ≤D)\n")
    # 1) 全A等权 close→close 基准
    feats_m = D.build_feats(data_root, D.universe_codes(data_root), f"{int(start[:4]) - 1}-06-01",
                            with_momentum=False)
    mkt = D.build_market(feats_m)
    mkt_cc = mkt["mkt_cc"]
    del feats_m
    # 2) 逐票找 C1∧C2∧C3 日 + wr,构事件
    logging.getLogger("collectors.chip").setLevel(logging.ERROR)
    codes = S.universe_codes(data_root)
    if limit:
        codes = codes[:limit]
    periods = [int(p) for p in S._CFG["均线多头周期"]]
    need = int(S._CFG["最少历史根数"])
    rows = []
    n_stocks = 0
    for code in codes:
        df = S.load_kline(data_root, code)
        if df is None or len(df) < need:
            continue
        feat = S.precompute_features(df, periods, horizons=(1, 2))
        c1, c2, c3, _ = S.cond123(feat, periods)
        sel = c1 & c2 & c3
        dates = feat["dates"]; close = feat["close"]
        o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
        lo = df["low"].to_numpy(float); c = close
        n = len(c)
        n_stocks += 1
        for t in np.nonzero(sel)[0]:
            if t < 1 or t + 2 >= n:      # 需 D-... 及 D+1,D+2
                continue
            dstr = dates[t]
            if dstr < start or dstr > end:
                continue
            close_D = c[t]
            o1, h1, l1, c1d = o[t + 1], h[t + 1], lo[t + 1], c[t + 1]
            c2d = c[t + 2]
            if not (o1 > 0) or not np.isfinite(c2d) or close_D <= 0:
                continue
            wr, _cost95 = S.chip_at(df, dstr)
            bkt = _bucket(wr)
            if bkt is None:
                continue
            unbuy = bool(K.limit_up_unbuyable(code, np.array([close_D]), np.array([o1]))[0])
            bench = (1.0 + mkt_cc.get(dates[t + 1], 0.0)) * (1.0 + mkt_cc.get(dates[t + 2], 0.0)) - 1.0
            for rule in ("limit_pc_0.01", "open"):
                if rule == "open":
                    P = o1
                else:
                    P = close_D * 0.99
                fill, ret, filled = K.fill_and_return(
                    np.array([P]), np.array([o1]), np.array([h1]), np.array([l1]),
                    np.array([c2d]), "marketable")   # 持有到 D+2 收盘:卖价=c2d
                f0 = bool(filled[0])
                if not f0:
                    rows.append(dict(code=code, D=dstr, wr=wr, bucket=bkt, rule=rule,
                                     filled=False, unbuyable=unbuy, net=np.nan, alpha_net=np.nan))
                    continue
                fillp = float(fill[0])
                g = c2d / fillp - 1.0
                net = (1.0 + g) * (1.0 - cost_bps / 1e4) - 1.0
                rows.append(dict(code=code, D=dstr, wr=wr, bucket=bkt, rule=rule,
                                 filled=True, unbuyable=unbuy, net=net, alpha_net=net - bench))
    ev = pd.DataFrame(rows)
    print(f"—— 事件 {len(ev)} 条(C1∧C2∧C3 通过·{n_stocks} 票·含 limit/open 两档)——")
    res = {"config": dict(start=start, end=end, cost_bps=cost_bps, n_stocks=n_stocks,
                          n_events=int(len(ev))),
           "预注册": {"强势票口径": "S05 C1∧C2∧C3(六均线多头+近期连涨+高位区间)",
                    "wr分档": [b[0] for b in WR_BUCKETS],
                    "判据": "wr>99 桶 (b)limit 绝对净收益显著 > (a)open,且 (b)>0 → gap-fade 应改为入场提示、不该降权"},
           "免责": "历史回测≠未来保证,非投资建议。"}
    table = {}
    for bkt, _lo, _hi in WR_BUCKETS:
        sub = ev[(ev["bucket"] == bkt) & (ev["filled"])]
        cell = {}
        for rule in ("limit_pc_0.01", "open"):
            r = sub[sub["rule"] == rule]
            net = r["net"].to_numpy(float)
            cell[rule] = {
                "n": int(len(r)),
                "fill_rate": None,
                "mean_net": round(float(net.mean()), 6) if len(net) else None,
                "win_rate": round(float((net > 0).mean()), 4) if len(net) else None,
                "mean_alpha_net": round(float(r["alpha_net"].mean()), 6) if len(r) else None,
                "cluster_t_net": _cluster_t(net, r["D"].to_numpy())["cluster_t"],
            }
        # 成交率(含未触发):以该桶该 rule 全事件为分母
        for rule in ("limit_pc_0.01", "open"):
            allr = ev[(ev["bucket"] == bkt) & (ev["rule"] == rule)]
            if len(allr):
                cell[rule]["fill_rate"] = round(float(allr["filled"].mean()), 4)
        # (b)−(a) 差(limit − open),按 D 聚类(只取两档都成交的同一 (code,D))
        merged = sub.pivot_table(index=["code", "D"], columns="rule", values="net")
        if {"limit_pc_0.01", "open"}.issubset(merged.columns):
            paired = merged.dropna(subset=["limit_pc_0.01", "open"])
            diff = (paired["limit_pc_0.01"] - paired["open"]).to_numpy()
            Ds = [idx[1] for idx in paired.index]
            cell["diff_limit_minus_open"] = _cluster_t(diff, np.array(Ds))
        table[bkt] = cell
        c = cell
        print(f"  [{bkt}] limit: n={c['limit_pc_0.01']['n']} mean_net={c['limit_pc_0.01']['mean_net']} "
              f"win={c['limit_pc_0.01']['win_rate']} fill={c['limit_pc_0.01']['fill_rate']} "
              f"clt={c['limit_pc_0.01']['cluster_t_net']} | open: mean_net={c['open']['mean_net']} "
              f"win={c['open']['win_rate']} clt={c['open']['cluster_t_net']} | "
              f"limit−open diff={c.get('diff_limit_minus_open', {}).get('mean')} "
              f"clt={c.get('diff_limit_minus_open', {}).get('cluster_t')}")
    res["by_bucket"] = table
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default=DEFAULT_ROOT)
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    run(a.data_root, a.start, a.end, a.cost_bps, a.json or None, limit=a.limit or None)
