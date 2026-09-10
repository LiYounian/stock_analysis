"""tech_atr 提维 / 维内加权 A/B 实验(样本外 walk-forward)——2026-09-10 维度贡献研究 §6.B。

背景:维度贡献研究发现技术维内 tech_atr(波动率,全表最强单特征 IC≈0.08-0.10)被维内**等权**
的其余 9 个弱/符号不稳列稀释到 1/10 权重(广度里 br_limit_up 同理)。本脚本做 A/B:把 ATR/涨停广度
提权,看**多空收益价差 long_short 是否明显、稳定、显著地转正**(研究强调:命中提升若换不出多空收益
则意义有限;判据焦点是收益价差不是命中)。

变体(全部只改合成结构,**不引入新数据、不改生产 predictor.py**):
  B0_baseline    : 现状(技术维10列等权含atr)——生产口径
  B1_atr_dim     : tech_atr 提为独立"波动"维(权重=1,等同一整维),技术维剩9列
  B3_atr_br_dim  : tech_atr 提"波动"维 + br_limit_up 提"涨停"维(研究 §6 候选ii扩展)
  B2_ic_weighted : 技术维内按训练集|IC|加权(数据驱动,无手调参;IC 只用训练集→防未来函数)

防未来函数:变体只改"维内/维间怎么配权",定向/标准化/IC 权重一律**只用训练集**;
walk-forward 训练集严格早于测试日(复用 market_forecast_backtest.walk_forward)。

⚠️ 结论(见 docs/计划/2026-09-10_tech_atr提维加权_AB结论.md):**判据不达标,未接生产**。
非投资建议。

用法:
  python -m tools.backtest.market_forecast_atr_ab              # 跑 proxy/hs300 × h1/h5 全格
  python -m tools.backtest.market_forecast_atr_ab --out x.json --robust
"""
from __future__ import annotations

import argparse
import json
import logging
import math

import numpy as np
import pandas as pd

import tools.analysis.market_forecast.predictor as P
from tools.analysis.market_forecast import breadth as B
from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast.features import (
    _BREADTH_COLS, _FUNDFLOW_COLS, _SENTI_COLS, _TECH_COLS,
)
from tools.backtest import market_forecast_backtest as BT

logger = logging.getLogger("backtest.market_forecast_atr_ab")
CompositeModel = P.CompositeModel


# ————————————————————————— 变体模型 —————————————————————————
class B1_ATRDim(CompositeModel):
    """tech_atr 独立成'波动'维,权重=1(其余不变)。"""

    def __init__(self, cfg=None):
        super().__init__(cfg)
        tech_wo = [c for c in _TECH_COLS if c != "tech_atr"]
        self._groups = {"技术": tech_wo, "波动": ["tech_atr"],
                        "广度": _BREADTH_COLS, "消息面": _SENTI_COLS,
                        "资金流": _FUNDFLOW_COLS}
        self.group_w = {"技术": 1.0, "波动": 1.0, "广度": 1.0,
                        "消息面": 1.0, "资金流": 0.0}
        self.eff_group_w = dict(self.group_w)


class B3_ATR_BR_Dim(CompositeModel):
    """tech_atr→'波动'维、br_limit_up→'涨停'维,各权重=1。"""

    def __init__(self, cfg=None):
        super().__init__(cfg)
        tech_wo = [c for c in _TECH_COLS if c != "tech_atr"]
        br_wo = [c for c in _BREADTH_COLS if c != "br_limit_up"]
        self._groups = {"技术": tech_wo, "波动": ["tech_atr"],
                        "广度": br_wo, "涨停": ["br_limit_up"],
                        "消息面": _SENTI_COLS, "资金流": _FUNDFLOW_COLS}
        self.group_w = {"技术": 1.0, "波动": 1.0, "广度": 1.0, "涨停": 1.0,
                        "消息面": 1.0, "资金流": 0.0}
        self.eff_group_w = dict(self.group_w)


class B2_ICWeighted(CompositeModel):
    """技术维内按训练集 |rank-IC| 加权,其余维不变。IC 只用训练集→防未来函数。"""

    def fit(self, X, y):
        super().fit(X, y)  # 复用标准化/定向/覆盖率/校准
        Xs = self.scaler.transform(X[self.cols].to_numpy(dtype=float))
        col_idx = {c: i for i, c in enumerate(self.cols)}
        yv = np.asarray(y, dtype=float)
        yr = pd.Series(yv).rank().to_numpy()
        w = {}
        for c in _TECH_COLS:
            col = Xs[:, col_idx[c]]
            if np.std(col) < 1e-9:
                w[c] = 0.0
                continue
            cr = pd.Series(col).rank().to_numpy()
            if np.std(cr) < 1e-12 or np.std(yr) < 1e-12:
                w[c] = 0.0
                continue
            ic = float(np.corrcoef(cr, yr)[0, 1])
            w[c] = abs(ic) if ic == ic else 0.0
        s = sum(w.values()) or 1.0
        self._tech_w = {c: w[c] / s for c in _TECH_COLS}
        raw = self._composite_raw(Xs).reshape(-1, 1)   # 维内权重变了→重算校准
        self.calib.fit(raw, yv)
        return self

    def _dim_scores(self, Xs):
        col_idx = {c: i for i, c in enumerate(self.cols)}
        oriented = Xs * self.orient
        dims = {}
        for name, cols in self._groups.items():
            idx = [col_idx[c] for c in cols]
            if name == "技术" and getattr(self, "_tech_w", None):
                wv = np.array([self._tech_w[c] for c in cols])
                dims[name] = (oriented[:, idx] * wv).sum(axis=1)
            else:
                dims[name] = oriented[:, idx].mean(axis=1)
        return dims


VARIANTS = {
    "B0_baseline": CompositeModel,
    "B1_atr_dim": B1_ATRDim,
    "B3_atr_br_dim": B3_ATR_BR_Dim,
    "B2_ic_weighted": B2_ICWeighted,
}


# ————————————————————————— 多空价差显著性 —————————————————————————
def _norm_sf2(t: float) -> float:
    """双侧 p 的正态近似(样本大时 Welch t≈z);erfc 实现,无 scipy 依赖。"""
    return math.erfc(abs(t) / math.sqrt(2.0))


def ls_tstat(rec: pd.DataFrame):
    """多空价差 Welch t 检验:预测涨的日子 fwd vs 预测跌的日子 fwd。p 为正态近似。"""
    up = rec[rec["pred_dir"] > 0]["fwd_ret"].to_numpy()
    dn = rec[rec["pred_dir"] < 0]["fwd_ret"].to_numpy()
    if len(up) < 2 or len(dn) < 2:
        return None, None
    m1, m2 = up.mean(), dn.mean()
    se = math.sqrt(up.var(ddof=1) / len(up) + dn.var(ddof=1) / len(dn))
    if se < 1e-12:
        return None, None
    t = (m1 - m2) / se
    return float(t), float(_norm_sf2(t))


def _register_variants():
    for name, cls in VARIANTS.items():
        P.MODELS[name] = cls
        BT.P.MODELS[name] = cls


def _long_short(rec: pd.DataFrame):
    up = rec[rec["pred_dir"] > 0]["fwd_ret"]
    dn = rec[rec["pred_dir"] < 0]["fwd_ret"]
    if len(up) < 2 or len(dn) < 2:
        return None
    return float(up.mean() - dn.mean())


def run_all(stride: int = 5, robust: bool = False, data_root=None) -> dict:
    _register_variants()
    bdf = B.compute_breadth(data_root=data_root)
    rows, robust_rows = [], []
    for tgt in ("proxy", "hs300"):
        for h in (1, 5):
            panel = F.build_panel(target=tgt, horizon=h, breadth_df=bdf,
                                  data_root=data_root)
            for vname in VARIANTS:
                rec = BT.walk_forward(panel, model_name=vname, stride=stride)
                s = BT.score(rec)
                if "error" in s:
                    rows.append({"target": tgt, "h": h, "variant": vname,
                                 "error": s["error"]})
                    continue
                t, pval = ls_tstat(rec)
                rows.append({
                    "target": tgt, "h": h, "variant": vname,
                    "n": s["n_test"], "hit": s["hit_rate"],
                    "edge_inertia": s["edge_vs_inertia"],
                    "long_short": s["long_short_fwd"],
                    "ls_t": round(t, 3) if t is not None else None,
                    "ls_p": round(pval, 4) if pval is not None else None,
                    "mono": s["bucket_monotonic_spearman"],
                    "prob_ret_corr": s["prob_ret_corr"],
                })
                if robust and tgt == "proxy":
                    rc = rec.sort_values("date").reset_index(drop=True)
                    mid = len(rc) // 2
                    robust_rows.append({
                        "target": tgt, "h": h, "variant": vname,
                        "ls_full": _long_short(rc),
                        "ls_first_half": _long_short(rc.iloc[:mid]),
                        "ls_second_half": _long_short(rc.iloc[mid:]),
                    })
    return {"grid": rows, "robust": robust_rows}


def fmt_table(rows) -> str:
    out = []
    hdr = "| target | h | variant | n | hit | edge_inertia | long_short | ls_t | ls_p | mono |"
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    for tgt in ("proxy", "hs300"):
        for h in (1, 5):
            out += [f"\n### {tgt} h{h}\n", hdr, sep]
            for r in rows:
                if r["target"] != tgt or r["h"] != h:
                    continue
                if "error" in r:
                    out.append(f"| {tgt} | {h} | {r['variant']} | — | {r['error']} |")
                    continue
                out.append(f"| {tgt} | {h} | {r['variant']} | {r['n']} | {r['hit']} | "
                           f"{r['edge_inertia']} | {r['long_short']} | {r['ls_t']} | "
                           f"{r['ls_p']} | {r['mono']} |")
    return "\n".join(out)


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--robust", action="store_true", help="proxy 前后半段 long_short 稳定性")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    res = run_all(stride=a.stride, robust=a.robust, data_root=a.data_root)
    print(fmt_table(res["grid"]))
    if a.robust:
        print("\n### proxy long_short 稳定性(全 | 前半 | 后半)\n")
        for r in res["robust"]:
            f = lambda x: f"{x:+.5f}" if x is not None else "  None "
            print(f"  {r['target']} h{r['h']} {r['variant']:16s}: "
                  f"{f(r['ls_full'])} | {f(r['ls_first_half'])} | {f(r['ls_second_half'])}")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fp:
            json.dump(res, fp, ensure_ascii=False, indent=2)
        print(f"\n[saved] {a.out}")


if __name__ == "__main__":
    _main()
