"""大盘预测·效力漂移探针(drift_monitor)——只读事后验证,让"效力在不在掉"看得见。

架构设计 §5-P1(docs/计划/2026-09-13_选股系统架构设计_程序化与模型迁移.md):
market_forecast 的维间组权重是手工常量(strategy.py:370)、永不重训 = 固化点;要"实时进化"
第一步是让**效力漂移可见**。本模块只读历史已产出的预测 + 事后已实现收益,算滚动命中/校准/IC,
**不改 predictor、不改权重、不改生产数据**(纯只读探针,§5.4-P1 无风险回退)。

两种预测记录来源(同一套评分口径):
  · source="production" —— 读历史 `data/analysis/<date>/market_forecast.json` 里**真实产出**的
    p_up,配 as_of 之后的**已实现** fwd_ret(真·线上模型的事后验证;当前样本很少,见报告局限)。
  · source="backtest"   —— 复用 `tools.backtest.market_forecast_backtest.walk_forward` 的
    滚动重训样本外记录(同一模型离线重跑,历史长,用于看滚动趋势)。

复用而非另造:评分算子直接用 `market_forecast_backtest.score` / `_spearman`;记录 schema 与
walk_forward 对齐(date/p_up/pred_dir/real_dir/fwd_ret/mom1),故两来源可喂同一评分栈。

防未来函数(硬红线):
  · 每个预测日 d 只用 d 当天的 p_up + 其**之后**才实现的收益 fwd_ret[d]=close[d+h]/close[d]-1
    (close[d+h] 全部 ≥ d,合法的事后标签);T+h 尚未到期 → fwd_ret 为 NaN → 该日**不评分**(不编造)。
  · 滚动指标一律**只用截至当日的历史**(trailing window),绝不用更晚信息回改早先评分。

非投资建议:测试环境研究模拟。
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
from typing import Iterable

import numpy as np
import pandas as pd

from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import predictor as P
from tools.backtest import market_forecast_backtest as BT

logger = logging.getLogger("market_forecast.drift_monitor")

# 记录列(与 walk_forward 输出对齐,便于复用 BT.score / rolling)
REC_COLS = ["date", "p_up", "pred_dir", "real_dir", "fwd_ret", "mom1"]

# ————————————————————————— 报警阈值(建议默认,可 CLI 覆盖) —————————————————————————
# 语义见 docs/计划/2026-09-13_大盘预测漂移探针报告.md;此处为"该报警触发再标定"的判据。
DEFAULT_THRESHOLDS = {
    "roll_window": 60,        # 滚动窗口(记录条数);样本不足时自动退化为全样本
    "min_periods": 20,        # 滚动最小样本(不足不出滚动值,避免追噪声)
    "hit_floor": 0.50,        # 滚动命中率跌破 50%(不如抛硬币)→ 报警
    "edge_vs_inertia_floor": 0.0,   # 滚动命中相对惯性基线的边际跌破 0(不如"顺昨日")→ 报警
    "brier_ratio_ceiling": 1.10,    # 滚动 Brier / 气候基线 Brier > 1.10(比"永远报基础上涨率"实质更差)→ 报警
                                    # 注:本模型无经济 alpha,Brier 常≈气候基线(比值≈1);故阈值取 1.10 表"实质劣化",
                                    #     而非"未跑赢气候基线"(那几乎恒真、无判别力)。理由见漂移探针报告。
    "ic_floor": 0.0,          # 滚动 IC(p_up 与 fwd_ret 相关)转负 → 报警
}


# ————————————————————————— 生产预测记录加载 —————————————————————————
def load_production_forecasts(data_root=None,
                             targets: Iterable[str] = ("hs300", "proxy"),
                             horizons: Iterable[int] = (1, 5)) -> pd.DataFrame:
    """扫 `analysis/<date>/market_forecast.json`,抽 (as_of, target, horizon, p_up, direction)。

    只读历史产物,不触碰生产数据。返回长表(可能空)。
    """
    from tools.analysis.market_forecast.dataroot import analysis_dir, ensure_data_root
    root = ensure_data_root(str(data_root) if data_root else None)
    adir = analysis_dir(root)
    tset = set(targets)
    hset = {str(h) for h in horizons}
    rows = []
    for p in sorted(glob.glob(str(adir / "*" / "market_forecast.json"))):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            logger.warning("跳过无法解析的预测产物 %s: %r", p, e)
            continue
        as_of = d.get("as_of") or os.path.basename(os.path.dirname(p))
        for tgt, tval in (d.get("targets") or {}).items():
            if tgt not in tset or not isinstance(tval, dict):
                continue
            for h, hval in (tval.get("horizons") or {}).items():
                if h not in hset or not isinstance(hval, dict) or "p_up" not in hval:
                    continue
                rows.append({
                    "as_of": str(as_of)[:10],
                    "target": tgt,
                    "horizon": int(h),
                    "p_up": float(hval["p_up"]),
                    "direction": hval.get("direction"),
                    "src_file": p,
                })
    return pd.DataFrame(rows)


# ————————————————————————— 预测 ⋈ 事后已实现收益 —————————————————————————
def _realized_panel(target: str, horizon: int, data_root=None,
                    breadth_df=None, cfg=None) -> pd.DataFrame:
    """建面板取"事后已实现"标签(fwd_ret/direction/tech_mom1),index=date。

    复用 features.build_panel(与 backtest 同口径同防未来);fwd_ret[d] 用 close[d+h],
    尚未到期 → NaN(交给调用方过滤,绝不编造)。
    """
    panel = F.build_panel(target=target, horizon=horizon, data_root=data_root,
                          breadth_df=breadth_df, cfg=cfg)
    keep = ["fwd_ret", "direction"]
    if "tech_mom1" in panel.columns:
        keep.append("tech_mom1")
    out = panel[keep].copy()
    return out


def records_from_production(target: str, horizon: int, prod_df: pd.DataFrame | None = None,
                            data_root=None, breadth_df=None, cfg=None) -> pd.DataFrame:
    """真·线上预测的事后验证:生产 p_up ⋈ as_of 之后已实现的 fwd_ret。

    输出记录 schema 与 walk_forward 一致(REC_COLS),可直接喂 BT.score / rolling。
    防未来:as_of 的 fwd_ret 只由 close[as_of+h] 定(≥as_of);未到期→丢弃(不评分)。
    """
    if prod_df is None:
        prod_df = load_production_forecasts(data_root, targets=(target,), horizons=(horizon,))
    sub = prod_df[(prod_df["target"] == target) & (prod_df["horizon"] == horizon)].copy()
    if sub.empty:
        return pd.DataFrame(columns=REC_COLS)
    panel = _realized_panel(target, horizon, data_root=data_root,
                            breadth_df=breadth_df, cfg=cfg)
    pidx = pd.to_datetime(panel.index)
    panel = panel.copy()
    panel.index = pidx
    recs = []
    for _, r in sub.iterrows():
        d = pd.Timestamp(r["as_of"])
        if d not in panel.index:
            continue
        fwd = panel.loc[d, "fwd_ret"]
        if pd.isna(fwd):                       # T+h 尚未到期 → 不评分(防编造)
            continue
        mom1 = panel.loc[d, "tech_mom1"] if "tech_mom1" in panel.columns else np.nan
        p_up = float(r["p_up"])
        recs.append({
            "date": d,
            "p_up": p_up,
            "pred_dir": 1 if p_up >= 0.5 else -1,
            "real_dir": int(np.sign(fwd)) if fwd != 0 else 0,
            "fwd_ret": float(fwd),
            "mom1": float(mom1) if pd.notna(mom1) else np.nan,
        })
    out = pd.DataFrame(recs, columns=REC_COLS)
    if not out.empty:
        out = out.sort_values("date").reset_index(drop=True)
    return out


def records_from_backtest(target: str, horizon: int, model: str = "composite",
                          stride: int = 5, min_train=None, data_root=None,
                          breadth_df=None, cfg=None) -> pd.DataFrame:
    """离线滚动重训样本外记录(同一模型、历史长)——复用 walk_forward,不重写。"""
    panel = F.build_panel(target=target, horizon=horizon, data_root=data_root,
                          breadth_df=breadth_df, cfg=cfg)
    rec = BT.walk_forward(panel, model_name=model, min_train=min_train,
                          stride=stride, cfg=cfg)
    if rec.empty:
        return pd.DataFrame(columns=REC_COLS)
    for c in REC_COLS:
        if c not in rec.columns:
            rec[c] = np.nan
    return rec[REC_COLS].sort_values("date").reset_index(drop=True)


# ————————————————————————— 校准 / Brier —————————————————————————
def brier_score(rec: pd.DataFrame) -> float:
    """Brier = mean((p_up - y)^2),y=1 若已实现上涨(fwd_ret>0)。越小越准。"""
    if rec.empty:
        return float("nan")
    y = (rec["fwd_ret"] > 0).astype(float).to_numpy()
    p = rec["p_up"].to_numpy(dtype=float)
    return float(np.mean((p - y) ** 2))


def climatology_brier(rec: pd.DataFrame) -> float:
    """气候基线 Brier:恒报"基础上涨率 base_rate"的 Brier = base*(1-base)。

    作校准的诚实参照——模型 Brier 高于它 = 比"永远报历史上涨率"还差(负校准价值)。
    """
    if rec.empty:
        return float("nan")
    base = float((rec["fwd_ret"] > 0).mean())
    return float(base * (1.0 - base))


def calibration_table(rec: pd.DataFrame, edges=(0.35, 0.45, 0.55, 0.65)) -> list[dict]:
    """可靠性表:按 p_up 分档 → 各档 平均预测概率 vs 实际上涨率 vs 样本数。

    档口径复用 predictor.prob_to_bucket 的边界(与生产 prob_bucket 一致),便于对照。
    校准好 = 平均预测概率 ≈ 实际上涨率(逐档)。
    """
    if rec.empty:
        return []
    r = rec.copy()
    r["_b"] = r["p_up"].apply(P.prob_to_bucket)
    rows = []
    labels = list(range(len(edges) + 1))
    for b in labels:
        g = r[r["_b"] == b]
        if g.empty:
            continue
        rows.append({
            "bucket_idx": int(b),
            "bucket": P.prob_bucket_label(b),
            "n": int(len(g)),
            "mean_p_up": round(float(g["p_up"].mean()), 4),
            "emp_up_rate": round(float((g["fwd_ret"] > 0).mean()), 4),
            "calib_gap": round(float(g["p_up"].mean() - (g["fwd_ret"] > 0).mean()), 4),
        })
    return rows


# ————————————————————————— 滚动指标 —————————————————————————
def rolling_metrics(rec: pd.DataFrame, window: int = 60, min_periods: int = 20) -> pd.DataFrame:
    """trailing 滚动:命中率 / Brier / IC(p_up~fwd_ret 相关) / 相对50边际。

    严格 trailing(只用截至当日的历史,防未来);样本 < min_periods 的窗口不出值(NaN)。
    返回 index=date 的滚动指标表。
    """
    if rec.empty:
        return pd.DataFrame()
    r = rec.sort_values("date").reset_index(drop=True).copy()
    r["_hit"] = (r["pred_dir"] == r["real_dir"]).astype(float)
    r["_up"] = (r["fwd_ret"] > 0).astype(float)
    r["_sq"] = (r["p_up"] - r["_up"]) ** 2
    eff_win = window if len(r) >= window else len(r)
    mp = min(min_periods, len(r))
    roll = r.rolling(window=eff_win, min_periods=mp)

    def _roll_ic(idx_series):
        # rolling corr(p_up, fwd_ret);pandas rolling.corr 需成对,单独算
        out = pd.Series(index=r.index, dtype=float)
        for i in range(len(r)):
            lo = max(0, i - eff_win + 1)
            seg = r.iloc[lo:i + 1]
            if len(seg) < mp or seg["p_up"].std() == 0 or seg["fwd_ret"].std() == 0:
                out.iloc[i] = np.nan
            else:
                out.iloc[i] = float(np.corrcoef(seg["p_up"], seg["fwd_ret"])[0, 1])
        return out

    res = pd.DataFrame({
        "date": r["date"],
        "n_in_window": roll["_hit"].count().astype("Int64"),
        "roll_hit": roll["_hit"].mean().round(4),
        "roll_brier": roll["_sq"].mean().round(4),
        "roll_up_rate": roll["_up"].mean().round(4),
        "roll_ic": _roll_ic(None).round(4),
    })
    return res.reset_index(drop=True)


def _trend(series: pd.Series, higher_is_better: bool = True) -> dict:
    """滚动序列的趋势画像:首/末值 + 前半段 vs 后半段均值 + **效力**方向(在不在掉)。

    higher_is_better=False 用于 Brier 等"越小越好"指标:delta<0(误差降)= 效力走强。
    delta 仍是原始差值(后半-前半),仅"走弱/走强"标签按效力语义翻转。
    """
    s = series.dropna()
    if len(s) < 2:
        return {"n": int(len(s)), "trend": "样本不足", "first": None, "last": None,
                "first_half_mean": None, "second_half_mean": None, "delta": None,
                "higher_is_better": higher_is_better}
    half = len(s) // 2
    fh = float(s.iloc[:half].mean()) if half else float(s.iloc[0])
    sh = float(s.iloc[half:].mean())
    delta = round(sh - fh, 4)
    eff = delta if higher_is_better else -delta       # 效力增量(正=走强)
    trend = "走弱" if eff < 0 else ("走强" if eff > 0 else "持平")
    return {
        "higher_is_better": higher_is_better,
        "n": int(len(s)),
        "first": round(float(s.iloc[0]), 4),
        "last": round(float(s.iloc[-1]), 4),
        "first_half_mean": round(fh, 4),
        "second_half_mean": round(sh, 4),
        "delta": delta,
        "trend": trend,
    }


# ————————————————————————— 报警评估 —————————————————————————
def evaluate_alarms(overall: dict, rec: pd.DataFrame, roll: pd.DataFrame,
                    thresholds: dict | None = None) -> dict:
    """按阈值判"是否该触发再标定"。用滚动近端值(有则用最后一个有效滚动值,无则退全样本)。"""
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    alarms, details = [], {}

    def _last_valid(col):
        if not roll.empty and col in roll and roll[col].notna().any():
            return float(roll[col].dropna().iloc[-1])
        return None

    # 命中率地板
    hit = _last_valid("roll_hit")
    if hit is None:
        hit = overall.get("hit_rate")
    details["hit"] = hit
    if hit is not None and hit < t["hit_floor"]:
        alarms.append(f"命中率 {hit:.3f} 跌破地板 {t['hit_floor']}")

    # 相对惯性基线的边际
    edge_inertia = overall.get("edge_vs_inertia")
    details["edge_vs_inertia"] = edge_inertia
    if edge_inertia is not None and edge_inertia < t["edge_vs_inertia_floor"]:
        alarms.append(f"相对惯性边际 {edge_inertia:+.3f} 跌破 {t['edge_vs_inertia_floor']}")

    # Brier vs 气候基线
    bs = _last_valid("roll_brier")
    if bs is None:
        bs = brier_score(rec)
    clim = climatology_brier(rec)
    details["brier"] = bs
    details["climatology_brier"] = round(clim, 4) if clim == clim else None
    if bs == bs and clim == clim and clim > 0 and bs / clim > t["brier_ratio_ceiling"]:
        alarms.append(f"Brier {bs:.4f} / 气候基线 {clim:.4f} = {bs/clim:.2f} 超阈 {t['brier_ratio_ceiling']}")

    # IC 转负
    ic = _last_valid("roll_ic")
    if ic is None:
        ic = overall.get("prob_ret_corr")
    details["ic"] = ic
    if ic is not None and ic < t["ic_floor"]:
        alarms.append(f"IC {ic:+.4f} 跌破 {t['ic_floor']}")

    return {"triggered": bool(alarms), "reasons": alarms, "metrics": details, "thresholds": t}


# ————————————————————————— 主扫描 —————————————————————————
def drift_scan(source: str = "production",
               targets: Iterable[str] = ("hs300", "proxy"),
               horizons: Iterable[int] = (1, 5),
               window: int = 60, min_periods: int = 20,
               model: str = "composite", stride: int = 5,
               data_root=None, breadth_df=None, cfg=None,
               thresholds: dict | None = None) -> dict:
    """逐 (target,horizon) 出:样本外总分(复用 BT.score)+ 校准表 + 滚动趋势 + 报警。"""
    targets = list(targets)
    horizons = list(horizons)
    prod_df = None
    if source == "production":
        prod_df = load_production_forecasts(data_root, targets=targets, horizons=horizons)

    blocks = {}
    for tgt in targets:
        for h in horizons:
            key = f"{tgt}/T+{h}"
            try:
                if source == "production":
                    rec = records_from_production(tgt, h, prod_df=prod_df,
                                                  data_root=data_root,
                                                  breadth_df=breadth_df, cfg=cfg)
                elif source == "backtest":
                    rec = records_from_backtest(tgt, h, model=model, stride=stride,
                                                data_root=data_root,
                                                breadth_df=breadth_df, cfg=cfg)
                else:
                    raise ValueError(f"未知 source:{source!r}(production|backtest)")
            except Exception as e:
                blocks[key] = {"error": f"记录构建失败:{e!r}"}
                continue

            if rec.empty:
                blocks[key] = {"n": 0, "note": "无可评分记录(无产物/未到期/数据不足)"}
                continue

            overall = BT.score(rec)
            roll = rolling_metrics(rec, window=window, min_periods=min_periods)
            alarms = evaluate_alarms(overall, rec, roll, thresholds=thresholds)
            trends = {}
            _hib = {"roll_hit": True, "roll_ic": True, "roll_brier": False}
            for col in ("roll_hit", "roll_brier", "roll_ic"):
                if not roll.empty and col in roll:
                    trends[col] = _trend(roll[col], higher_is_better=_hib[col])
            blocks[key] = {
                "n": int(len(rec)),
                "date_range": f"{str(rec['date'].min())[:10]}..{str(rec['date'].max())[:10]}",
                "overall": overall,
                "brier": round(brier_score(rec), 4),
                "climatology_brier": round(climatology_brier(rec), 4),
                "calibration": calibration_table(rec),
                "rolling_trend": trends,
                "alarms": alarms,
            }

    return {
        "schema": "market_forecast_drift/v1",
        "source": source,
        "window": window,
        "min_periods": min_periods,
        "targets": targets,
        "horizons": horizons,
        "non_investment_advice": True,
        "note": ("只读事后验证探针,不改模型/权重/生产数据。source=production 为真·线上预测事后验证"
                 "(样本受历史产物数量限制);source=backtest 为同模型离线滚动重训(历史长,看趋势)。"),
        "blocks": blocks,
    }


def _main():
    ap = argparse.ArgumentParser(description="大盘预测效力漂移探针(只读)")
    ap.add_argument("--source", default="production", choices=["production", "backtest"],
                    help="production=事后验证线上产物;backtest=离线滚动重训(历史长)")
    ap.add_argument("--targets", default="hs300,proxy")
    ap.add_argument("--horizons", default="1,5")
    ap.add_argument("--window", type=int, default=DEFAULT_THRESHOLDS["roll_window"])
    ap.add_argument("--min-periods", type=int, default=DEFAULT_THRESHOLDS["min_periods"])
    ap.add_argument("--model", default="composite", choices=list(P.MODELS))
    ap.add_argument("--stride", type=int, default=5, help="backtest 重训步长")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--out", default=None, help="落盘路径;缺省只打印")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    rep = drift_scan(
        source=a.source,
        targets=[t.strip() for t in a.targets.split(",") if t.strip()],
        horizons=[int(h) for h in a.horizons.split(",") if h.strip()],
        window=a.window, min_periods=a.min_periods,
        model=a.model, stride=a.stride, data_root=a.data_root,
    )
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"[saved] {a.out}")


if __name__ == "__main__":
    _main()
