"""预测两条线(止跌/止盈)· 历史回测验证(数据说话,非投资建议)。

验证的对象:`tools/analysis/predict.py` 对每只票、每个交易日 t **只用 ≤t 数据**产出的
两条线与分布,画得准不准。严格无未来函数:逐日把 df.iloc[:t+1] 喂真正的 predict()
(连 technical.compute 也在切片上重算),再看 t+1..t+N 的真实 OHLC 路径。

产出五个指标(见 docs/计划/预测两条线_回测验证与调参_计划.md §1):
  1. 止损触发率 —— 持有期内跌破止损位的比例;
  2. 止盈先于止损命中率 —— 逐日判先摸哪条 + 按真实盈亏比算期望 E + 实测收益;
  3. 区间覆盖校准 —— 实际 r_N 落在 [q10,q90] 的比例(应≈80%);
  4. 上涨概率校准 —— 预测上涨概率分箱 vs 实际,Brier 分;
  5. 叠加方向信号后的期望 —— 只在"买卖倾向=偏买入"的 t 上重测第2项。

两条线各测一遍:**波动率括号**(持有期建议) vs **L3 结构锚定**(结构位.锚定),横向对比。

用法:python -m tools.backtest.backtest_predict [--horizon 1,5,10] [--codes 300308,...] [--json 输出路径]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from tools.analysis import predict as pred
from tools.analysis import technical
from tools.collectors import market
from tools.config import stock_pool
from tools.config.strategy import THRESHOLDS

_P = THRESHOLDS["预测"]
_WARMUP = 40          # 冷启动:predict 需 ≥30 根 + 留 ATR/pivot 余量
_DISCLAIMER = "历史回测≠未来保证,非投资建议;概率为历史频率、区间为波动率构造。"


# ————————————————————————— 前瞻路径:先摸哪条 —————————————————————————
def _first_touch(high: np.ndarray, low: np.ndarray, sl, tp) -> str:
    """沿路径逐日判定先摸到哪条线。保守:同日两条都触及记 SL-first(A股跌得快)。

    返回 'TP' / 'SL' / 'NEITHER'。sl/tp 任一为 None → 该条不参与(视为不会被摸)。
    """
    n = len(high)
    for d in range(n):
        hit_sl = sl is not None and low[d] <= sl
        hit_tp = tp is not None and high[d] >= tp
        if hit_sl:                    # 同日 tie 也归 SL(保守)
            return "SL"
        if hit_tp:
            return "TP"
    return "NEITHER"


def _bracket_outcome(high, low, close_end, entry, sl, tp, loss_pct, gain_pct):
    """一条"括号"(sl/tp)在前瞻路径上的结局 + 实测收益%。

    先摸止盈→+目标%;先摸止损→−止损%;到期都没碰→按末日收盘 mark-to-close(r_N)。
    returns (touch, realized_pct)。sl/tp 缺失 → touch='NA'。
    """
    if sl is None or tp is None:
        return "NA", None
    touch = _first_touch(high, low, sl, tp)
    if touch == "TP":
        return "TP", float(gain_pct)
    if touch == "SL":
        return "SL", -float(loss_pct)
    return "NEITHER", float(close_end / entry - 1.0) * 100.0


# ————————————————————————— 逐票逐日建 panel —————————————————————————
def build_panel(codes, horizons=(1, 5, 10), warmup=_WARMUP) -> pd.DataFrame:
    """逐票逐日无未来函数跑 predict,落长表。每行 = 一个 (code,t,N) 观测。"""
    ql, qm, qh = _P["情景分位"]
    rows = []
    maxN = max(horizons)
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < warmup + maxN + 5:
            continue
        df = df.reset_index(drop=True)
        high = df["high"].to_numpy(float)
        low = df["low"].to_numpy(float)
        close = df["close"].to_numpy(float)
        n = len(df)
        used += 1
        for t in range(warmup, n - maxN):
            sl_ = df.iloc[: t + 1]
            try:
                tech = technical.compute(sl_)
                p = pred.predict(sl_, tech)
            except Exception:
                continue
            if p.get("error"):
                continue
            entry = float(close[t])
            bias = (p.get("买卖倾向") or {}).get("结论")
            anc = (p.get("结构位") or {}).get("锚定") or {}
            anc_sl, anc_tp = anc.get("止损位"), anc.get("止盈位")
            anc_scn = anc.get("情景")
            sc_all = p.get("情景预测") or {}
            hb = p.get("持有期建议") or {}
            for N in horizons:
                hi = high[t + 1: t + 1 + N]
                lo = low[t + 1: t + 1 + N]
                if len(hi) < N:
                    continue
                r_N = float(close[t + N] / entry - 1.0) * 100.0
                # —— 波动率括号(按 N) ——
                br = hb.get(f"{N}日") or {}
                br_sl, br_tp = br.get("止损位"), br.get("止盈位")
                br_loss, br_gain = br.get("最大亏损%"), br.get("目标盈利%")
                br_touch, br_real = _bracket_outcome(
                    hi, lo, close[t + N], entry, br_sl, br_tp, br_loss, br_gain)
                # —— L3 锚定(与 N 无关的单一 sl/tp,跨 N 各测)——
                # 锚定的"名义盈亏%"用锚点相对现价算,便于统一 E 口径
                anc_loss = (1.0 - anc_sl / entry) * 100.0 if anc_sl else None
                anc_gain = (anc_tp / entry - 1.0) * 100.0 if anc_tp else None
                anc_touch, anc_real = _bracket_outcome(
                    hi, lo, close[t + N], entry, anc_sl, anc_tp, anc_loss, anc_gain)
                # —— 情景分布 ——
                sc = sc_all.get(f"{N}日") or {}
                up_p = sc.get("上涨概率%")
                q_lo = sc.get(f"悲观%(q{ql})")
                q_mid = sc.get(f"中位%(q{qm})")
                q_hi = sc.get(f"乐观%(q{qh})")
                rows.append({
                    "code": code, "t": t, "N": N, "entry": entry, "r_N": r_N, "bias": bias,
                    "br_touch": br_touch, "br_real": br_real,
                    "br_loss": br_loss, "br_gain": br_gain, "br_sl_hit": bool(br_sl is not None and lo.min() <= br_sl),
                    "anc_scn": anc_scn, "anc_touch": anc_touch, "anc_real": anc_real,
                    "anc_loss": anc_loss, "anc_gain": anc_gain,
                    "anc_sl_hit": bool(anc_sl is not None and lo.min() <= anc_sl),
                    "up_p": up_p, "q_lo": q_lo, "q_mid": q_mid, "q_hi": q_hi,
                })
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


# ————————————————————————— 五指标 —————————————————————————
def _touch_stats(sub: pd.DataFrame, prefix: str) -> dict:
    """第2项:先摸哪条 + 期望 E(按名义盈亏比)+ 实测收益。prefix ∈ {'br','anc'}。"""
    d = sub[sub[f"{prefix}_touch"] != "NA"]
    m = len(d)
    if m == 0:
        return {"n": 0}
    tp = float((d[f"{prefix}_touch"] == "TP").mean())
    sl = float((d[f"{prefix}_touch"] == "SL").mean())
    ne = float((d[f"{prefix}_touch"] == "NEITHER").mean())
    gain = float(d[f"{prefix}_gain"].mean())
    loss = float(d[f"{prefix}_loss"].mean())
    E = tp * gain - sl * loss                       # 名义期望(先命中口径)
    realized = float(d[f"{prefix}_real"].mean())    # 实测:止盈/止损/到期收盘
    return {"n": m, "TP先%": tp * 100, "SL先%": sl * 100, "未触%": ne * 100,
            "名义E%": E, "实测均值%": realized, "止损触发率%": float(d[f"{prefix}_sl_hit"].mean()) * 100,
            "均目标%": gain, "均止损%": loss}


def _coverage(sub: pd.DataFrame) -> dict:
    """第3项:区间覆盖校准(应≈80%)+ 中位偏差。"""
    d = sub.dropna(subset=["q_lo", "q_hi", "q_mid"])
    if d.empty:
        return {"n": 0}
    cover = float(((d["r_N"] >= d["q_lo"]) & (d["r_N"] <= d["q_hi"])).mean()) * 100
    below = float((d["r_N"] < d["q_lo"]).mean()) * 100
    above = float((d["r_N"] > d["q_hi"]).mean()) * 100
    med_bias = float((d["r_N"] - d["q_mid"]).mean())
    return {"n": len(d), "覆盖率%": cover, "跌破下沿%": below, "冲破上沿%": above,
            "中位偏差pp": med_bias, "目标覆盖": 80.0}


def _calibration(sub: pd.DataFrame) -> dict:
    """第4项:上涨概率校准。分箱 + Brier + 基础率。"""
    d = sub.dropna(subset=["up_p"]).copy()
    if d.empty:
        return {"n": 0}
    d["hit"] = (d["r_N"] > 0).astype(float)
    base = float(d["hit"].mean()) * 100
    brier = float(((d["up_p"] / 100.0 - d["hit"]) ** 2).mean())
    bins = [0, 40, 50, 60, 70, 101]
    d["bin"] = pd.cut(d["up_p"], bins=bins, right=False)
    curve = []
    for b, g in d.groupby("bin", observed=True):
        curve.append({"区间": str(b), "预测均值%": round(float(g["up_p"].mean()), 1),
                      "实际上涨%": round(float(g["hit"].mean()) * 100, 1), "n": int(len(g))})
    return {"n": len(d), "基础上涨率%": base, "Brier": brier, "曲线": curve}


def metrics_from_panel(panel: pd.DataFrame, horizons) -> dict:
    """把 panel 压成五指标(按 N 分组;两条线各一份;第5项加方向信号过滤)。"""
    out = {"样本股数": int(panel.attrs.get("used", 0)), "总观测": int(len(panel)), "免责": _DISCLAIMER}
    for N in horizons:
        sub = panel[panel["N"] == N]
        if sub.empty:
            continue
        buy = sub[sub["bias"] == "偏买入"]
        out[f"{N}日"] = {
            "波动率括号": _touch_stats(sub, "br"),
            "L3锚定": _touch_stats(sub, "anc"),
            "区间校准": _coverage(sub),
            "上涨概率校准": _calibration(sub),
            "叠加偏买入·波动率括号": _touch_stats(buy, "br"),
            "叠加偏买入·L3锚定": _touch_stats(buy, "anc"),
            "叠加偏买入·样本占比%": round(len(buy) / len(sub) * 100, 1) if len(sub) else 0.0,
        }
    return out


# ————————————————————————— CLI / 报告 —————————————————————————
def _fmt(d: dict, keys) -> str:
    return "  ".join(f"{k}={d[k]:+.1f}" if isinstance(d.get(k), float) else f"{k}={d.get(k)}"
                     for k in keys if k in d)


def run(horizons=(1, 5, 10), codes=None, json_path=None):
    codes = codes or stock_pool.get_codes()
    panel = build_panel(codes, horizons)
    if panel.empty:
        print("!! panel 为空(无足够历史)");
        return
    res = metrics_from_panel(panel, horizons)
    print(f"\n===== 预测两条线 · 历史回测 · 样本 {res['样本股数']} 只 · 观测 {res['总观测']} · "
          f"horizon={horizons} =====\n(非投资建议;严格无未来函数)\n")
    for N in horizons:
        r = res.get(f"{N}日")
        if not r:
            continue
        print(f"—— {N} 交易日 ——")
        print(f"  [1/2 波动率括号] " + _fmt(r["波动率括号"],
              ["n", "TP先%", "SL先%", "未触%", "止损触发率%", "名义E%", "实测均值%", "均目标%", "均止损%"]))
        print(f"  [1/2 L3 锚定  ] " + _fmt(r["L3锚定"],
              ["n", "TP先%", "SL先%", "未触%", "止损触发率%", "名义E%", "实测均值%", "均目标%", "均止损%"]))
        print(f"  [3 区间校准   ] " + _fmt(r["区间校准"], ["n", "覆盖率%", "跌破下沿%", "冲破上沿%", "中位偏差pp", "目标覆盖"]))
        cal = r["上涨概率校准"]
        print(f"  [4 上涨概率   ] n={cal.get('n')} 基础上涨率%={cal.get('基础上涨率%'):.1f} Brier={cal.get('Brier'):.3f}")
        for pt in cal.get("曲线", []):
            print(f"        {pt['区间']:<12} 预测{pt['预测均值%']:>5}% → 实际{pt['实际上涨%']:>5}%  (n={pt['n']})")
        print(f"  [5 叠加偏买入 ] 占比{r['叠加偏买入·样本占比%']}%  "
              f"括号:" + _fmt(r["叠加偏买入·波动率括号"], ["n", "TP先%", "SL先%", "名义E%", "实测均值%"]))
        print(f"                 L3:" + _fmt(r["叠加偏买入·L3锚定"], ["n", "TP先%", "SL先%", "名义E%", "实测均值%"]))
        print()
    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="1,5,10")
    ap.add_argument("--codes", default="")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    run(tuple(int(x) for x in a.horizon.split(",")),
        codes=[c for c in a.codes.split(",") if c] or None,
        json_path=a.json or None)
