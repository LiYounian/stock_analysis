"""动量高位超买抑制层 A/B 前瞻回测(A=纯动量 vs B=+高位超买抑制)——达标才接生产。

口径源自 docs/每日分析/策略建议/动量策略高位超买抑制层.md §4。

复用:
  · 动量打分 tools.strategy.momentum.weighted_log_momentum + laplace_trend_signal(R²+拉普拉斯闸门,
    与 screen_momentum 同口径)——panel 只保留过闸的动量候选。
  · 超买/涨幅特征 tools.analysis.technical(kdj/rsi/ma + _overbought_oversold)——逐 t 复算(无未来函数)。
  · 抑制裁决 tools.strategy.momentum_overheat.overheat_verdict + sort_key(纯函数,吃 panel 预抽特征)。

每调仓日取动量分 TopK(A 池);B = 对同一候选池叠加抑制层分层重排后取 TopK(超买透支票被挤出、
健康强票回填)。A/B 指标:
  ① TopK 前瞻收益 / 胜率(全样本 B 相对 A **不劣化**;动量 edge 主要在 h=1,如实呈现多尺度);
  ② **高位超买子样本**(入选时抑制触发)的踩雷率/前瞻收益,B(挤出)相对 A 是否显著改善;
  ③ **不误杀**:被挤出票(A有B无)的前瞻收益应低于保留票——否则=误杀好票。

防未来函数(硬红线):动量/特征只读 close[:t+1] 尾部;前瞻收益/踩雷取 t 之后价仅作被预测标签。
⚠️ 非投资建议;历史回测≠未来保证;产物只写 worktree 本地,不写主检出、不动 main。

用法:python -m tools.backtest.backtest_momentum_overheat [--sample N] [--seed 42] [--step K]
      [--horizon 1,5,10] [--topk 10] [--dd-horizon 10] [--dd-thresh -12]
      [--data-root /path/to/repo/data] [--json out.json]
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.momentum_overheat")
_DISCLAIMER = "历史回测≠未来保证,非投资建议。"


# ————————————————————————— 数据根解析(worktree 兼容,仿 backtest_reversal_veto)—————————————————————————
def _resolve_data_root(cli_root: str | None) -> Path | None:
    from tools.config import settings
    for cand in (cli_root, os.getenv("STOCK_DATA_ROOT")):
        if cand:
            p = Path(cand).expanduser().resolve()
            if (p / "master" / "kline").exists():
                return p
            print(f"[warn] 指定 data-root 无 master/kline:{p}", file=sys.stderr)
    default_master = settings.DATA_MASTER / "kline"
    if default_master.exists() and any(default_master.glob("*.parquet")):
        return None
    try:
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(settings.PROJECT_ROOT), text=True).strip()
        cand = Path(common).resolve().parent / "data"
        if (cand / "master" / "kline").exists():
            print(f"[info] worktree 无本地主档,自动使用主仓数据根:{cand}", file=sys.stderr)
            return cand
    except Exception as e:                                 # noqa: BLE001
        print(f"[warn] 自动探测主仓失败:{e!r}", file=sys.stderr)
    return None


def _apply_data_root(root: Path | None) -> None:
    if root is None:
        return
    from tools.config import settings
    from tools.store import repo as store
    store._RAW_DIR = root / "raw"
    store._MASTER_DIR = root / "master"
    settings.DATA_RAW = root / "raw"
    settings.DATA_MASTER = root / "master"


# ————————————————————————— 回测用抑制 config —————————————————————————
def _oh_cfg() -> dict:
    """回测用抑制配置:读 config 默认块,强制启用(A/B 的 B 腿)。"""
    from tools.config.strategy import THRESHOLDS
    c = copy.deepcopy(THRESHOLDS.get("动量高位超买抑制", {}) or {})
    c["启用"] = True
    c["模式"] = "软降级"                                    # 分层重排等价于把超买票挤出 TopK
    return c


# ————————————————————————— 建 panel(动量候选 + 超买特征 + 前瞻收益 + 踩雷标签)—————————————————————————
def build_panel(codes, lookback=25, r2_min=0.4, s=0.07, min_slope=0.002,
                horizons=(1, 5, 10), step=1, warmup=120, ret_win=10,
                dd_horizon=10, dd_thresh=-12.0) -> pd.DataFrame:
    """逐票逐日算动量分(过 R²+拉普拉斯闸门的候选)+ 超买/涨幅特征 + 前瞻收益 + 踩雷标签。无未来函数。"""
    from tools.analysis import technical as ta
    from tools.collectors import market
    from tools.strategy.momentum import laplace_trend_signal, weighted_log_momentum

    maxN = max(max(horizons), dd_horizon)
    rows = []
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:                                  # noqa: BLE001
            continue
        if df is None or len(df) < warmup + maxN + 5 or "close" not in df.columns:
            continue
        df = df.reset_index(drop=True)
        close = df["close"].to_numpy(float)
        low = df["low"].to_numpy(float) if "low" in df.columns else close
        dates = [str(x)[:10] for x in df["date"].tolist()]
        n = len(df)
        # 全序列指标(一次算,逐 t 索引;无未来函数:t 处的值只用 ≤t 数据)
        has_hl = {"high", "low"}.issubset(df.columns)
        kd = ta.kdj(df) if has_hl else None
        k_arr = kd["k"].to_numpy(float) if kd is not None else np.full(n, np.nan)
        j_arr = kd["j"].to_numpy(float) if kd is not None else np.full(n, np.nan)
        rsi12 = ta.rsi(df["close"], 12).to_numpy(float)
        ma20 = ta.ma(df["close"], 20).to_numpy(float)
        lap = laplace_trend_signal(close.tolist(), s=s, min_slope=min_slope)
        used += 1
        for t in range(warmup, n - maxN, step):
            # 动量闸门(与 screen_momentum 同口径):R² + 拉普拉斯末根='买'
            mom = weighted_log_momentum(close[: t + 1], lookback_days=lookback)
            if mom.get("r_squared", 0.0) < r2_min:
                continue
            if lap[t] != "买":
                continue
            if close[t] <= 0:
                continue
            score = float(mom["score"])
            # 超买/涨幅特征(as-of:只用 ≤t)
            m20 = ma20[t]
            bias = float((close[t] - m20) / m20 * 100.0) if (m20 and not np.isnan(m20)) else np.nan
            ret_n = (float(close[t] / close[t - ret_win] - 1.0) * 100.0
                     if (t >= ret_win and close[t - ret_win] > 0) else np.nan)
            ob = ta._overbought_oversold(k_arr[t], j_arr[t], rsi12[t], bias)
            # 踩雷:T+1..T+dd_horizon 最低价相对建仓价的最大回撤(用 low,贴盘中触发)
            path_low = low[t + 1: t + 1 + dd_horizon]
            dd = float(path_low.min() / close[t] - 1.0) * 100.0 if len(path_low) else np.nan
            row = {"date": dates[t], "code": code, "score": score,
                   "ob_verdict": ob.get("verdict"), "ob_reson": int(ob.get("resonance") or 0),
                   "bias20": None if np.isnan(bias) else float(bias),
                   "ret_n": None if np.isnan(ret_n) else float(ret_n),
                   "ret_window": ret_win,
                   "dd": dd, "踩雷": bool(np.isfinite(dd) and dd <= dd_thresh)}
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


# ————————————————————————— A/B 评测 —————————————————————————
def annotate_triggers(panel: pd.DataFrame, c: dict) -> pd.DataFrame:
    """给 panel 加 `_trig`(抑制是否触发)列——DRY 复用 overheat_verdict(不在此重实现阈值)。
    逐行调纯裁决函数(dict 操作,廉价);panel 量级(万级)可接受。"""
    from tools.strategy import momentum_overheat as oh
    trig = []
    for _, row in panel.iterrows():
        feats = {"ob_os_verdict": row.get("ob_verdict"), "ob_os_resonance": row.get("ob_reson"),
                 "bias20": row.get("bias20"), "ret_n": row.get("ret_n"),
                 "ret_window": row.get("ret_window")}
        trig.append(bool(oh.overheat_verdict(feats, c).get("触发")))
    out = panel.copy()
    out["_trig"] = trig
    return out


def run_ab(panel: pd.DataFrame, topk=10, horizons=(1, 5, 10), step_reb=1, min_cross=10) -> dict:
    """每调仓日取动量分 TopK(A);对全候选池叠加抑制层分层重排取 TopK(B)。
    统计 A/B 胜率/前瞻收益/踩雷率 + 高位超买子样本 + 挤出/保留/回填子样本。"""
    c = _oh_cfg()
    panel = annotate_triggers(panel, c)                    # 一次性标注触发(DRY)
    all_dates = sorted(panel["date"].unique())
    rebal_dates = all_dates[::step_reb]

    a_rows, b_rows = [], []                # A/B 的 TopK 入选票
    trig_rows = []                         # 高位超买子样本(入选 A 且触发)
    squeezed_rows, retained_rows, added_rows = [], [], []   # 挤出 / 保留 / 回填
    for d in rebal_dates:
        g = panel[panel["date"] == d]
        if len(g) < min_cross:
            continue
        gs = g.sort_values("score", ascending=False)
        kk = min(topk, len(gs) // 2)
        if kk < 1:
            continue
        recs = [{k: r[k] for k in r.index} for _, r in gs.iterrows()]
        a_top = recs[:kk]                                  # A:纯动量 TopK
        # B:分层重排(触发→tier1 沉到 clean 之后;同层保动量序),取 TopK
        b_top = sorted(recs, key=lambda r: (1 if r["_trig"] else 0, -r["score"]))[:kk]

        b_codes = {r["code"] for r in b_top}
        a_codes = {r["code"] for r in a_top}
        for r in a_top:
            a_rows.append(r)
            if r["_trig"]:
                trig_rows.append(r)
            (retained_rows if r["code"] in b_codes else squeezed_rows).append(r)
        for r in b_top:
            b_rows.append(r)
            if r["code"] not in a_codes:
                added_rows.append(r)                       # B 有 A 无 = 健康回填

    def _stats(rows):
        if not rows:
            return {"n": 0}
        dfr = pd.DataFrame(rows)
        out = {"n": int(len(dfr)), "踩雷率%": round(float(dfr["踩雷"].mean()) * 100, 2)}
        for N in horizons:
            col = f"r_{N}"
            out[f"胜率{N}日%"] = round(float((dfr[col] > 0).mean()) * 100, 2)
            out[f"均收益{N}日%"] = round(float(dfr[col].mean()), 3)
        return out

    trig_all = panel[panel["_trig"]]                       # 全 panel 触发票(基率)
    return {
        "调仓次数": int(len(rebal_dates)),
        "A(纯动量)TopK": _stats(a_rows),
        "B(加抑制层)TopK": _stats(b_rows),
        "高位超买子样本(入选A且触发)": _stats(trig_rows),
        "被挤出票(A有B无)": _stats(squeezed_rows),
        "保留票(A∩B)": _stats(retained_rows),
        "回填票(B有A无)": _stats(added_rows),
        "全panel高位超买触发基率踩雷%": (round(float(trig_all["踩雷"].mean()) * 100, 2)
                                    if len(trig_all) else None),
        "全panel高位超买触发数": int(len(trig_all)),
    }


def _verdict_lines(ab: dict, horizons) -> list[str]:
    a = ab["A(纯动量)TopK"]; b = ab["B(加抑制层)TopK"]
    sq = ab["被挤出票(A有B无)"]; rt = ab["保留票(A∩B)"]; tr = ab["高位超买子样本(入选A且触发)"]
    lines = []
    if a.get("n") and b.get("n"):
        lines.append(f"TopK 踩雷率: A={a.get('踩雷率%')}% → B={b.get('踩雷率%')}%"
                     f"(降 {round(a.get('踩雷率%', 0) - b.get('踩雷率%', 0), 2)}pp)")
        for N in horizons:
            lines.append(f"  {N}日: 胜率 A={a.get(f'胜率{N}日%')}%→B={b.get(f'胜率{N}日%')}% | "
                         f"均收益 A={a.get(f'均收益{N}日%')}%→B={b.get(f'均收益{N}日%')}%")
    if sq.get("n"):
        lines.append(f"被挤出票 n={sq['n']} 踩雷率={sq.get('踩雷率%')}%(vs 保留票 "
                     f"{rt.get('踩雷率%')}%,越高=抑制越准);"
                     + " ".join(f"{N}日均收益 挤出={sq.get(f'均收益{N}日%')}%/保留={rt.get(f'均收益{N}日%')}%"
                                for N in horizons))
        lines.append("  ↳ 不误杀判据:挤出票前瞻收益应 ≤ 保留票(否则=错杀好票)")
    if tr.get("n"):
        lines.append(f"高位超买子样本(入选A且触发) n={tr['n']} 踩雷率={tr.get('踩雷率%')}% "
                     + " ".join(f"{N}日均收益={tr.get(f'均收益{N}日%')}%" for N in horizons))
    return lines


def run(codes, horizons=(1, 5, 10), step=1, step_reb=1, topk=10, lookback=25,
        ret_win=10, dd_horizon=10, dd_thresh=-12.0, warmup=120, json_path=None) -> dict:
    panel = build_panel(codes, lookback=lookback, horizons=horizons, step=step, warmup=warmup,
                        ret_win=ret_win, dd_horizon=dd_horizon, dd_thresh=dd_thresh)
    if panel.empty:
        print("!! panel 为空(样本无足量长历史 kline 或无过闸动量候选)")
        return {"error": "空panel"}
    used = int(panel.attrs.get("used", 0))
    ab = run_ab(panel, topk=topk, horizons=horizons, step_reb=step_reb)
    res = {
        "策略": "动量高位超买抑制层 A/B 前瞻回测",
        "参数": {"lookback": lookback, "topk": topk, "step": step, "step_reb": step_reb,
                 "涨幅窗口": ret_win, "踩雷窗口": dd_horizon, "踩雷阈值%": dd_thresh,
                 "抑制config": _oh_cfg()},
        "样本股数": used, "总观测(过闸动量候选)": int(len(panel)),
        "交易日数": int(panel["date"].nunique()),
        "全样本踩雷基率%": round(float(panel["踩雷"].mean()) * 100, 2),
        "A/B": ab, "免责": _DISCLAIMER,
    }
    res["判定"] = _verdict_lines(ab, horizons)
    print(f"\n===== 动量高位超买抑制层 A/B · 样本 {used} 只 · 过闸候选 {len(panel)} · "
          f"{res['交易日数']} 交易日 · 踩雷={dd_horizon}日maxDD≤{dd_thresh}% =====")
    print(f"(全样本踩雷基率 {res['全样本踩雷基率%']}%;全panel高位超买触发 "
          f"{ab['全panel高位超买触发数']} 观测/基率踩雷 {ab['全panel高位超买触发基率踩雷%']}%)")
    print(f"A TopK n={ab['A(纯动量)TopK'].get('n')} / B TopK n={ab['B(加抑制层)TopK'].get('n')} / "
          f"被挤出 {ab['被挤出票(A有B无)'].get('n')} 只\n")
    for ln in res["判定"]:
        print(ln)
    print()
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="动量高位超买抑制层 A/B 前瞻回测")
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--step", type=int, default=1, help="建 panel 的逐票采样步长(交易日)")
    ap.add_argument("--step-reb", type=int, default=1, help="调仓步长(交易日;默认每日调仓)")
    ap.add_argument("--horizon", default="1,5,10")
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument("--ret-win", type=int, default=10)
    ap.add_argument("--dd-horizon", type=int, default=10)
    ap.add_argument("--dd-thresh", type=float, default=-12.0)
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    root = _resolve_data_root(a.data_root)
    _apply_data_root(root)
    from tools.store import repo as store

    if a.codes:
        codes = [c for c in a.codes.split(",") if c]
    elif a.sample:
        import random
        allc = sorted(store.list_master_codes())
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    else:
        codes = sorted(store.list_master_codes())[:300]
    print(f"票池 {len(codes)} 只", file=sys.stderr)
    run(codes=codes, horizons=tuple(int(x) for x in a.horizon.split(",")),
        step=a.step, step_reb=a.step_reb, topk=a.topk, ret_win=a.ret_win,
        dd_horizon=a.dd_horizon, dd_thresh=a.dd_thresh, json_path=a.json or None)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
