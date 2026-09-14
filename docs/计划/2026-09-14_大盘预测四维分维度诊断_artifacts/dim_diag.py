"""大盘预测四维·分维度 OOS 诊断(只读、不改模型)。

思路:复用 market_forecast 现有面板与 walk-forward 训练口径,但在每个样本外测试日
额外记录**四维各自的定向标准化分数**(用训练集拟合的 scaler/orient),据此离线算:
  · 每维单独:命中率 / IC(Pearson) / rank-IC(Spearman) / AUC / 多空价差 + N + 标准误
  · 维间相关矩阵(OOS 分数)、与"最优单维"及合成的对比(抵消证据)
  · 权重合理性:现配置(资金流=0) vs 等权 vs IC加权 vs 逻辑元模型
    —— 用 dev/holdout 时序切分(dev 拟合、holdout 评估),防在测试集调权重。

防未来函数:训练集严格早于测试日且标签到期(pos[t]+h < pos[d]);orient/scaler 只在训练集拟合。
非投资建议。用法: PY dim_diag.py --data-root <主仓data> --out <json>
"""
from __future__ import annotations
import argparse, json, sys, logging
import numpy as np, pandas as pd

logging.disable(logging.WARNING)
sys.path.insert(0, ".")
from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import predictor as P
from tools.analysis.market_forecast.features import (
    _TECH_COLS, _BREADTH_COLS, _SENTI_COLS, _FUNDFLOW_COLS, FEATURE_COLS,
)

DIMS = {"技术": _TECH_COLS, "广度": _BREADTH_COLS, "消息面": _SENTI_COLS, "资金流": _FUNDFLOW_COLS}


def _scaler_fit(X):
    mu = np.nanmean(X, axis=0); sd = np.nanstd(X, axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return mu, sd


def _scaler_tf(X, mu, sd, clip=4.0):
    z = np.nan_to_num((X - mu) / sd, nan=0.0)
    return np.clip(z, -clip, clip)


def record_dim_scores(panel, min_train=120, stride=5):
    """每个 OOS 测试日记录四维定向分数 + fwd/direction。返回 DataFrame。"""
    cols = FEATURE_COLS
    col_idx = {c: i for i, c in enumerate(cols)}
    feat_ok = panel[cols].notna().all(axis=1)
    lab_ok = panel["fwd_ret"].notna()
    idx_all = panel.index[feat_ok]
    pos = {d: i for i, d in enumerate(panel.index)}
    h = int(panel.attrs.get("horizon", 1))
    recs = []
    mu = sd = orient = None; last_fit = -10**9
    for d in idx_all:
        i = pos[d]
        train_mask = (panel.index < d) & feat_ok & lab_ok
        train_rows = [t for t in panel.index[train_mask] if pos[t] + h < i]
        if len(train_rows) < min_train:
            continue
        if mu is None or (i - last_fit) >= stride:
            Xtr = panel.loc[train_rows, cols].to_numpy(float)
            ytr = (panel.loc[train_rows, "fwd_ret"] > 0).astype(float).to_numpy()
            mu, sd = _scaler_fit(Xtr)
            Xs = _scaler_tf(Xtr, mu, sd)
            orient = np.zeros(Xs.shape[1])
            for j in range(Xs.shape[1]):
                c = Xs[:, j]
                if np.std(c) < 1e-9:
                    continue
                cc = np.corrcoef(c, ytr)[0, 1]
                orient[j] = np.sign(cc) if not np.isnan(cc) else 0.0
            last_fit = i
        fwd = panel.loc[d, "fwd_ret"]
        if pd.isna(fwd):
            continue
        x = panel.loc[[d], cols].to_numpy(float)
        xs = _scaler_tf(x, mu, sd)[0] * orient
        row = {"date": d, "fwd_ret": float(fwd),
               "direction": int(np.sign(fwd)) if fwd != 0 else 0}
        for dim, dcols in DIMS.items():
            idx = [col_idx[c] for c in dcols]
            row[dim] = float(xs[idx].mean())
            raw = x[0][idx]
            row[dim + "_avail"] = int(np.abs(raw).sum() > 1e-9)
        recs.append(row)
    return pd.DataFrame(recs)


def _auc(score, label_up):
    s = np.asarray(score); y = np.asarray(label_up)
    n1 = y.sum(); n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return np.nan
    r = pd.Series(s).rank().to_numpy()
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def dim_metrics(rec, dim, avail_only=True):
    r = rec.copy()
    if avail_only:
        r = r[r[dim + "_avail"] == 1]
    r = r[r["direction"] != 0]
    n = len(r)
    if n < 10:
        return {"n": n, "note": "样本不足(<10)"}
    s = r[dim].to_numpy(); fwd = r["fwd_ret"].to_numpy(); dirn = r["direction"].to_numpy()
    up = (dirn > 0).astype(int)
    nz = np.abs(s) > 1e-9
    hit = float((np.sign(s[nz]) == dirn[nz]).mean()) if nz.sum() else np.nan
    nhit = int(nz.sum())
    ic = float(np.corrcoef(s, fwd)[0, 1]) if np.std(s) > 0 else np.nan
    ric = float(np.corrcoef(pd.Series(s).rank(), pd.Series(fwd).rank())[0, 1]) if np.std(s) > 0 else np.nan
    auc = _auc(s, up)
    ls = float(fwd[s > 0].mean() - fwd[s < 0].mean()) if (s > 0).any() and (s < 0).any() else np.nan
    se_hit = float(np.sqrt(hit * (1 - hit) / nhit)) if nhit and not np.isnan(hit) else np.nan
    se_ic = float(1 / np.sqrt(n - 1))
    return {"n": n, "n_hit_days": nhit, "hit": round(hit, 4) if hit==hit else None,
            "hit_se": round(se_hit, 4) if se_hit==se_hit else None,
            "IC": round(ic, 4) if ic==ic else None, "IC_se": round(se_ic, 4),
            "rankIC": round(ric, 4) if ric==ric else None,
            "AUC": round(auc, 4) if auc==auc else None,
            "long_short_fwd": round(ls, 5) if ls==ls else None}


def corr_matrix(rec):
    r = rec[list(DIMS)].copy()
    return r.corr().round(3).to_dict()


def weight_tests(rec, horizon):
    r = rec[rec["direction"] != 0].reset_index(drop=True)
    n = len(r)
    if n < 40:
        return {"note": f"样本不足做权重切分(n={n})"}
    cut = int(n * 0.6)
    dev, hold = r.iloc[:cut], r.iloc[cut:]
    S_hold = hold[list(DIMS)].to_numpy()
    S_dev = dev[list(DIMS)].to_numpy()
    y_dev = (dev["direction"] > 0).astype(float).to_numpy()
    fwd_hold = hold["fwd_ret"].to_numpy(); dir_hold = hold["direction"].to_numpy()

    def eval_w(w):
        w = np.asarray(w, float)
        comp = S_hold @ w
        nz = np.abs(comp) > 1e-12
        hit = float((np.sign(comp[nz]) == dir_hold[nz]).mean()) if nz.sum() else np.nan
        ic = float(np.corrcoef(comp, fwd_hold)[0, 1]) if np.std(comp) > 0 else np.nan
        se = float(np.sqrt(hit*(1-hit)/nz.sum())) if nz.sum() and hit==hit else np.nan
        return {"weights": [round(float(x),3) for x in w], "holdout_hit": round(hit,4) if hit==hit else None,
                "holdout_hit_se": round(se,4) if se==se else None,
                "holdout_IC": round(ic,4) if ic==ic else None, "n_hold": int(nz.sum())}

    out = {}
    out["现配置(技1广1消1资0)"] = eval_w([1, 1, 1, 0])
    out["等权4维"] = eval_w([1, 1, 1, 1])
    ics = []
    for dim in DIMS:
        s = dev[dim].to_numpy(); f = dev["fwd_ret"].to_numpy()
        ics.append(max(0.0, np.corrcoef(s, f)[0, 1]) if np.std(s) > 0 else 0.0)
    ics = np.array(ics); wsum = ics.sum() or 1.0
    out["IC加权(dev拟合)"] = eval_w(ics / wsum)
    out["_dev_dim_IC"] = {d: round(float(v),4) for d, v in zip(DIMS, ics)}
    lr = P._LogReg(l2=1.0, lr=0.3, epochs=800)
    mu, sdv = S_dev.mean(0), np.where(S_dev.std(0) < 1e-9, 1.0, S_dev.std(0))
    lr.fit((S_dev - mu) / sdv, y_dev)
    p = lr.predict_proba((S_hold - mu) / sdv)
    nz = np.abs(p - 0.5) > 1e-9
    hit = float(((p[nz] >= .5).astype(int)*2-1 == dir_hold[nz]).mean())
    ic = float(np.corrcoef(p, fwd_hold)[0, 1])
    out["逻辑元模型(dev拟合)"] = {"weights_std": [round(float(x),3) for x in lr.w],
                                   "holdout_hit": round(hit,4),
                                   "holdout_hit_se": round(float(np.sqrt(hit*(1-hit)/nz.sum())),4),
                                   "holdout_IC": round(ic,4), "n_hold": int(nz.sum())}
    out["_split"] = {"n_total": n, "dev": cut, "holdout": n - cut,
                     "dev_range": [str(dev['date'].min())[:10], str(dev['date'].max())[:10]],
                     "hold_range": [str(hold['date'].min())[:10], str(hold['date'].max())[:10]]}
    return out


def run_one(target, horizon, data_root):
    panel = F.build_panel(target=target, horizon=horizon, data_root=data_root)
    rec = record_dim_scores(panel, min_train=int(P._CFG["最小训练样本"]))
    res = {"target": target, "horizon": horizon,
           "n_oos": len(rec),
           "range": [str(rec['date'].min())[:10], str(rec['date'].max())[:10]] if len(rec) else None,
           "per_dim_full": {d: dim_metrics(rec, d, avail_only=False) for d in DIMS},
           "per_dim_availonly": {d: dim_metrics(rec, d, avail_only=True) for d in DIMS},
           "dim_corr_matrix": corr_matrix(rec),
           "weight_tests": weight_tests(rec, horizon)}
    return res, rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="dim_diag_result.json")
    a = ap.parse_args()
    allres = {}
    for target in ["proxy", "hs300"]:
        for horizon in [1, 5]:
            key = f"{target}_T{horizon}"
            res, rec = run_one(target, horizon, a.data_root)
            allres[key] = res
            rec.to_csv(a.out.replace(".json", f"_{key}_records.csv"), index=False)
            print(f"[{key}] n_oos={res['n_oos']} range={res['range']}")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(allres, f, ensure_ascii=False, indent=2)
    print(f"[saved] {a.out}")


if __name__ == "__main__":
    main()
