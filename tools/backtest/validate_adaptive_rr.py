"""自适应盈亏比(RR)vs 固定 1.33 · A/B 回测验证(数据说话,非投资建议)。

复用 `backtest_predict.build_panel` 的**严格无未来函数**逐日 predict 循环:对同一 sample/seed,
只切换 `THRESHOLDS['预测']['盈亏比自适应']` 开关、各重建一遍 panel,横向比对**波动率括号**
(持有期建议)的:
  - 实测均值%(先摸止盈→+目标 / 先摸止损→−止损 / 到期→收盘 mark-to-close)
  - TP先% / SL先% / 止损触发率%(**应不变**——只动止盈目标、不动止损带宽)
  - 名义E%(先命中口径期望)、均目标%
并统计 RR 分布(证明不再恒定 1.33 + 自适应覆盖率)——直接取自**自适应 panel** 每行
`br_gain/br_loss`(逐观测、天然无未来函数),不再单开一遍全历史 predict。

—— A/B 脚本空 panel bug 根因(2026-09-01 修)——
`_sample_universe` → `store.list_master_codes()` 读 `store._MASTER_DIR/kline`。在**隔离 worktree**
里该目录不存在(主档数据只在主仓 `data/master`)→ 返回空 codes → build_panel 迭代空 → 空 DataFrame
→ 无 'N' 列 → 下游 `panel[panel['N']==N]` 抛 `KeyError: 'N'`。
修:本脚本启动时**解析真实数据根**(`--data-root` / 环境变量 `STOCK_DATA_ROOT` / 自动探测主仓),
monkeypatch `store._MASTER_DIR`+`store._RAW_DIR` 指过去;并对空 panel 显式报错(而非 KeyError)。

性能:逐日 predict 偏慢(~20s/票·单遍),默认用 multiprocessing 跨核并行(`--jobs`,默认 CPU-2);
spawn 语义下每个 worker 由 initializer 重设数据根 + 自适应开关。

用法:
  python -m tools.backtest.validate_adaptive_rr [n_sample] [step] [seed]
  python -m tools.backtest.validate_adaptive_rr --sample 300 --step 5 --seed 42 --jobs 9
  python -m tools.backtest.validate_adaptive_rr --sample 300 --data-root /path/to/repo/data
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd


# ————————————————————————— 数据根解析(worktree 兼容) —————————————————————————
def _resolve_data_root(cli_root: str | None) -> Path | None:
    """解析含 master/ + raw/ 的真实数据根;None=用 store 默认(主仓正常跑)。

    优先级:--data-root > 环境变量 STOCK_DATA_ROOT > store 默认(若主档非空)> 自动探测主仓 data/。
    """
    from tools.config import settings

    for cand in (cli_root, os.getenv("STOCK_DATA_ROOT")):
        if cand:
            p = Path(cand).expanduser().resolve()
            if (p / "master" / "kline").exists():
                return p
            print(f"[warn] 指定 data-root 无 master/kline:{p}", file=sys.stderr)

    default_master = settings.DATA_MASTER / "kline"
    if default_master.exists() and any(default_master.glob("*.parquet")):
        return None  # store 默认已可用,不必 monkeypatch

    try:  # worktree 场景:git 公共目录父级=主仓根
        common = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(settings.PROJECT_ROOT), text=True,
        ).strip()
        cand = Path(common).resolve().parent / "data"
        if (cand / "master" / "kline").exists():
            print(f"[info] worktree 无本地主档,自动使用主仓数据根:{cand}", file=sys.stderr)
            return cand
    except Exception as e:  # pragma: no cover
        print(f"[warn] 自动探测主仓失败:{e!r}", file=sys.stderr)
    return None


def _apply_data_root(root_str: str | None) -> None:
    if not root_str:
        return
    from tools.store import repo as store
    root = Path(root_str)
    store._MASTER_DIR = root / "master"
    store._RAW_DIR = root / "raw"


# ————————————————————————— 并行 build(spawn 安全) —————————————————————————
# worker 全局(由 initializer 设定)
_W_HZ: tuple = (1, 5, 10)
_W_STEP: int = 5


def _worker_init(root_str, flag, horizons, step):
    """spawn worker 初始化:重设数据根 + 自适应开关 + 全局 horizons/step。"""
    global _W_HZ, _W_STEP
    _apply_data_root(root_str)
    from tools.config.strategy import THRESHOLDS
    THRESHOLDS["预测"]["盈亏比自适应"] = flag
    _W_HZ, _W_STEP = tuple(horizons), int(step)


def _worker_build(code):
    """单票 build_panel → (records, used)。异常吞掉(返回空),不炸整池。"""
    try:
        from tools.backtest import backtest_predict as bp
        p = bp.build_panel([code], _W_HZ, step=_W_STEP)
        return p.to_dict("records"), int(p.attrs.get("used", 0))
    except Exception:
        return [], 0


def build_panel_parallel(codes, flag, horizons, step, root_str, jobs) -> pd.DataFrame:
    """跨核并行重建 panel(单遍=一个自适应开关档)。jobs≤1 走串行。"""
    if jobs and jobs > 1:
        import multiprocessing as mp
        rows, used = [], 0
        with mp.Pool(jobs, initializer=_worker_init,
                     initargs=(root_str, flag, horizons, step)) as pool:
            for recs, u in pool.imap_unordered(_worker_build, codes, chunksize=1):
                rows.extend(recs)
                used += u
        panel = pd.DataFrame(rows)
        panel.attrs["used"] = used
        return panel
    # 串行回退
    from tools.backtest import backtest_predict as bp
    from tools.config.strategy import THRESHOLDS
    THRESHOLDS["预测"]["盈亏比自适应"] = flag
    return bp.build_panel(codes, horizons, step=step)


# ————————————————————————— 报告 —————————————————————————
def _fmt(d: dict, cols) -> str:
    return "  ".join(
        f"{k}={d.get(k):+.2f}" if isinstance(d.get(k), float) else f"{k}={d.get(k)}"
        for k in cols)


def _rr_from_panel(pa: pd.DataFrame, N: int = 5):
    """从自适应 panel 逐观测取 RR = br_gain/br_loss(rounded,足够看分布)。"""
    d = pa[(pa["N"] == N) & pa["br_gain"].notna() & pa["br_loss"].notna()].copy()
    rr = (d["br_gain"] / d["br_loss"]).replace([np.inf, -np.inf], np.nan).dropna()
    return rr.to_numpy()


def run_ab(n_sample, step, seed, horizons=(1, 5, 10), data_root=None, jobs=None, json_path=None):
    root = _resolve_data_root(str(data_root) if data_root else None)
    root_str = str(root) if root else None
    _apply_data_root(root_str)  # 主进程也需(串行 & 采样)

    from tools.backtest import backtest_predict as bp

    codes = bp._sample_universe(n_sample, seed)
    if jobs is None:
        jobs = max(1, (os.cpu_count() or 2) - 2)
    print(f"[cfg] sample={n_sample} step={step} seed={seed} horizons={horizons} "
          f"codes={len(codes)} jobs={jobs} "
          f"master_root={'store默认' if root is None else root}", file=sys.stderr)
    if not codes:
        print("!! 采样为空:无主档数据(worktree 未接数据根)。用 --data-root 指向含 master/ 的目录。",
              file=sys.stderr)
        return 2

    print("[run] building adaptive panel ...", file=sys.stderr)
    pa = build_panel_parallel(codes, True, horizons, step, root_str, jobs)
    print(f"[run] adaptive rows={len(pa)} used={pa.attrs.get('used')}; building fixed panel ...",
          file=sys.stderr)
    pf = build_panel_parallel(codes, False, horizons, step, root_str, jobs)
    print(f"[run] fixed rows={len(pf)} used={pf.attrs.get('used')}", file=sys.stderr)

    if pa.empty or pf.empty or "N" not in pa.columns:
        print(f"!! panel 为空(adaptive={len(pa)} fixed={len(pf)});样本历史不足"
              f"(每票需 ≥warmup+maxN+5 根)。", file=sys.stderr)
        return 2

    result = {"cfg": {"sample": n_sample, "step": step, "seed": seed, "horizons": list(horizons),
                      "used": int(pa.attrs.get("used", 0)), "rows": int(len(pa))},
              "by_horizon": {}, "免责": "历史回测≠未来保证,非投资建议。"}

    print("\n===== 自适应RR vs 固定1.33 · 波动率括号(持有期建议) =====")
    print("(非投资建议;历史回测≠未来;同 sample/seed,只切自适应开关)\n")
    cols = ["n", "TP先%", "SL先%", "止损触发率%", "均目标%", "名义E%", "实测均值%"]
    for N in horizons:
        ta = bp._touch_stats(pa[pa["N"] == N], "br")
        tf = bp._touch_stats(pf[pf["N"] == N], "br")
        print(f"—— {N} 交易日 (n={ta.get('n')}) ——")
        print(f"  固定1.33 : {_fmt(tf, cols)}")
        print(f"  自适应RR : {_fmt(ta, cols)}")
        dr = ta.get("实测均值%", float("nan")) - tf.get("实测均值%", float("nan"))
        de = ta.get("名义E%", float("nan")) - tf.get("名义E%", float("nan"))
        dsl = ta.get("止损触发率%", float("nan")) - tf.get("止损触发率%", float("nan"))
        print(f"  Δ实测均值% = {dr:+.3f}   Δ名义E% = {de:+.3f}   "
              f"Δ止损触发率% = {dsl:+.3f}  (Δ止损触发率应≈0=只动目标)\n")
        result["by_horizon"][str(N)] = {"固定1.33": tf, "自适应RR": ta,
                                        "Δ实测均值%": dr, "Δ名义E%": de, "Δ止损触发率%": dsl}

    rrs = _rr_from_panel(pa, 5)
    if len(rrs):
        const = float(np.mean(np.abs(rrs - 1.33) < 0.02)) * 100
        rr_stat = {"n": int(len(rrs)), "min": float(rrs.min()),
                   "p25": float(np.percentile(rrs, 25)), "median": float(np.median(rrs)),
                   "p75": float(np.percentile(rrs, 75)), "max": float(rrs.max()),
                   "std": float(rrs.std()), "恒定1.33占比%": const}
        result["RR分布_5日"] = rr_stat
        print(f"===== 自适应 RR 分布(5日,{len(rrs)} 观测·逐日 as-of)=====")
        print(f"  RR: min={rrs.min():.2f} p25={np.percentile(rrs, 25):.2f} "
              f"中位={np.median(rrs):.2f} p75={np.percentile(rrs, 75):.2f} "
              f"max={rrs.max():.2f} std={rrs.std():.2f}")
        print(f"  恒定1.33 占比 = {const:.1f}%(越低=越个股化;固定档=100%)")

    if json_path:
        Path(json_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已落盘:{json_path}", file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="自适应RR vs 固定1.33 A/B 回测验证")
    ap.add_argument("sample", nargs="?", type=int, default=400)
    ap.add_argument("step", nargs="?", type=int, default=5)
    ap.add_argument("seed", nargs="?", type=int, default=42)
    ap.add_argument("--sample", dest="sample_opt", type=int, default=None)
    ap.add_argument("--step", dest="step_opt", type=int, default=None)
    ap.add_argument("--seed", dest="seed_opt", type=int, default=None)
    ap.add_argument("--horizon", default="1,5,10")
    ap.add_argument("--jobs", type=int, default=None, help="并行进程数(默认 CPU-2;1=串行)")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--json", default=None, help="A/B 结果落盘路径")
    a = ap.parse_args(argv)
    n_sample = a.sample_opt if a.sample_opt is not None else a.sample
    step = a.step_opt if a.step_opt is not None else a.step
    seed = a.seed_opt if a.seed_opt is not None else a.seed
    horizons = tuple(int(x) for x in a.horizon.split(","))
    return run_ab(n_sample, step, seed, horizons,
                  data_root=Path(a.data_root) if a.data_root else None,
                  jobs=a.jobs, json_path=a.json)


if __name__ == "__main__":
    raise SystemExit(main())
