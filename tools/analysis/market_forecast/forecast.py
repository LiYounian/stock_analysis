"""每日大盘预测产出(market_forecast.json)——把预测器接到生产。

给定特征截止日 T(收盘后),用**严格早于 T 且标签已到期**的样本训练预测器,对 T 预测其
T+1 / T+5 涨跌方向概率 + 五档 + 各维贡献,落 data/analysis/<T>/market_forecast.json。

schema(market_forecast/v0.5)见 build_forecast 返回。防未来函数:训练样本标签窗口需在 T 之前收口。
选股任务读它做 β 基准、算个股 α(设计对接口,详见 docs/计划/大盘预测策略.md §4"接入选股")。
非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from tools.analysis.market_forecast import breadth as B
from tools.analysis.market_forecast import features as F
from tools.analysis.market_forecast import predictor as P

logger = logging.getLogger("market_forecast.forecast")

_TARGET_NAME = {"hs300": "沪深300", "proxy": "全A等权代理指数"}


def _direction(p_up: float) -> str:
    if p_up >= 0.55:
        return "偏多"
    if p_up <= 0.45:
        return "偏空"
    return "震荡"


def _fit_predict_asof(panel: pd.DataFrame, as_of: pd.Timestamp, horizon: int,
                      model_name: str, cfg=None):
    """用早于 as_of 且标签到期的样本训练 → 预测 as_of 行。返回 (p_up, model) 或 (None, None)。"""
    cfg = cfg or P._CFG
    cols = P.FEATURE_COLS
    pos = {d: i for i, d in enumerate(panel.index)}
    if as_of not in pos:
        return None, None
    i = pos[as_of]
    feat_ok = panel[cols].notna().all(axis=1)
    if not bool(feat_ok.get(as_of, False)):
        return None, None
    lab_ok = panel["fwd_ret"].notna()
    train_rows = [t for t in panel.index if pos[t] + horizon < i and feat_ok[t] and lab_ok[t]]
    if len(train_rows) < int(cfg["最小训练样本"]):
        return None, None
    Xtr = panel.loc[train_rows]
    ytr = (panel.loc[train_rows, "fwd_ret"] > 0).astype(float).to_numpy()
    model = P.MODELS[model_name](cfg).fit(Xtr, ytr)
    p_up = float(model.predict_proba(panel.loc[[as_of]])[0])
    return p_up, model


def build_forecast(as_of: str | None = None, targets=("hs300", "proxy"),
                   horizons=(1, 5), model_name="composite", data_root=None,
                   breadth_df=None, cfg=None) -> dict:
    """产出某日大盘预测 dict(schema market_forecast/v0.5)。"""
    cfg = cfg or P._CFG
    if breadth_df is None:
        breadth_df = B.compute_breadth(data_root=data_root, cfg=cfg)

    out_targets = {}
    snap_date = None
    for tgt in targets:
        try:
            panel = F.build_panel(target=tgt, horizon=1, data_root=data_root,
                                  breadth_df=breadth_df, cfg=cfg)
        except Exception as e:
            out_targets[tgt] = {"error": f"面板构建失败:{e!r}"}
            continue
        # as_of 默认取该标的最后一个特征齐全日
        feat_ok = panel[P.FEATURE_COLS].notna().all(axis=1)
        valid_days = panel.index[feat_ok]
        if len(valid_days) == 0:
            out_targets[tgt] = {"error": "无特征齐全的交易日"}
            continue
        aod = pd.Timestamp(as_of) if as_of else valid_days[-1]
        if aod not in set(valid_days):
            aod = valid_days[valid_days <= aod][-1] if (valid_days <= aod).any() else valid_days[-1]
        snap_date = aod if snap_date is None else max(snap_date, aod)

        hz = {}
        for h in horizons:
            ph, model = _fit_predict_asof(panel if h == 1 else
                                          F.build_panel(target=tgt, horizon=h,
                                                        data_root=data_root,
                                                        breadth_df=breadth_df, cfg=cfg),
                                          aod, h, model_name, cfg)
            if ph is None:
                hz[str(h)] = {"error": "训练样本不足"}
                continue
            contrib = model.explain(panel.loc[[aod]]) if hasattr(model, "explain") else {}
            hz[str(h)] = {
                "p_up": round(ph, 4),
                "direction": _direction(ph),
                "bucket": P._LABELS[P.prob_to_bucket(ph)],
                "bucket_idx": P.prob_to_bucket(ph),
                "factor_contrib": {k: round(v, 4) for k, v in contrib.items()},
                "model": model_name,
            }
        out_targets[tgt] = {"name": _TARGET_NAME.get(tgt, tgt),
                            "as_of": str(aod)[:10], "horizons": hz}

    # 广度 / 消息面快照(as_of 当日)
    bsnap, ssnap = {}, {}
    try:
        if snap_date in breadth_df.index:
            row = breadth_df.loc[snap_date]
            for k in ("total", "adv", "dec", "limit_up", "limit_down",
                      "net_adv", "above_ma20_ratio", "below_ma20_ratio",
                      "nh_net_20", "median_pct"):
                if k in row:
                    bsnap[k] = round(float(row[k]), 4)
    except Exception:
        pass
    try:
        from tools.analysis.market_forecast import sentiment as S
        se = S.compute_sentiment(data_root)
        if snap_date is not None and snap_date in se.index:
            r = se.loc[snap_date]
            ssnap = {k: (round(float(r[k]), 4) if k in r else None)
                     for k in ("se_net", "se_ratio", "se_bull", "se_bear", "se_n")}
    except Exception:
        pass

    return {
        "schema": "market_forecast/v0.5",
        "as_of": str(snap_date)[:10] if snap_date is not None else None,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "non_investment_advice": True,
        "model": model_name,
        "targets": out_targets,
        "breadth_snapshot": bsnap,
        "sentiment_snapshot": ssnap,
        "notes": ("v0.5 三维(技术+广度+消息面)。消息面历史浅(~1月)作近端因子,缺日降级中性。"
                  "全A等权代理指数含幸存者偏差,方向研究用,绝对收益勿当真。回测详见 "
                  "tools.backtest.market_forecast_backtest。"),
    }


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", default=None, help="特征截止日 YYYY-MM-DD(默认最新)")
    ap.add_argument("--model", default="composite", choices=list(P.MODELS))
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--out", default=None, help="落盘路径;缺省只打印")
    ap.add_argument("--write-analysis", action="store_true",
                    help="写 data/analysis/<as_of>/market_forecast.json")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    fc = build_forecast(as_of=a.as_of, model_name=a.model, data_root=a.data_root)
    print(json.dumps(fc, ensure_ascii=False, indent=2))
    out = a.out
    if a.write_analysis and fc.get("as_of"):
        from tools.analysis.market_forecast.dataroot import ensure_data_root, analysis_dir
        root = ensure_data_root(a.data_root)
        d = analysis_dir(root) / fc["as_of"]
        d.mkdir(parents=True, exist_ok=True)
        out = str(d / "market_forecast.json")
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(fc, f, ensure_ascii=False, indent=2)
        print(f"[saved] {out}")


if __name__ == "__main__":
    _main()
