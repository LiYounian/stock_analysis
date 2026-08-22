"""反转低换手组合(候选策略)前瞻回测——因子有效性 + 真实成本净额 + 对齐既有 rev5。

谱系:复用 `backtest_rank` 的 IC/ICIR/分层机制(它正是产出 data/analysis/backtest/
rank_rev5*.json 的回测器),故与既有 rev5 单因子**苹果对苹果**可比。

为什么不能直接用 backtest_rank 的单票 scorer:复合因子 = 横截面 winsorize+zscore
两个原始因子后加权,**需要整个横截面**才能标准化,单票 scorer 算不出。故本模块:
  1) build_rt_panel:逐票逐日算 rev / turn **两个原始因子** + 前瞻收益(无未来函数)
  2) add_scores:逐日横截面 winsor+zscore 合成 score_composite;同时保留 score_rev /
     score_turn(单因子排序与其原始值秩等价,IC/分层不变)→ 同一 panel 三组对比
  3) 复用 backtest_rank.ic_metrics / decile_metrics
  4) topk_net_metrics(本模块自写):**非重叠调仓 + 组合换手率 + 真实往返成本**——
     反转类换手高,net 超额为正才算"能交易";低换手腿若降低组合换手 → 省成本,
     正是本策略核心看点(统筹审查意见 1)。

诚实性(统筹审查意见):net 超额为正(gross 为正不算数)+ 相对纯 rev5 有净额增量,
才算复合成立;无增量如实写。防未来函数:因子只读 kdf[:t+1] 尾部,前瞻收益取 t 之后
价仅作被预测标签。⚠️ 非投资建议;产物只写 worktree 本地,不写主检出、不动 main。

用法:python -m tools.backtest.backtest_reversal_turnover [--sample N] [--seed 42]
      [--step K] [--horizon 5,10,20] [--topk 20] [--roundtrip-bps 17.5]
      [--min-liq-pct 0.5] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.backtest.backtest_rank import _WARMUP, decile_metrics, ic_metrics
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy._factor_util import winsorize_med, zscore
from tools.strategy.reversal_turnover import low_turnover_factor, reversal_factor

logger = logging.getLogger("backtest.reversal_turnover")

_CFG = THRESHOLDS.get("反转低换手", {})
_DISCLAIMER = "历史回测≠未来保证,非投资建议。"


# ————————————————————————— 建横截面 panel(rev + turn 双原始因子)—————————————————————————
def build_rt_panel(codes, rev_n: int, turn_n: int, horizons=(5, 10, 20),
                   step: int = 1, warmup: int = _WARMUP) -> pd.DataFrame:
    """逐票逐日算 rev/turn 原始因子 + 流动性 + 前瞻收益,落长表。无未来函数。

    rev = reversal_factor(close[:t+1])(跌得多→高);turn = low_turnover_factor(turnover[:t+1])
    (换手低→高,近端 NaN 按有效值兜底)。liq = 近20日均成交额(close×vol,与 backtest_rank 同口径)。
    """
    maxN = max(horizons)
    rows = []
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:                                    # noqa: BLE001
            continue
        if df is None or len(df) < warmup + maxN + 5:
            continue
        df = df.reset_index(drop=True)
        close = df["close"].to_numpy(float)
        vol = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
        turn_arr = (df["turnover"].to_numpy(float) if "turnover" in df.columns
                    else np.full(len(df), np.nan))
        amt = close * vol
        dates = [str(x)[:10] for x in df["date"].tolist()]
        n = len(df)
        used += 1
        for t in range(warmup, n - maxN, step):
            rev = reversal_factor(close[: t + 1], n=rev_n)
            turn = low_turnover_factor(turn_arr[: t + 1], n=turn_n)
            if rev is None or turn is None:
                continue
            if not (np.isfinite(rev) and np.isfinite(turn)):
                continue
            liq = float(np.mean(amt[max(0, t - 19): t + 1]))
            row = {"date": dates[t], "code": code, "rev": float(rev),
                   "turn": float(turn), "liq": liq}
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


def add_scores(panel: pd.DataFrame, w_rev: float, w_turn: float,
               scale: float = 3.0) -> pd.DataFrame:
    """逐日横截面 winsor+zscore 合成 score_composite;保留 score_rev / score_turn。

    score_rev / score_turn 直接用原始值(单因子排序与其秩等价,IC/分层不变),
    与既有 rank_rev5(纯 rev5 原始打分)口径一致,可比。
    """
    if panel.empty:
        return panel
    out = []
    for _, g in panel.groupby("date", sort=False):
        g = g.copy()
        zr = zscore(winsorize_med(g["rev"].tolist(), scale=scale))
        zt = zscore(winsorize_med(g["turn"].tolist(), scale=scale))
        g["z_rev"] = zr
        g["z_turn"] = zt
        g["score_composite"] = [w_rev * zr[i] + w_turn * zt[i] for i in range(len(g))]
        g["score_rev"] = g["rev"]
        g["score_turn"] = g["turn"]
        out.append(g)
    return pd.concat(out, ignore_index=True)


def _liq_filter(panel: pd.DataFrame, min_liq_pct: float) -> pd.DataFrame:
    """每日剔除成交额分位 < min_liq_pct 的票(测 edge 在可交易票里是否还在)。"""
    if not min_liq_pct or "liq" not in panel.columns:
        return panel
    keep = panel.groupby("date")["liq"].transform(lambda s: s.rank(pct=True)) >= min_liq_pct
    return panel[keep]


# ————————————————————————— TopK 净额(非重叠调仓 + 组合换手率 + 真实成本)—————————————————————————
def topk_net_metrics(panel: pd.DataFrame, score_col: str, N: int, k: int = 20,
                     roundtrip_bps: float = 17.5, min_cross: int = 10) -> dict:
    """非重叠 N 日调仓的 TopK 组合:gross vs net 超额 + 组合换手率 + 年化。

    · 调仓日 = 每 N 个交易日取一个(非重叠,持有到下一调仓日,对齐前瞻窗口 N)。
    · 每调仓日:score_col 降序取 TopK 等权,前瞻 N 日收益;超额 = TopK 均 − 全样本均。
    · 组合换手率:本期 TopK 与上期 TopK 的名字更替比例 = 1 − |交集|/k(反映实际交易量)。
    · net 每轮 = gross 每轮 − 换手率 × 往返成本(roundtrip_bps%);年化 ×(250/N)。
      低换手腿若使组合换手率更低 → 成本更省 → net 更活(核心看点)。
    """
    col = f"r_{N}"
    all_dates = sorted(panel["date"].unique())
    rebal_dates = all_dates[::N]                              # 非重叠调仓日
    prev_top: set[str] | None = None
    gross_list, turnover_list = [], []
    for d in rebal_dates:
        g = panel[panel["date"] == d]
        if len(g) < min_cross:
            continue
        gs = g.sort_values(score_col, ascending=False)
        kk = min(k, len(gs) // 2)
        if kk < 1:
            continue
        top = gs.head(kk)
        top_codes = set(top["code"].tolist())
        gross = float(top[col].mean() - g[col].mean())       # 超额(相对全样本均值基准)
        gross_list.append(gross)
        if prev_top is not None:
            turnover_list.append(1.0 - len(top_codes & prev_top) / len(top_codes))
        prev_top = top_codes
    if not gross_list:
        return {"调仓次数": 0}
    gross_avg = float(np.mean(gross_list))                    # 每轮 gross 超额 %
    turn_avg = float(np.mean(turnover_list)) if turnover_list else 1.0
    cost_per_round = turn_avg * roundtrip_bps / 100.0         # % per round
    net_avg = gross_avg - cost_per_round
    rounds_per_year = 250.0 / N
    return {
        "调仓次数": len(gross_list),
        "组合换手率": round(turn_avg, 3),
        "gross每轮超额%": round(gross_avg, 3),
        "净成本每轮%": round(cost_per_round, 3),
        "net每轮超额%": round(net_avg, 3),
        "gross年化超额%": round(gross_avg * rounds_per_year, 1),
        "net年化超额%": round(net_avg * rounds_per_year, 1),
        "net是否为正": bool(net_avg > 0),
    }


# ————————————————————————— 主流程 —————————————————————————
def run(codes, horizons=(5, 10, 20), step=1, topk=20, roundtrip_bps=17.5,
        min_liq_pct=0.0, w_rev=None, w_turn=None, rev_n=None, turn_n=None,
        json_path=None):
    rev_n = int(rev_n if rev_n is not None else _CFG.get("反转窗口", 5))
    turn_n = int(turn_n if turn_n is not None else _CFG.get("换手窗口", 20))
    _w = _CFG.get("权重", {"反转": 0.5, "低换手": 0.5})
    w_rev = float(w_rev if w_rev is not None else _w.get("反转", 0.5))
    w_turn = float(w_turn if w_turn is not None else _w.get("低换手", 0.5))

    panel = build_rt_panel(codes, rev_n, turn_n, horizons, step=step)
    if panel.empty:
        print("!! panel 为空"); return None
    used = int(panel.attrs.get("used", 0))
    if min_liq_pct:
        panel = _liq_filter(panel, min_liq_pct)
    panel = add_scores(panel, w_rev, w_turn)

    factors = {"composite": "score_composite", "rev%d" % rev_n: "score_rev",
               "turn%d" % turn_n: "score_turn"}
    res = {
        "策略": "反转低换手组合(候选)前瞻回测",
        "参数": {"反转窗口": rev_n, "换手窗口": turn_n, "权重": {"反转": w_rev, "低换手": w_turn},
                 "topk": topk, "往返成本bps": roundtrip_bps, "流动性过滤分位": min_liq_pct, "step": step},
        "样本股数": used, "总观测": int(len(panel)),
        "交易日数": int(panel["date"].nunique()), "免责": _DISCLAIMER,
        "结果": {},
    }
    print(f"\n===== 反转低换手 前瞻回测 · 样本 {used} 只 · 观测 {len(panel)} · "
          f"{res['交易日数']} 交易日 · 往返成本 {roundtrip_bps}bps · 流动性过滤≥{min_liq_pct} =====")
    print("(横截面·无未来函数;net 超额为正才算能交易;非投资建议)\n")

    for fname, col in factors.items():
        res["结果"][fname] = {}
        print(f"########## 因子:{fname} ##########")
        for N in horizons:
            tmp = panel.rename(columns={col: "score"})
            ic = ic_metrics(tmp, N)
            dec = decile_metrics(tmp, N)
            tk = topk_net_metrics(panel, col, N, k=topk, roundtrip_bps=roundtrip_bps)
            res["结果"][fname][f"{N}日"] = {"IC": ic, "分层": dec, "TopK净额": tk}
            d0 = dec[0]["均收益%"]; d9 = dec[9]["均收益%"]
            mono = round((d9 - d0), 2) if (d0 is not None and d9 is not None) else None
            print(f"  —— {N}日 ——  IC均值={ic.get('IC均值')} ICIR={ic.get('ICIR')} "
                  f"t={ic.get('t')} | 分层D9-D0={mono}pp | "
                  f"TopK: gross年化={tk.get('gross年化超额%')}% net年化={tk.get('net年化超额%')}% "
                  f"换手={tk.get('组合换手率')} net正={tk.get('net是否为正')}")
        print()

    _verdict(res, horizons)
    if json_path:
        from pathlib import Path
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


def _verdict(res: dict, horizons) -> None:
    """诚实判定:复合的 net 增量是否成立(相对纯 rev5)。写入 res['判定'] 并打印。"""
    comp = res["结果"].get("composite", {})
    rev_key = next((k for k in res["结果"] if k.startswith("rev")), None)
    rev = res["结果"].get(rev_key, {}) if rev_key else {}
    lines = []
    for N in horizons:
        c = (comp.get(f"{N}日", {}) or {}).get("TopK净额", {})
        r = (rev.get(f"{N}日", {}) or {}).get("TopK净额", {})
        cn = c.get("net年化超额%"); rn = r.get("net年化超额%")
        cic = (comp.get(f"{N}日", {}) or {}).get("IC", {}).get("ICIR")
        if cn is None or rn is None:
            continue
        incr = round(cn - rn, 1)
        verdict = "复合净额增量成立" if (cn > 0 and incr > 0) else (
            "复合net>0但无相对rev5增量" if cn > 0 else "复合net≤0(扣真实成本后不可交易)")
        lines.append(f"  {N}日: composite net年化={cn}% vs {rev_key} net年化={rn}% "
                     f"(增量{incr:+}pp) · composite ICIR={cic} → {verdict}")
    res["判定"] = lines
    print("========== 诚实判定(net 为准,gross 不算数)==========")
    for ln in lines:
        print(ln)
    if not lines:
        print("  (无足够数据判定)")
    print()


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="反转低换手 前瞻回测(因子有效性+真实成本净额+对齐rev5)")
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--horizon", default="5,10,20")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--roundtrip-bps", type=float, default=17.5, help="往返成本(印花税+佣金+滑点),默认17.5")
    ap.add_argument("--min-liq-pct", type=float, default=0.0, help="每日剔除成交额分位<此值的票(0~1)")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    codes = [c for c in a.codes.split(",") if c] or None
    if a.sample:
        import random
        allc = sorted(store.list_master_codes())
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    run(codes=codes, horizons=tuple(int(x) for x in a.horizon.split(",")),
        step=a.step, topk=a.topk, roundtrip_bps=a.roundtrip_bps,
        min_liq_pct=a.min_liq_pct, json_path=a.json or None)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
