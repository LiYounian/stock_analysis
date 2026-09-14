"""市场状态 regime(大盘冷热)· 历史重建 + 预测力/标定诊断(只读、不改模型)。

复用生产口径 pattern_screener.regime 的因子函数(不另造),按 as-of 因果切片逐日重建:
  · 指数多头 / 量能 —— 用 hs300 真指数 idx.iloc[:t+1](量能需 amount)
  · 涨跌停          —— 用 breadth 当日 limit_up/limit_down 家数(生产里被硬编码 None→降级,此处复活以评估其价值)
  · 宽度            —— 用 breadth above_ma20_ratio 作达标占比代理 / 参考占比(诊断占位参数是否饱和)
  · 科技共振        —— 龙头池空→无法重建(生产恒降级),标注为 dead
逐日算情绪分(有效因子平权×100)+ 五档 → 记录 hs300 前瞻收益 T+1/T+5/T+20。

诊断:①各因子 liveness(方差/是否饱和常数)②情绪分/五档分布 vs 现占位边界(标定)
③预测力:corr(情绪分,fwd)、各档平均前瞻收益(单调/反转?)、各因子 IC ④长样本(2018+)对
涨跌停/宽度两个 breadth 因子 vs proxy 前瞻收益(幸存者偏差 caveat)补充功效。

防未来函数:情绪分只用 ≤T 信息(MA/量能 trailing、当日家数);前瞻收益是标签(合法)。非投资建议。
"""
from __future__ import annotations
import argparse, json, sys, logging
import numpy as np, pandas as pd
logging.disable(logging.WARNING)
sys.path.insert(0, ".")
from tools.analysis.pattern_screener import regime as R
from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import breadth as B
from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["市场状态"]
FACTORS = ["指数多头", "科技共振", "量能", "宽度", "涨跌停"]


def reconstruct(idx: pd.DataFrame, bd: pd.DataFrame, use_breadth_factors=True):
    """逐日按 as-of 重建因子子分 + 情绪分 + 五档。idx=指数(date列),bd=breadth(index=date)。"""
    idx = idx.sort_values("date").reset_index(drop=True)
    for c in ("amount", "volume", "open", "high", "low", "close"):
        if c in idx.columns:
            idx[c] = pd.to_numeric(idx[c], errors="coerce")  # None→NaN(生产因子只 if x==x 过滤NaN)
    rows = []
    for t in range(len(idx)):
        d = pd.Timestamp(idx.loc[t, "date"])
        sub = idx.iloc[:t + 1]
        subs = {}
        # 指数多头 / 量能:生产口径直接用切片
        subs["指数多头"] = R.factor_指数多头(sub, _CFG)[0]
        subs["量能"] = R.factor_量能(sub, _CFG)[0]
        subs["科技共振"] = None                     # 龙头池空,生产恒降级
        # 涨跌停 / 宽度:用 breadth 当日值
        if use_breadth_factors and d in bd.index:
            br = bd.loc[d]
            subs["涨跌停"] = R.factor_涨跌停(
                {"涨停": float(br["limit_up"]), "跌停": float(br["limit_down"])}, _CFG)[0]
            subs["宽度"] = R.factor_宽度(float(br["above_ma20_ratio"]), _CFG)[0]
        else:
            subs["涨跌停"] = None; subs["宽度"] = None
        avail = [v for v in subs.values() if v is not None]
        score = round(sum(avail) / len(avail) * 100, 2) if avail else np.nan
        label = R.label_of(score, _CFG) if score == score else None
        row = {"date": d, "情绪分": score, "标签": label, "有效因子数": len(avail)}
        for f in FACTORS:
            row["f_" + f] = subs[f]
        rows.append(row)
    return pd.DataFrame(rows)


def add_fwd(rec, idx, horizons=(1, 5, 20)):
    close = idx.set_index("date")["close"].astype(float)
    close.index = pd.to_datetime(close.index)
    rec = rec.set_index("date")
    c = close.reindex(rec.index)
    for h in horizons:
        rec[f"fwd{h}"] = close.reindex(rec.index).shift(-h).values / c.values - 1.0
    return rec.reset_index()


def diagnose(rec, horizons=(1, 5, 20), tag=""):
    out = {"tag": tag, "n": len(rec),
           "range": [str(rec['date'].min())[:10], str(rec['date'].max())[:10]]}
    # 因子 liveness
    live = {}
    for f in FACTORS:
        col = rec["f_" + f]
        v = col.dropna()
        live[f] = {"avail_rate": round(col.notna().mean(), 3),
                   "std": round(float(v.std()), 4) if len(v) else None,
                   "mean": round(float(v.mean()), 4) if len(v) else None,
                   "nunique": int(v.round(3).nunique()) if len(v) else 0}
    out["factor_liveness"] = live
    # 情绪分分布 + 五档计数
    s = rec["情绪分"].dropna()
    out["score_dist"] = {"mean": round(float(s.mean()), 2), "std": round(float(s.std()), 2),
                         "min": round(float(s.min()), 2), "max": round(float(s.max()), 2),
                         "q": {q: round(float(s.quantile(q)), 2) for q in (.1, .25, .5, .75, .9)}}
    out["tier_counts"] = rec["标签"].value_counts().to_dict()
    out["有效因子数分布"] = rec["有效因子数"].value_counts().sort_index().to_dict()
    # 预测力:情绪分 vs fwd + 各因子 IC + 五档平均前瞻
    pred = {}
    for h in horizons:
        col = f"fwd{h}"
        r = rec[[col, "情绪分", "标签"] + ["f_" + f for f in FACTORS]].dropna(subset=[col])
        n = len(r)
        if n < 20:
            pred[f"T+{h}"] = {"n": n, "note": "样本不足"}; continue
        ic_score = float(r["情绪分"].corr(r[col])) if r["情绪分"].std() > 0 else None
        # 方向命中:情绪分>中位 → 预测涨
        med = r["情绪分"].median()
        pred_up = (r["情绪分"] > med)
        real_up = r[col] > 0
        hit = float((pred_up == real_up).mean())
        # 各因子 IC
        fic = {}
        for f in FACTORS:
            cc = r["f_" + f]
            fic[f] = round(float(cc.corr(r[col])), 4) if cc.std() > 0 and cc.notna().sum() > 20 else None
        # 五档平均前瞻收益(反转 or 趋势?)
        tier_fwd = {k: round(float(v), 5) for k, v in r.groupby("标签")[col].mean().items()}
        tier_n = r.groupby("标签")[col].size().to_dict()
        se = float(np.sqrt(hit * (1 - hit) / n))
        pred[f"T+{h}"] = {"n": n, "IC_情绪分": round(ic_score, 4) if ic_score is not None else None,
                          "hit_vs_median": round(hit, 4), "hit_se": round(se, 4),
                          "factor_IC": fic, "tier_mean_fwd": tier_fwd, "tier_n": tier_n}
    out["predictive"] = pred
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="regime_diag_result.json")
    a = ap.parse_args()
    DR = a.data_root
    res = {}
    # ① hs300 真指数窗口(全因子可重建,短样本)
    idx = F.load_hs300(DR)
    bd = B.compute_breadth(data_root=DR)
    rec_hs = add_fwd(reconstruct(idx, bd), idx)
    rec_hs.to_csv(a.out.replace(".json", "_hs300_records.csv"), index=False)
    res["hs300_full5factor"] = diagnose(rec_hs, tag="hs300 全因子重建(指数多头/量能/涨跌停/宽度;科技共振dead)")
    # ② 长样本 proxy(2018+):只为 涨跌停/宽度 两个 breadth 因子补功效(指数多头/量能在proxy失真→不解读)
    proxy = F.build_proxy_index(bd)
    rec_px = add_fwd(reconstruct(proxy, bd), proxy)
    rec_px.to_csv(a.out.replace(".json", "_proxy_records.csv"), index=False)
    res["proxy_long_caveat"] = diagnose(rec_px, tag="proxy长样本(⚠️幸存者偏差+指数多头/量能失真,只读涨跌停/宽度IC)")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"[saved] {a.out}")
    # 摘要打印
    for k, v in res.items():
        print(f"\n=== {k} | n={v['n']} {v['range']} ===")
        print("factor_liveness:", json.dumps(v["factor_liveness"], ensure_ascii=False))
        print("tier_counts:", v["tier_counts"], "| 有效因子数:", v["有效因子数分布"])
        print("score_dist:", v["score_dist"])
        for h, p in v["predictive"].items():
            if "note" in p: continue
            print(f"  {h}: IC情绪分={p['IC_情绪分']} hit={p['hit_vs_median']}±{p['hit_se']} 因子IC={p['factor_IC']}")
            print(f"       五档前瞻={p['tier_mean_fwd']} (n={p['tier_n']})")


if __name__ == "__main__":
    main()
