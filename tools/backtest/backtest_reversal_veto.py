"""反转否决层 A/B 前瞻回测(A=纯量价反转 vs B=+否决层)——达标才接生产。

口径源自 docs/每日分析/策略建议/反转策略否决层.md §4 + docs/计划/反转策略否决层_实现方案.md §4。

复用 `backtest_reversal_turnover.add_scores`(逐日横截面 winsor+zscore 复合)与
`reversal_turnover` 纯因子;新增:
  · 逐 (code, date) 的**踩雷标签**(纯价、可长历史算):T+DD 窗内最大回撤 ≤ 阈值(默认 -15%)。
  · 逐 A-TopK 高分票的 **as-of 否决裁决**(reversal_veto,回测默认只开可长历史回溯的
    基本面轴 + ST 治理轴;事件/龙虎榜轴数据仅近月快照,不足长历史严格回测 → 默认关,诚实说明)。
A/B 指标(每调仓 A-TopK 高分票池):
  ① T+N 胜率 / 前瞻收益(B 相对 A **不劣化**);
  ② **踩雷率**(高分票 T+DD 内 maxDD ≤ 阈值占比),B 是否显著降;
  ③ **误杀率**(被 B 否决的高分票里,**非踩雷**的占比 = 好票被错杀比例)。
达标:踩雷率显著降 + 胜率/收益不劣化。数据不足以严格回测的轴**如实说明**。

防未来函数(硬红线):因子只读 kdf[:t+1] 尾部;前瞻收益/踩雷取 t 之后价仅作被预测标签;
  否决裁决走 reversal_veto.extract_features(财报 disclosure_date<=as_of / ST 名称近似)。
⚠️ 非投资建议;历史回测≠未来保证;产物只写 worktree 本地,不写主检出、不动 main。

用法:python -m tools.backtest.backtest_reversal_veto [--sample N] [--seed 42] [--step K]
      [--horizon 5,10] [--topk 20] [--dd-horizon 10] [--dd-thresh -15]
      [--fin-only] [--data-root /path/to/repo/data] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.reversal_veto")
_DISCLAIMER = "历史回测≠未来保证,非投资建议。"


# ————————————————————————— 数据根解析(worktree 兼容,仿 validate_adaptive_rr)—————————————————————————
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


# ————————————————————————— 建 panel(rev/turn + 前瞻收益 + 踩雷标签)—————————————————————————
def build_panel(codes, rev_n, turn_n, horizons=(5, 10), step=1, warmup=120,
                dd_horizon=10, dd_thresh=-15.0) -> pd.DataFrame:
    """逐票逐日算 rev/turn 原始因子 + 前瞻收益 + 踩雷标签(T+dd_horizon 内最大回撤 ≤ 阈值)。无未来函数。"""
    from tools.collectors import market
    from tools.strategy.reversal_turnover import low_turnover_factor, reversal_factor

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
        turn_arr = (df["turnover"].to_numpy(float) if "turnover" in df.columns
                    else np.full(len(df), np.nan))
        dates = [str(x)[:10] for x in df["date"].tolist()]
        n = len(df)
        used += 1
        for t in range(warmup, n - maxN, step):
            rev = reversal_factor(close[: t + 1], n=rev_n)
            turn = low_turnover_factor(turn_arr[: t + 1], n=turn_n)
            if rev is None or turn is None or not (np.isfinite(rev) and np.isfinite(turn)):
                continue
            if close[t] <= 0:
                continue
            # 踩雷:T+1..T+dd_horizon 的最低价相对建仓价的最大回撤(用 low 更贴实盘盘中触发)
            path_low = low[t + 1: t + 1 + dd_horizon]
            dd = float(path_low.min() / close[t] - 1.0) * 100.0 if len(path_low) else np.nan
            row = {"date": dates[t], "code": code, "rev": float(rev), "turn": float(turn),
                   "dd": dd, "踩雷": bool(np.isfinite(dd) and dd <= dd_thresh)}
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


# ————————————————————————— A/B 评测 —————————————————————————
def _veto_cfg(fin_only: bool) -> dict:
    """回测用否决配置:默认只开可长历史回溯的轴(基本面空心 + 治理风险 ST);
    事件/龙虎榜/重组轴数据仅近月快照,长历史不可信 → 关(诚实)。fin_only=True 时连 ST 也关(纯财报轴)。"""
    from tools.config.strategy import THRESHOLDS
    import copy
    c = copy.deepcopy(THRESHOLDS.get("反转否决层", {}))
    c["启用"] = True
    c["模式"] = "否决"                                      # 回测按硬否决评"剔除后"效果(降级等价把分沉底)
    c["否决沉底保留展示"] = True
    axes = c.setdefault("轴", {})
    axes.setdefault("基本面空心", {})["启用"] = True
    axes.setdefault("治理风险", {})["启用"] = not fin_only
    axes.setdefault("事件博弈", {})["启用"] = False        # 数据仅近月,长历史回测关
    axes.setdefault("重组未完成", {})["启用"] = False
    return c


def run_ab(panel: pd.DataFrame, topk=20, horizons=(5, 10), w_rev=0.5, w_turn=0.5,
           fin_only=False, verdict_cache: dict | None = None, min_cross=10) -> dict:
    """每调仓日取 composite TopK(A 高分池);对其逐票 as-of 否决裁决 → B(否决剔除)。
    统计 A/B 的胜率/前瞻收益/踩雷率/误杀率。"""
    from tools.backtest.backtest_reversal_turnover import add_scores
    from tools.strategy import reversal_veto as rv

    c = _veto_cfg(fin_only)
    panel = add_scores(panel, w_rev, w_turn)
    verdict_cache = verdict_cache if verdict_cache is not None else {}

    def _verdict(code, as_of):
        key = (code, as_of)
        if key not in verdict_cache:
            try:
                feats = rv.extract_features(code, as_of, c=c)
                verdict_cache[key] = rv.veto_verdict(feats, c)
            except Exception:                              # noqa: BLE001
                verdict_cache[key] = {"触发": False, "否决": False}
        return verdict_cache[key]

    all_dates = sorted(panel["date"].unique())
    N_reb = max(horizons)
    rebal_dates = all_dates[::N_reb]                       # 非重叠调仓

    a_rows, b_rows, vetoed_rows = [], [], []
    for d in rebal_dates:
        g = panel[panel["date"] == d]
        if len(g) < min_cross:
            continue
        gs = g.sort_values("score_composite", ascending=False)
        kk = min(topk, len(gs) // 2)
        if kk < 1:
            continue
        top = gs.head(kk)
        for _, r in top.iterrows():
            v = _verdict(str(r["code"]), str(r["date"]))
            rec = {k: r[k] for k in r.index}
            rec["vetoed"] = bool(v.get("触发"))
            a_rows.append(rec)                             # A:全部高分票
            if v.get("触发"):
                vetoed_rows.append(rec)
            else:
                b_rows.append(rec)                         # B:未被否决的高分票

    def _stats(rows):
        if not rows:
            return {"n": 0}
        dfr = pd.DataFrame(rows)
        out = {"n": int(len(dfr)),
               "踩雷率%": round(float(dfr["踩雷"].mean()) * 100, 2)}
        for N in horizons:
            col = f"r_{N}"
            out[f"胜率{N}日%"] = round(float((dfr[col] > 0).mean()) * 100, 2)
            out[f"均收益{N}日%"] = round(float(dfr[col].mean()), 3)
        return out

    a_stat, b_stat = _stats(a_rows), _stats(b_rows)
    v_stat = _stats(vetoed_rows)
    # 误杀率 = 被否决高分票里 非踩雷 的占比(好票被错杀比例)
    n_veto = len(vetoed_rows)
    n_veto_trap = int(sum(1 for r in vetoed_rows if r["踩雷"])) if n_veto else 0
    误杀率 = round((1 - n_veto_trap / n_veto) * 100, 2) if n_veto else None
    否决命中踩雷率 = round(n_veto_trap / n_veto * 100, 2) if n_veto else None

    return {
        "调仓次数": len([d for d in rebal_dates]),
        "A(纯量价)": a_stat, "B(加否决层)": b_stat, "被否决高分票": v_stat,
        "否决票数": n_veto, "否决命中踩雷率%": 否决命中踩雷率, "误杀率%": 误杀率,
        "开启轴": [k for k, x in (c.get("轴") or {}).items() if x.get("启用")],
        "关闭轴(数据不足长历史)": [k for k, x in (c.get("轴") or {}).items() if not x.get("启用")],
    }


def _verdict_lines(res: dict, horizons) -> list[str]:
    ab = res["A/B"]
    a = ab["A(纯量价)"]; b = ab["B(加否决层)"]; v = ab.get("被否决高分票", {})
    lines = []
    if a.get("n") and b.get("n"):
        dt = a.get("踩雷率%"); dtb = b.get("踩雷率%")
        lines.append(f"踩雷率: A={dt}% → B={dtb}%(降 {round(dt - dtb, 2)}pp);"
                     f"被否决票踩雷率={v.get('踩雷率%')}%(vs 保留票 B={dtb}%,越高=否决越准)")
        for N in horizons:
            wa, wb = a.get(f"胜率{N}日%"), b.get(f"胜率{N}日%")
            ra, rb = a.get(f"均收益{N}日%"), b.get(f"均收益{N}日%")
            rv = v.get(f"均收益{N}日%")
            lines.append(f"  {N}日: 胜率 A={wa}%→B={wb}% | 均收益 A={ra}%→B={rb}% "
                         f"| 被否决票均收益={rv}%(低于保留票=否决剔的是差票,不误杀)")
        # 严格「误杀率」(=被否决里非踩雷占比;traps 稀有→天然偏高,仅作参考,以收益对比为准)
        lines.append(f"  参考·严格误杀率(被否决里非踩雷占比,traps稀有事件→偏高)={ab.get('误杀率%')}%")
    return lines


def run(codes, horizons=(5, 10), step=1, topk=20, rev_n=5, turn_n=20,
        dd_horizon=10, dd_thresh=-15.0, fin_only=False, warmup=120, json_path=None) -> dict:
    panel = build_panel(codes, rev_n, turn_n, horizons=horizons, step=step,
                        warmup=warmup, dd_horizon=dd_horizon, dd_thresh=dd_thresh)
    if panel.empty:
        print("!! panel 为空(样本无足量长历史 kline)"); return {"error": "空panel"}
    used = int(panel.attrs.get("used", 0))
    ab = run_ab(panel, topk=topk, horizons=horizons, fin_only=fin_only)
    res = {
        "策略": "反转否决层 A/B 前瞻回测",
        "参数": {"反转窗口": rev_n, "换手窗口": turn_n, "topk": topk, "step": step,
                 "踩雷窗口": dd_horizon, "踩雷阈值%": dd_thresh, "fin_only": fin_only},
        "样本股数": used, "总观测": int(len(panel)), "交易日数": int(panel["date"].nunique()),
        "全样本踩雷基率%": round(float(panel["踩雷"].mean()) * 100, 2),
        "A/B": ab, "免责": _DISCLAIMER,
        "数据可用性说明": ("财报 raw 单快照含~12期(disclosure_date),基本面轴可 as-of 回溯~3年,"
                       "但仅约175只有财报快照;ST 用当前名称近似(状态粘性);事件/龙虎榜轴数据仅"
                       "近月快照(~24采集日),不足长历史严格回测→本回测默认关。踩雷/收益为纯价、可长历史算。"),
    }
    res["判定"] = _verdict_lines(res, horizons)
    print(f"\n===== 反转否决层 A/B · 样本 {used} 只 · 观测 {len(panel)} · "
          f"{res['交易日数']} 交易日 · 踩雷={dd_horizon}日maxDD≤{dd_thresh}% =====")
    print(f"(全样本踩雷基率 {res['全样本踩雷基率%']}%;开启轴 {ab['开启轴']};"
          f"关闭轴 {ab['关闭轴(数据不足长历史)']})")
    print(f"A 高分票 n={ab['A(纯量价)'].get('n')} / B n={ab['B(加否决层)'].get('n')} / "
          f"否决 {ab['否决票数']} 只\n")
    for ln in res["判定"]:
        print(ln)
    print()
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


def _fin_covered_codes(root: Path | None) -> list[str]:
    """有财报 raw 快照的代码(基本面轴有数据的票池)。root=None → 用 store 默认。"""
    from tools.config import settings
    base = (root / "raw") if root else settings.DATA_RAW
    codes: set[str] = set()
    if base.exists():
        for p in base.glob("*/financial_report/*.json"):
            stem = p.stem
            if len(stem) == 6 and stem.isdigit():
                codes.add(stem)
    return sorted(codes)


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="反转否决层 A/B 前瞻回测")
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--horizon", default="5,10")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--dd-horizon", type=int, default=10)
    ap.add_argument("--dd-thresh", type=float, default=-15.0)
    ap.add_argument("--fin-only", action="store_true", help="只开基本面轴(连 ST 治理轴也关)")
    ap.add_argument("--fin-universe", action="store_true",
                    help="票池限定为有财报快照的票(基本面轴有数据,A/B 更可比)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)

    root = _resolve_data_root(a.data_root)
    _apply_data_root(root)
    from tools.store import repo as store

    if a.codes:
        codes = [c for c in a.codes.split(",") if c]
    elif a.fin_universe:
        codes = _fin_covered_codes(root)
        if a.sample:
            import random
            codes = random.Random(a.seed).sample(codes, min(a.sample, len(codes)))
    elif a.sample:
        import random
        allc = sorted(store.list_master_codes())
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    else:
        codes = sorted(store.list_master_codes())[:300]
    print(f"票池 {len(codes)} 只(fin_universe={a.fin_universe}, fin_only={a.fin_only})", file=sys.stderr)
    run(codes=codes, horizons=tuple(int(x) for x in a.horizon.split(",")),
        step=a.step, topk=a.topk, dd_horizon=a.dd_horizon, dd_thresh=a.dd_thresh,
        fin_only=a.fin_only, json_path=a.json or None)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
