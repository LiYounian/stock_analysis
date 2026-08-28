"""指标条件化预测 · walk-forward A/B 回测(计划文档1 F6)。

问题:F3 的"指标条件化上涨概率/区间"相对**无条件基线**(全池历史频率,已自证≈掷硬币 Brier≈0.25)
到底有没有修好方向区分力?严格无未来函数、诚实回填(允许"聚合无 alpha")。

方法(高效批量,复用 state_pool):
  · 测试日:池全历史交易日按 stride 抽样 + 每 regime 段保底(screen_forward_common.pick_test_days)。
  · 每测试日 t:对每个"当日出现的状态格",用 **od_N ≤ t** 的样本(bisect 前缀,无未来)算条件分布,每格算一次;
    无条件基线 = 全池 od_N ≤ t 的分布(不分状态)。eval 点 = 当日各股的真实 r_N(其 od_N=t+N>t 天然不入条件池)。
  · 匹配阶梯 精确→放宽1→放宽2→退回(min 样本);记放宽层级。
指标:①Brier(条件化 vs 无条件)②区间覆盖率 ③区间宽度 ④上涨概率校准曲线 ⑤退回率
      ⑥按 regime 分层 ⑦按放宽层级分层 ⑧聚类 t(按测试日聚类的 ΔBrier 显著性)⑨预注册主格 vs 探索格。

⚠️ 非投资建议。用法:python -m tools.backtest.backtest_conditional [--stride 60] [--min-samples 500] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.analysis import conditional_predict as cp
from tools.backtest import screen_forward_common as C
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("backtest.conditional")
_HORIZONS = (1, 5, 10)
_DISCLAIMER = "历史回测≠未来保证,非投资建议;无未来函数(od_N≤t)。"

# 预注册主格(A3 防过拟合):这些格每 horizon 必报,不论输赢;其余为探索格另列。
_MAIN_CELLS = [
    ("多头排列", "强", "触上轨"),
    ("多头排列", "强", "中性"),
    ("空头排列", "弱", "触下轨"),
    ("纠缠", "中", "中性"),
]


def _dist(r: np.ndarray, ql, qm, qh) -> dict:
    return {"up": float((r > 0).mean()), "q_lo": float(np.percentile(r, ql)),
            "q_mid": float(np.percentile(r, qm)), "q_hi": float(np.percentile(r, qh)),
            "mean": float(r.mean()), "n": int(len(r))}


def build_eval(stride: int = 60, min_samples: int = None) -> pd.DataFrame:
    """逐测试日逐股产出评测长表:每行 = 一个 (测试日,股票,horizon) 的 条件化预测 vs 无条件 vs 实际。"""
    pool = cp.load_state_pool()
    if pool is None or pool.empty:
        raise SystemExit("!! state_pool 缺失,先建池:python -c 'from tools.analysis import conditional_predict as cp,tools.store.repo as s; cp.build_state_pool(sorted(s.list_master_codes()),save=True)'")
    idx = cp.get_pool_index()
    ql, qm, qh = THRESHOLDS["预测"]["情景分位"]
    min_samples = min_samples or THRESHOLDS["指标条件化"]["min相似样本数"]

    hs_feat = C._hs300_regime_series(C.load_hs300())
    days = [pd.Timestamp(d) for d in sorted(pool["date"].unique())]
    test_days = C.pick_test_days(days, hs_feat, stride=stride, max_forward=max(_HORIZONS), per_regime_min=3)
    logger.info("测试日 %d 个(stride=%d)", len(test_days), stride)

    all_cells = list(idx["cells"].keys())
    rows = []
    for t, regime in test_days:
        t_int = int(pd.Timestamp(t).value)
        day_rows = pool[pool["date"] == t]
        if day_rows.empty:
            continue
        uniq_cells = set(map(tuple, day_rows[["trend", "mom", "boll"]].drop_duplicates().to_numpy()))
        for N in _HORIZONS:
            base_r = cp._gather(idx, all_cells, N, t_int)     # 无条件基线(全池 od_N≤t)
            if len(base_r) < 20:
                continue
            base = _dist(base_r, ql, qm, qh)
            # 每格条件分布(od_N≤t),每格算一次
            cell_pred = {}
            for key in uniq_cells:
                exact = [key] if key in idx["cells"] else []
                tm = idx["by_tm"].get((key[0], key[1]), [])
                tr = idx["by_t"].get(key[0], [])
                chosen = None
                for lvl, cells in (("精确", exact), ("放宽1", tm), ("放宽2", tr)):
                    r = cp._gather(idx, cells, N, t_int)
                    if len(r) >= min_samples:
                        chosen = (lvl, _dist(r, ql, qm, qh))
                        break
                cell_pred[key] = chosen        # None = 退回
            actual_col = f"r{N}"
            sub = day_rows.dropna(subset=[actual_col])
            for row in sub[["trend", "mom", "boll", actual_col]].itertuples(index=False):
                key = (row[0], row[1], row[2])
                actual = float(row[3])
                pred = cell_pred.get(key)
                use = pred[1] if pred else base
                rows.append({
                    "day": t, "regime": regime, "N": N,
                    "cell": "×".join(key), "level": (pred[0] if pred else "退回"),
                    "fallback": pred is None,
                    "up": use["up"], "q_lo": use["q_lo"], "q_hi": use["q_hi"], "mean": use["mean"],
                    "base_up": base["up"], "base_q_lo": base["q_lo"], "base_q_hi": base["q_hi"],
                    "actual": actual, "hit": 1.0 if actual > 0 else 0.0,
                })
    return pd.DataFrame(rows)


def _brier(p, y):
    return float(np.mean((np.asarray(p) - np.asarray(y)) ** 2))


def _cluster_t(ev: pd.DataFrame) -> dict:
    """按测试日聚类的 ΔBrier(无条件−条件化,正=条件化更好)显著性 t 统计。"""
    per_day = []
    for _, g in ev.groupby("day"):
        b_cond = _brier(g["up"], g["hit"])       # up 为分数(0-1),与 hit(0/1)同尺度
        b_base = _brier(g["base_up"], g["hit"])
        per_day.append(b_base - b_cond)
    d = np.array(per_day)
    if len(d) < 2 or d.std(ddof=1) == 0:
        return {"ΔBrier均值": float(d.mean()) if len(d) else None, "t": None, "n_days": len(d)}
    t = float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d))))
    return {"ΔBrier均值": float(d.mean()), "t": round(t, 2), "n_days": len(d)}


def summarize(ev: pd.DataFrame) -> dict:
    ql, qm, qh = THRESHOLDS["预测"]["情景分位"]
    out = {"免责": _DISCLAIMER, "测试日数": int(ev["day"].nunique()), "总评测点": int(len(ev)),
           "regime分布": {k: int(v) for k, v in ev.groupby("regime")["day"].nunique().items()}}
    for N in _HORIZONS:
        s = ev[ev["N"] == N]
        if s.empty:
            continue
        cov_cond = float(((s["actual"] >= s["q_lo"]) & (s["actual"] <= s["q_hi"])).mean()) * 100
        cov_base = float(((s["actual"] >= s["base_q_lo"]) & (s["actual"] <= s["base_q_hi"])).mean()) * 100
        blk = {
            "Brier条件化": round(_brier(s["up"], s["hit"]), 4),
            "Brier无条件": round(_brier(s["base_up"], s["hit"]), 4),
            "基础上涨率%": round(float(s["hit"].mean()) * 100, 1),
            "区间覆盖_条件化%": round(cov_cond, 1), "区间覆盖_无条件%": round(cov_base, 1),
            "区间宽度_条件化": round(float((s["q_hi"] - s["q_lo"]).mean()), 2),
            "区间宽度_无条件": round(float((s["base_q_hi"] - s["base_q_lo"]).mean()), 2),
            "退回率%": round(float(s["fallback"].mean()) * 100, 1),
            "聚类t": _cluster_t(s),
        }
        blk["ΔBrier(无条件−条件化)"] = round(blk["Brier无条件"] - blk["Brier条件化"], 4)
        # 校准曲线(up 为分数 0-1)
        cut = pd.cut(s["up"], bins=[0, 0.4, 0.5, 0.6, 0.7, 1.01], right=False)
        curve = []
        for b, g in s.groupby(cut, observed=True):
            curve.append({"区间": str(b), "预测均值%": round(float(g["up"].mean()) * 100, 1),
                          "实际上涨%": round(float(g["hit"].mean()) * 100, 1), "n": int(len(g))})
        blk["校准曲线"] = curve
        # 按 regime
        blk["按regime"] = {}
        for reg, g in s.groupby("regime"):
            blk["按regime"][reg] = {"Brier条件化": round(_brier(g["up"], g["hit"]), 4),
                                    "Brier无条件": round(_brier(g["base_up"], g["hit"]), 4),
                                    "n": int(len(g))}
        # 按放宽层级
        blk["按放宽层级"] = {}
        for lvl, g in s.groupby("level"):
            cov = float(((g["actual"] >= g["q_lo"]) & (g["actual"] <= g["q_hi"])).mean()) * 100
            blk["按放宽层级"][lvl] = {"Brier": round(_brier(g["up"], g["hit"]), 4),
                                      "覆盖%": round(cov, 1), "占比%": round(len(g) / len(s) * 100, 1),
                                      "n": int(len(g))}
        # 预注册主格 vs 探索格
        blk["预注册主格"] = {}
        for cell in _MAIN_CELLS:
            key = "×".join(cell)
            g = s[s["cell"] == key]
            if len(g):
                blk["预注册主格"][key] = {"预测上涨%": round(float(g["up"].mean()) * 100, 1),
                                          "实际上涨%": round(float(g["hit"].mean()) * 100, 1),
                                          "均期望%": round(float(g["mean"].mean()), 2), "n": int(len(g))}
        out[f"{N}日"] = blk
    return out


def _print(res: dict):
    print(f"\n===== 指标条件化预测 · walk-forward A/B 回测 · 测试日 {res['测试日数']} · "
          f"评测点 {res['总评测点']:,} · regime {res['regime分布']} =====\n({_DISCLAIMER})\n")
    for N in _HORIZONS:
        r = res.get(f"{N}日")
        if not r:
            continue
        ct = r["聚类t"]
        print(f"—— {N} 交易日 ——")
        print(f"  Brier 条件化={r['Brier条件化']} vs 无条件={r['Brier无条件']}  ΔBrier={r['ΔBrier(无条件−条件化)']:+.4f}"
              f"  聚类t={ct['t']}(n_days={ct['n_days']})  基础上涨率={r['基础上涨率%']}%")
        print(f"  区间覆盖 条件化={r['区间覆盖_条件化%']}% vs 无条件={r['区间覆盖_无条件%']}%  "
              f"宽度 条件化={r['区间宽度_条件化']} vs 无条件={r['区间宽度_无条件']}  退回率={r['退回率%']}%")
        print(f"  校准曲线:", "  ".join(f"{c['区间']}预{c['预测均值%']}→实{c['实际上涨%']}(n{c['n']})" for c in r["校准曲线"]))
        print(f"  按regime:", "  ".join(f"{k}:cond{v['Brier条件化']}/base{v['Brier无条件']}(n{v['n']})" for k, v in r["按regime"].items()))
        print(f"  按放宽层级:", "  ".join(f"{k}:Brier{v['Brier']}/覆盖{v['覆盖%']}%/占{v['占比%']}%" for k, v in r["按放宽层级"].items()))
        print(f"  预注册主格:")
        for k, v in r["预注册主格"].items():
            print(f"      {k:<18} 预测上涨{v['预测上涨%']:>5}% → 实际{v['实际上涨%']:>5}%  期望{v['均期望%']:+.2f}%  (n={v['n']:,})")
        print()


def run(stride=60, min_samples=None, json_path=None):
    ev = build_eval(stride=stride, min_samples=min_samples)
    if ev.empty:
        print("!! 评测表为空")
        return
    res = summarize(ev)
    _print(res)
    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stride", type=int, default=60)
    ap.add_argument("--min-samples", type=int, default=0)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    run(stride=a.stride, min_samples=a.min_samples or None, json_path=a.json or None)
