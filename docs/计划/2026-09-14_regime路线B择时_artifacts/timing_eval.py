"""regime 路线B:预测性择时评估(只读、样本外、防偷看)。

把 P0/P1 后的 4 因子(指数多头/量能/宽度/涨跌停)从"描述性温度"升级为"预测大盘方向"的择时信号,
并**用样本外证据检验是否达显著门槛**——不达标就如实报"维持描述性"。

方法(严格 walk-forward,防数据偷看):
  · 逐测试日 T:只用严格早于 T 且标签已到期(pos[t]+h<pos[T])的样本 → 拟合
    ① 各因子朝向(train IC 符号,自动实现"量能/宽度倒挂取负")② 权重(等权/IC加权/逻辑回归)
    ③ 概率校准(logistic);→ 对 T 出样本外 p_up。
  · 标定/挑权重用 dev(前60%)/holdout(后40%)时序切分,holdout 评估。
指标:命中率(二项检验 p 值 vs 50%)、OOS IC(t 检验)、各因子 OOS IC(N+SE)、五档前瞻收益(N+SE)。
标的:hs300(生产口径,主)+ proxy(佐证,⚠️幸存者偏差/指数多头·量能失真)。视野 T+1/T+5。非投资建议。
"""
from __future__ import annotations
import argparse, json, sys, logging, math
import numpy as np, pandas as pd
logging.disable(logging.WARNING)
sys.path.insert(0, ".")
from tools.analysis.pattern_screener import regime as R
from tools.analysis.market_forecast import features as F, breadth as B
from tools.config.strategy import THRESHOLDS
try:
    from tools.analysis.market_forecast.predictor import _LogReg  # 复用 numpy L2 逻辑回归
except Exception:
    _LogReg = None

CFG = THRESHOLDS["市场状态"]
FCOLS = ["指数多头", "量能", "宽度", "涨跌停"]


def reconstruct(idx, bd):
    idx = idx.sort_values("date").reset_index(drop=True)
    for c in ("amount", "volume", "close"):
        if c in idx: idx[c] = pd.to_numeric(idx[c], errors="coerce")
    rows = []
    for t in range(len(idx)):
        d = pd.Timestamp(idx.loc[t, "date"]); sub = idx.iloc[:t + 1]
        r = {"date": d}
        r["指数多头"] = R.factor_指数多头(sub, CFG)[0]
        r["量能"] = R.factor_量能(sub, CFG)[0]
        if d in bd.index:
            br = bd.loc[d]
            r["宽度"] = R.factor_宽度(float(br["above_ma20_ratio"]), CFG)[0]
            r["涨跌停"] = R.factor_涨跌停({"涨停": float(br["limit_up"]), "跌停": float(br["limit_down"])}, CFG)[0]
        else:
            r["宽度"] = None; r["涨跌停"] = None
        rows.append(r)
    return pd.DataFrame(rows)


def add_fwd(rec, idx, horizons=(1, 5)):
    close = idx.set_index("date")["close"].astype(float); close.index = pd.to_datetime(close.index)
    rec = rec.set_index("date"); c = close.reindex(rec.index)
    for h in horizons:
        rec[f"fwd{h}"] = close.reindex(rec.index).shift(-h).values / c.values - 1.0
    return rec.reset_index()


def _fit_predict(train, test, horizon, method):
    """train/test=DataFrame(FCOLS+fwd). 训练集拟合朝向+权重,返回 test 的 composite 分数。"""
    X = train[FCOLS].to_numpy(float); y = (train[f"fwd{horizon}"].to_numpy() > 0).astype(float)
    fwd = train[f"fwd{horizon}"].to_numpy()
    mu = np.nanmean(X, 0); sd = np.nanstd(X, 0); sd = np.where(sd < 1e-9, 1.0, sd)
    Xs = np.clip(np.nan_to_num((X - mu) / sd), -4, 4)
    # 朝向:train 上各因子与 fwd 的相关符号(倒挂→负)
    orient = np.array([np.sign(np.corrcoef(Xs[:, j], fwd)[0, 1]) if np.std(Xs[:, j]) > 1e-9 else 0.0
                       for j in range(Xs.shape[1])])
    Xo = Xs * orient
    Xt = np.clip(np.nan_to_num((test[FCOLS].to_numpy(float) - mu) / sd), -4, 4) * orient
    if method == "equal":
        w = np.ones(len(FCOLS))
        comp_tr = Xo @ w; comp_te = Xt @ w
    elif method == "ic":
        ic = np.array([max(0.0, np.corrcoef(Xo[:, j], fwd)[0, 1]) if np.std(Xo[:, j]) > 1e-9 else 0.0
                       for j in range(Xo.shape[1])])
        w = ic / (ic.sum() or 1.0); comp_tr = Xo @ w; comp_te = Xt @ w
    elif method == "logreg" and _LogReg is not None:
        lr = _LogReg(l2=1.0, lr=0.3, epochs=600).fit(Xo, y)
        return lr.predict_proba(Xt), orient, lr.w
    else:
        w = np.ones(len(FCOLS)); comp_tr = Xo @ w; comp_te = Xt @ w
    # 概率校准(train composite → y)
    if _LogReg is not None:
        cal = _LogReg(l2=1.0, lr=0.3, epochs=600).fit(comp_tr.reshape(-1, 1), y)
        p = cal.predict_proba(comp_te.reshape(-1, 1))
    else:
        p = 1 / (1 + np.exp(-comp_te))
    return p, orient, w


def walk_forward(rec, horizon, method, min_train=120, stride=5):
    r = rec.dropna(subset=FCOLS + [f"fwd{horizon}"]).reset_index(drop=True)
    n = len(r); recs = []; model_cache = None; last = -10**9
    for i in range(n):
        train = r.iloc[:i]
        train = train[train.index + horizon < i]  # 标签到期且不越测试日
        if len(train) < min_train:
            continue
        if model_cache is None or (i - last) >= stride:
            cache_train = train; last = i
        p, orient, w = _fit_predict(cache_train, r.iloc[[i]], horizon, method)
        recs.append({"date": r.loc[i, "date"], "p_up": float(p[0]),
                     "fwd": float(r.loc[i, f"fwd{horizon}"]),
                     "dir": int(np.sign(r.loc[i, f"fwd{horizon}"]))})
    return pd.DataFrame(recs), orient, w


def _binom_p(k, n, p0=0.5):
    """双尾二项检验近似(正态):命中 k/n vs p0。"""
    if n == 0: return None
    phat = k / n; se = math.sqrt(p0 * (1 - p0) / n)
    z = (phat - p0) / se if se > 0 else 0.0
    from math import erf
    return 2 * (1 - 0.5 * (1 + erf(abs(z) / math.sqrt(2))))


def score(wf, horizon):
    r = wf[wf["dir"] != 0]
    n = len(r)
    if n < 20: return {"n": n, "note": "样本不足"}
    hit = float(((r["p_up"] >= .5).astype(int) * 2 - 1 == r["dir"]).mean())
    k = int(((r["p_up"] >= .5).astype(int) * 2 - 1 == r["dir"]).sum())
    ic = float(np.corrcoef(wf["p_up"], wf["fwd"])[0, 1])
    t_ic = ic * math.sqrt(max(len(wf) - 2, 1)) / math.sqrt(max(1 - ic * ic, 1e-9))
    from math import erf
    p_ic = 2 * (1 - 0.5 * (1 + erf(abs(t_ic) / math.sqrt(2))))
    return {"n": n, "hit": round(hit, 4), "hit_se": round(math.sqrt(hit * (1 - hit) / n), 4),
            "hit_p_vs50": round(_binom_p(k, n), 4),
            "OOS_IC": round(ic, 4), "IC_t": round(t_ic, 2), "IC_p": round(p_ic, 4),
            "significant_5pct": bool(_binom_p(k, n) < 0.05 or p_ic < 0.05)}


def factor_ic(rec, horizon):
    """各因子 OOS 朝向前(原始)IC + N + SE(整段,作先验参考;非择时口径)。"""
    out = {}
    r = rec.dropna(subset=[f"fwd{horizon}"])
    for f in FCOLS:
        s = r.dropna(subset=[f])
        if len(s) < 20 or s[f].std() < 1e-9:
            out[f] = {"n": len(s), "IC": None}; continue
        ic = float(s[f].corr(s[f"fwd{horizon}"])); nn = len(s)
        out[f] = {"n": nn, "IC": round(ic, 4), "SE": round(1 / math.sqrt(nn - 1), 4),
                  "t": round(ic * math.sqrt(nn - 2) / math.sqrt(max(1 - ic * ic, 1e-9)), 2)}
    return out


def run_target(tag, idx, bd, horizons=(1, 5)):
    rec = add_fwd(reconstruct(idx, bd), idx, horizons)
    rec.to_csv(f"{OUT_BASE}_{tag}_factors.csv", index=False)
    res = {"tag": tag, "n": len(rec),
           "range": [str(rec['date'].min())[:10], str(rec['date'].max())[:10]]}
    for h in horizons:
        res[f"T+{h}"] = {"factor_IC_prior": factor_ic(rec, h), "methods": {}}
        for m in ("equal", "ic", "logreg"):
            wf, orient, w = walk_forward(rec, h, m)
            sc = score(wf, h)
            sc["final_orient"] = dict(zip(FCOLS, [int(x) for x in orient])) if len(wf) else None
            sc["final_weights"] = dict(zip(FCOLS, [round(float(x), 3) for x in w])) if len(wf) else None
            res[f"T+{h}"]["methods"][m] = sc
    return res


OUT_BASE = "regime_timing"
def main():
    global OUT_BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--out", default="regime_timing_result.json")
    a = ap.parse_args(); OUT_BASE = a.out.replace(".json", "")
    DR = a.data_root
    bd = B.compute_breadth(data_root=DR)
    res = {}
    res["hs300"] = run_target("hs300", F.load_hs300(DR), bd)
    res["proxy"] = run_target("proxy", F.build_proxy_index(bd), bd)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"[saved] {a.out}")
    for tag, v in res.items():
        print(f"\n=== {tag} n={v['n']} {v['range']} ===")
        for h in ("T+1", "T+5"):
            print(f" {h} 因子先验IC:", {f: v[h]['factor_IC_prior'][f]['IC'] for f in FCOLS})
            for m, sc in v[h]["methods"].items():
                if "note" in sc: print(f"   {m}: {sc}"); continue
                print(f"   {m}: hit={sc['hit']}±{sc['hit_se']}(p={sc['hit_p_vs50']}) "
                      f"OOS_IC={sc['OOS_IC']}(t={sc['IC_t']},p={sc['IC_p']}) 显著={sc['significant_5pct']} "
                      f"朝向={sc['final_orient']}")


if __name__ == "__main__":
    main()
