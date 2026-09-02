"""大盘预测 v0.5 · 前向(walk-forward)回测——验证方向判别力 vs 基线。

复用 forward_scorecard 的"信号只用 ≤T、标签用 T+h"前向思路,但这里是**滚动重训 + 样本外预测**:
对每个测试日 T,只用严格早于 T 的样本训练模型 → 预测 T 的 T+h 方向概率 → 攒样本外记录。
再统计:
  · 方向命中率 vs 基线(50% / 多数类 / 惯性(昨日方向))
  · 分档单调性(按预测 P(上涨) 五分位 → 各组实际前瞻收益是否递增;Spearman)
  · 多空价差(预测涨的日子 − 预测跌的日子 的平均前瞻收益)
  · 覆盖年份 / 样本数

用法:
  python -m tools.backtest.market_forecast_backtest --target proxy --horizon 1 --model composite
  python -m tools.backtest.market_forecast_backtest --target hs300 --horizon 5 --stride 3 --out x.json
非投资建议。防未来函数:训练集严格早于测试日;标签为未来收益(合法)。
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import predictor as P

logger = logging.getLogger("backtest.market_forecast")


def _spearman(a, b) -> float:
    a = pd.Series(a).rank()
    b = pd.Series(b).rank()
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def walk_forward(panel: pd.DataFrame, model_name: str = "composite",
                 min_train: int | None = None, stride: int = 5,
                 cfg=None) -> pd.DataFrame:
    """滚动重训 + 样本外预测。返回逐测试日记录(date, p_up, pred_dir, real_dir, fwd_ret)。"""
    cfg = cfg or P._CFG
    min_train = min_train or int(cfg["最小训练样本"])
    cols = P.FEATURE_COLS
    # 只留特征齐全 + 有标签的行做训练;测试日只需特征齐全
    feat_ok = panel[cols].notna().all(axis=1)
    lab_ok = panel["fwd_ret"].notna()
    idx_all = panel.index[feat_ok]                      # 可预测的日子(特征齐)
    recs = []
    model = None
    last_fit = -10**9
    ModelCls = P.MODELS[model_name]

    pos = {d: i for i, d in enumerate(panel.index)}
    for d in idx_all:
        i = pos[d]
        # 训练集:严格早于 d、特征齐、标签齐(标签到期,即其 T+h ≤ d-1 也自动满足因 fwd 落在过去)
        train_mask = (panel.index < d) & feat_ok & lab_ok
        # 且训练样本的 fwd 窗口不能覆盖到 d 之后(标签用未来但不能用测试日之后信息):
        # fwd_ret 在行 t 用 close[t+h];要求 t+h < i 才是"已实现且不越过测试日"。
        h = int(panel.attrs.get("horizon", 1))
        train_rows = [t for t in panel.index[train_mask] if pos[t] + h < i]
        if len(train_rows) < min_train:
            continue
        if model is None or (i - last_fit) >= stride:
            Xtr = panel.loc[train_rows]
            ytr = (panel.loc[train_rows, "fwd_ret"] > 0).astype(float).to_numpy()
            model = ModelCls(cfg).fit(Xtr, ytr)
            last_fit = i
        p_up = float(model.predict_proba(panel.loc[[d]])[0])
        fwd = panel.loc[d, "fwd_ret"]
        if pd.isna(fwd):                                # 测试日尚未到期 → 无法评分(留给生产预测)
            continue
        recs.append({
            "date": d, "p_up": p_up,
            "pred_dir": 1 if p_up >= 0.5 else -1,
            "real_dir": int(np.sign(fwd)) if fwd != 0 else 0,
            "fwd_ret": float(fwd),
            "mom1": float(panel.loc[d, "tech_mom1"]) if "tech_mom1" in panel.columns else np.nan,
        })
    return pd.DataFrame(recs)


def score(rec: pd.DataFrame, n_buckets: int = 5) -> dict:
    """记录 → 回测指标字典。"""
    if rec.empty:
        return {"error": "无样本外记录(训练样本不足或数据太短)"}
    r = rec[rec["real_dir"] != 0].copy()               # 剔除平盘日(方向不可评)
    n = len(r)
    hit = float((r["pred_dir"] == r["real_dir"]).mean())
    up_rate = float((r["real_dir"] > 0).mean())
    majority = max(up_rate, 1 - up_rate)               # 多数类基线
    # 惯性基线:预测方向=昨日指数涨跌(mom1 符号)
    inertia_dir = np.sign(r["mom1"]).replace(0, 1)
    inertia_hit = float((inertia_dir == r["real_dir"]).mean())
    # 多空价差
    up_days = rec[rec["pred_dir"] > 0]["fwd_ret"]
    dn_days = rec[rec["pred_dir"] < 0]["fwd_ret"]
    ls = float(up_days.mean() - dn_days.mean()) if len(up_days) and len(dn_days) else float("nan")
    # 分档单调性:按 p_up 分位 → 各组平均前瞻收益
    q = pd.qcut(rec["p_up"].rank(method="first"), n_buckets, labels=False)
    grp = rec.groupby(q)["fwd_ret"].mean()
    mono = _spearman(grp.index.to_numpy(), grp.to_numpy())
    corr = float(np.corrcoef(rec["p_up"], rec["fwd_ret"])[0, 1]) if len(rec) > 2 else float("nan")
    return {
        "n_test": int(n), "n_all": int(len(rec)),
        "years": f"{str(rec['date'].min())[:10]}..{str(rec['date'].max())[:10]}",
        "hit_rate": round(hit, 4),
        "baseline_50": 0.5,
        "baseline_majority": round(majority, 4),
        "baseline_inertia": round(inertia_hit, 4),
        "edge_vs_50": round(hit - 0.5, 4),
        "edge_vs_majority": round(hit - majority, 4),
        "edge_vs_inertia": round(hit - inertia_hit, 4),
        "long_short_fwd": round(ls, 5) if ls == ls else None,
        "bucket_mean_fwd": {int(k): round(float(v), 5) for k, v in grp.items()},
        "bucket_monotonic_spearman": round(mono, 4) if mono == mono else None,
        "prob_ret_corr": round(corr, 4) if corr == corr else None,
    }


def run(target="proxy", horizon=1, model="composite", stride=5,
        min_train=None, data_root=None, breadth_df=None, cfg=None,
        include_fundflow=True) -> dict:
    panel = F.build_panel(target=target, horizon=horizon, data_root=data_root,
                          breadth_df=breadth_df, cfg=cfg,
                          include_fundflow=include_fundflow)
    rec = walk_forward(panel, model_name=model, min_train=min_train, stride=stride, cfg=cfg)
    s = score(rec)
    s["target"], s["horizon"], s["model"] = target, horizon, model
    s["include_fundflow"] = include_fundflow
    return {"summary": s, "records": rec}


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="proxy", choices=["proxy", "hs300"])
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--model", default="composite", choices=list(P.MODELS))
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--min-train", type=int, default=None)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--no-fundflow", action="store_true",
                    help="关闭资金流维(=v0.5 三维,A/B 回测的 A 组)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = run(target=a.target, horizon=a.horizon, model=a.model, stride=a.stride,
              min_train=a.min_train, data_root=a.data_root,
              include_fundflow=not a.no_fundflow)
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out["summary"], f, ensure_ascii=False, indent=2)
        print(f"[saved] {a.out}")


if __name__ == "__main__":
    _main()
