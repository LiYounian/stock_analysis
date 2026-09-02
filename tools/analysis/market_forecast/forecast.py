"""每日大盘预测产出(market_forecast.json)——把预测器接到生产。

给定特征截止日 T(收盘后),用**严格早于 T 且标签已到期**的样本训练预测器,对 T 预测其
T+1 / T+5 涨跌方向概率 + 五档 + 各维贡献,落 data/analysis/<T>/market_forecast.json。

schema(market_forecast/v1)见 build_forecast 返回。v1 相对 v0.5 补第四维「资金流」
(SSE 市场级两融,盘后披露→特征滞后≥1交易日),各标的 horizon 的 factor_contrib 多一维「资金流」,
新增 fundflow_snapshot(as_of 可用的最新一日两融,即 as_of 的前一交易日)。CLI 参数不变。
防未来函数:训练样本标签窗口需在 T 之前收口;资金流特征只用 date < T 的两融。
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
# 每个标的的 β 适用范围声明(选股环节据此选个股β基准,见提议问题B)。
_TARGET_SCOPE = {
    "hs300": "权重股/大盘β;不代表个股中位数",
    "proxy": "个股/中小盘β基准(含幸存者偏差,方向用)",
}


def _style_divergence(out_targets: dict, cfg) -> dict:
    """风格背离(二八分化)标记:比较 β 背景标的(hs300)与 β 基准标的(proxy)的 p_up。

    逐 horizon 触发条件(任一):方向档背离 / p_up 跨越 0.5 两侧 / |差值| ≥ 阈值。
    触发时按方向给类型:背景更强→"权重搭台中小盘偏弱";基准更强→"中小盘领跑权重滞后"。
    让选股环节读 β 时知道"hs300 偏多 ≠ 个股偏多",以 proxy 为准。见提议问题B/提议3。
    """
    beta = cfg.get("个股β基准", "proxy")
    bg = cfg.get("β背景标的", "hs300")
    thr = float(cfg.get("风格背离差值阈值", 0.10))
    ht = out_targets.get(bg, {}).get("horizons", {}) or {}
    pt = out_targets.get(beta, {}).get("horizons", {}) or {}
    dims, any_flag = {}, False
    common = sorted(set(ht) & set(pt), key=lambda x: int(x))
    for h in common:
        hh, pp = ht[h], pt[h]
        if "p_up" not in hh or "p_up" not in pp:
            continue
        p_bg, p_beta = float(hh["p_up"]), float(pp["p_up"])
        diff = round(p_bg - p_beta, 4)
        cross0 = (p_bg - 0.5) * (p_beta - 0.5) < 0
        dir_diff = hh.get("direction") != pp.get("direction")
        flag = cross0 or dir_diff or abs(diff) >= thr
        typ = ("一致" if not flag else
               ("权重搭台中小盘偏弱" if diff > 0 else "中小盘领跑权重滞后"))
        any_flag = any_flag or flag
        dims[h] = {
            "触发": flag, "类型": typ,
            f"{bg}_p_up": p_bg, f"{beta}_p_up": p_beta,
            f"{bg}_direction": hh.get("direction"), f"{beta}_direction": pp.get("direction"),
            "差值(背景-基准)": diff, "跨越0.5": cross0, "方向档背离": dir_diff,
        }
    return {
        "触发": any_flag,
        "口径": "二八分化/风格背离",
        "阈值": thr,
        "维度": dims,
        "说明": (f"{bg}(权重股背景)与{beta}(个股β基准)方向/强度对比;个股选股 β 以 {beta} 为准,"
                 f"{bg} 仅作权重股背景,勿被其偏多读成个股偏多。"),
    }


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
            bidx = P.prob_to_bucket(ph)
            hz[str(h)] = {
                "p_up": round(ph, 4),
                "direction": _direction(ph),
                # 概率口径:档位=P(上涨)的方向概率分位,**不是**涨跌幅预测(命中率~55%,不说幅度)。
                "prob_bucket": P.prob_bucket_label(bidx),
                "prob_bucket_idx": bidx,
                "bucket": P.prob_bucket_label(bidx),   # 兼容旧字段名,值已切概率口径(不再是"大涨/大跌")
                "bucket_idx": bidx,
                "bucket_kind": "上行概率分位(方向,非涨跌幅)",
                "amplitude_forecast": None,            # 不预测涨跌幅
                "amplitude_note": "仅方向概率,幅度未知;勿把高概率读成大幅",
                "factor_contrib": {k: round(v, 4) for k, v in contrib.items()},
                "model": model_name,
            }
        out_targets[tgt] = {"name": _TARGET_NAME.get(tgt, tgt),
                            "as_of": str(aod)[:10],
                            "适用范围": _TARGET_SCOPE.get(tgt, ""),
                            "horizons": hz}

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

    # 资金流快照:as_of 可用的最新一日两融(严格早于 as_of,防未来函数)+ 滞后拼到 as_of 的资金流特征
    ffsnap = {}
    try:
        from tools.analysis.market_forecast import fundflow as FF
        mm = FF.load_market_margin(data_root)
        if snap_date is not None and not mm.empty:
            usable = mm[mm.index < snap_date]           # 严格早于 as_of(两融盘后披露)
            if not usable.empty:
                last = usable.iloc[-1]
                ffsnap = {
                    "margin_date": str(usable.index[-1])[:10],   # 用到的两融日(=as_of前一交易日)
                    "融资余额": round(float(last["rz_bal"]), 2),
                    "融资买入额": round(float(last["rz_buy"]), 2),
                    "融资融券余额": round(float(last["rzrq_bal"]), 2),
                    "source": "akshare:stock_margin_sse",
                    "note": "SSE市场级两融,盘后披露,已滞后至as_of前一交易日(防未来函数)",
                }
    except Exception:
        pass

    return {
        "schema": "market_forecast/v1",
        "as_of": str(snap_date)[:10] if snap_date is not None else None,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "non_investment_advice": True,
        "model": model_name,
        "选股用β基准": {
            "默认": cfg.get("个股β基准", "proxy"),
            "背景": cfg.get("β背景标的", "hs300"),
            "说明": ("个股 β 读全A等权代理(proxy,≈中小盘);沪深300(hs300)仅作权重股背景;"
                     "两者分歧时以 proxy 为准并看 '分歧标记'。"),
        },
        "分歧标记": _style_divergence(out_targets, cfg),
        "targets": out_targets,
        "breadth_snapshot": bsnap,
        "sentiment_snapshot": ssnap,
        "fundflow_snapshot": ffsnap,
        "notes": ("档位=**上行概率分位(方向口径)**,不是涨跌幅预测(命中率~55%,不说'大涨/大跌');幅度未知。"
                  "个股选股 β 基准默认 proxy(全A等权≈中小盘),hs300 仅权重股背景,分歧看'分歧标记'。"
                  "v1 四维(技术+广度+消息面+资金流)。资金流=SSE市场级两融(akshare stock_margin_sse,"
                  "回溯2022),盘后披露→特征滞后≥1交易日;历史未采到的日子该维降级中性(按覆盖率自动降权)。"
                  "消息面历史浅(~1月)作近端因子,缺日降级中性。全A等权代理指数含幸存者偏差,"
                  "方向研究用,绝对收益勿当真。A/B回测详见 docs/计划/大盘预测策略.md §7 与 "
                  "tools.backtest.market_forecast_backtest(--no-fundflow 关资金流维=v0.5)。"),
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
