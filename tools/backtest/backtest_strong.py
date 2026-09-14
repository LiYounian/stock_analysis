"""策略 S05「最强选股」walk-forward 诊断回测(前向 α + winner_rate 分桶 + C1–C4 分维贡献)。

目的(诊断优先,先证实"高获利=派发负α"假设,再决定是否加派发闸):
  1. 前向 α:S05 历史入选票在 t+1/t+5 的 α = 个股收益 − **全A等权**同期收益(项目口径)。
  2. winner_rate 分桶:把 C1∧C2∧C3 通过的候选按筹码获利比例(winner_rate)分桶,对比各桶 α,
     检验"winner_rate 越高(潜在派发抛压越大)→ 前向 α 越低"是否成立。
  3. C1–C4 分维贡献:C1 六均线多头 / C2 近期连涨 / C3 高位区间 / C4 筹码高度获利,
     逐层叠加看每条在加 α 还是减 α(别因整体没用就一句否掉,也别因整体有用掩盖某条拖累)。

口径与防未来函数(硬红线):
  · 入场锚定信号日收盘价 close[t];前向收益 r_N = close[t+N]/close[t] − 1(t+N 越界 → 未到期,剔除)。
  · α = r_N − baseline_N(t);baseline_N(t) = 当日**全A所有已到期票**的 r_N 等权均值(横截面市场均收益)。
  · C1/C2/C3 只用 ≤t 的 K线;C4 走 chip.summarize_asof(≤t 的 bar 推演),无前视。
  · 前向收益 close[t+N] 仅作**结果标签**,绝不回喂入选判定。
  · 测试日必须留足前瞻余量(t+max_horizon ≤ 最后交易日),否则该 horizon 剔除。

与生产的等价性:C1/C2/C3 向量化实现,由 test_backtest_strong 锁死与 screen_strong.signal_at 逐点一致;
C4 直接复用 chip.summarize_asof + 同一阈值判据(winner_rate>获利比阈值 或 high≥成本区间上沿)。

产物只写 worktree 本地 / 传入的 --out-dir;绝不污染主检出。⚠️ 测试环境研究模拟,非投资建议。

用法:python -m tools.backtest.backtest_strong --data-root <主仓> [--out-dir DIR]
      [--start 2019-01-01] [--end 2026-09-07] [--stride 1] [--horizons 1,5]
"""
from __future__ import annotations

import argparse
import logging
import os

import numpy as np
import pandas as pd

from tools.collectors import chip
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("backtest.strong")

_CFG = THRESHOLDS["最强选股"]
_BJ_PREFIX = ("8", "4")


# ────────────────────────────── 票池与特征 ──────────────────────────────
def universe_codes(data_root: str, exclude_bj: bool = True) -> list[str]:
    """主档 kline 目录下全部代码(可排北交所 8/4 头)。"""
    kdir = os.path.join(data_root, "data", "master", "kline")
    codes = [fn[:-8] for fn in os.listdir(kdir) if fn.endswith(".parquet")]
    if exclude_bj:
        codes = [c for c in codes if c[:1] not in _BJ_PREFIX]
    return sorted(codes)


def load_kline(data_root: str, code: str) -> pd.DataFrame | None:
    """读单票主档 K线(只读主仓,升序)。缺失/异常 → None。"""
    path = os.path.join(data_root, "data", "master", "kline", f"{code}.parquet")
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if df is None or "date" not in df.columns or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def precompute_features(df: pd.DataFrame, periods, horizons) -> dict:
    """把单票 K线预算成回测所需数组:MA(向量化,与 ind.ma 等价)、H52、日涨幅、前向收益。

    等价性说明(与 screen_strong.signal_at / ind 对齐):
      · MA(n)[i] = close[i-n+1:i+1] 均值,i-n+1<0 → NaN(不足 n 根不算),与 ind.ma 一致。
      · H52[i]  = high[i-249:i+1] 最大值,需满 250 根有效,与 ind.highest_high(win=250) 一致。
      · 日涨幅 up5[i] = close[i] ≥ close[i-1]·(1+涨幅阈值),i=0 → False(无前收盘)。
      · 前向 r_N[i] = close[i+N]/close[i] − 1,i+N 越界 → NaN(未到期,结果标签,无前视)。
    """
    close = df["close"].to_numpy(float)
    high = df["high"].to_numpy(float)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    n = len(close)
    s_close = pd.Series(close)

    mas = {p: s_close.rolling(p, min_periods=p).mean().to_numpy() for p in periods}
    h52 = pd.Series(high).rolling(int(_CFG["H52窗口"]), min_periods=int(_CFG["H52窗口"])).max().to_numpy()

    thr = 1.0 + float(_CFG["涨幅阈值"])
    prev = np.empty(n); prev[0] = np.nan; prev[1:] = close[:-1]
    with np.errstate(invalid="ignore"):
        up = (prev > 0) & (close >= prev * thr)
    up[0] = False

    fwd = {}
    for N in horizons:
        r = np.full(n, np.nan)
        if n > N:
            base = close[:-N]
            with np.errstate(invalid="ignore", divide="ignore"):
                rr = close[N:] / base - 1.0
            rr[base <= 0] = np.nan
            r[:-N] = rr
        fwd[N] = r

    return {"dates": dates, "close": close, "high": high, "ma": mas,
            "h52": h52, "up": up, "fwd": fwd, "n": n,
            "didx": {d: i for i, d in enumerate(dates)}}


# ────────────────────────────── 条件判定(向量化)──────────────────────────────
def cond123(feat: dict, periods) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """向量化 C1/C2/C3 + 近期大涨次数。返回 (c1, c2, c3, big_count)。逐点与 signal_at 等价。"""
    n = feat["n"]
    close, high, h52 = feat["close"], feat["high"], feat["h52"]
    ma = feat["ma"]

    # C1 六均线多头:每条 MA 有效且严格递减
    c1 = np.ones(n, dtype=bool)
    prev_ma = None
    for p in periods:
        m = ma[p]
        c1 &= ~np.isnan(m)
        if prev_ma is not None:
            c1 &= prev_ma > m
        prev_ma = m

    # C2 近期连涨:近 涨幅窗口 日内 单日涨≥阈值 的天数 ≥ 涨幅次数
    win = int(_CFG["涨幅窗口"])
    up = feat["up"].astype(int)
    big = pd.Series(up).rolling(win, min_periods=1).sum().to_numpy()
    c2 = big >= int(_CFG["涨幅次数"])

    # C3 高位区间:0.9·H52 < close < 1.2·H52(H52 满 250 根才有效)
    lo, hi = float(_CFG["贴近高下界"]), float(_CFG["贴近高上界"])
    with np.errstate(invalid="ignore"):
        c3 = (~np.isnan(h52)) & (h52 > 0) & (close > h52 * lo) & (close < h52 * hi)

    return c1, c2, c3, big.astype(int)


def chip_at(df_full: pd.DataFrame, as_of: str) -> tuple[float | None, float | None]:
    """C4 取数:chip.summarize_asof(≤as_of) → (winner_rate 百分数 0~100, cost_95pct 元)。

    与 screen_strong._local_chip_of 同口径:获利比例×100→winner_rate、成本区间上沿→cost_95pct;
    换手缺失/数据不足(获利比例 None)→ (None, None),交上层令 C4=False、不选。
    """
    rec = chip.summarize_asof(df_full, as_of)
    wr = rec.get("获利比例")
    if wr is None:
        return None, None
    cost95 = rec.get("成本区间上沿")
    return float(wr) * 100.0, (float(cost95) if cost95 is not None else None)


def c4_of(wr: float | None, cost95: float | None, high_t: float) -> bool:
    """C4 判据(与 signal_at 一致):winner_rate>获利比阈值 或 high[t]≥cost_95pct。"""
    thr = float(_CFG["获利比阈值"])
    return bool((wr is not None and wr > thr) or (cost95 is not None and high_t >= cost95))


# ────────────────────────────── 回测主循环 ──────────────────────────────
def run_backtest(data_root: str, start: str | None, end: str | None,
                 stride: int, horizons, exclude_bj: bool = True,
                 limit: int | None = None) -> pd.DataFrame:
    """回放 S05:对每票每个合格测试日评 C1/C2/C3,C1∧C2∧C3 通过者再算 C4 与前向 α。

    只对 C1∧C2∧C3 通过者调 chip(省算);记录全部通过者(含 C4=False),供分维/分桶诊断。
    baseline_N(t) 用全A所有已到期票 r_N 等权均值(先全量扫一遍前向收益累加)。
    """
    logging.getLogger("collectors.chip").setLevel(logging.ERROR)  # 静默逐票换手降级告警(回测批量噪声)
    codes = universe_codes(data_root, exclude_bj)
    if limit:
        codes = codes[:limit]
    periods = [int(p) for p in _CFG["均线多头周期"]]
    need = int(_CFG["最少历史根数"])
    maxh = max(horizons)

    logger.info("加载 %d 票 K线 + 预算特征 + 累加全A等权 baseline ...", len(codes))
    feats: dict[str, dict] = {}
    raw: dict[str, pd.DataFrame] = {}
    # baseline 累加器:{N: {date: [sum, cnt]}}
    base_acc: dict[int, dict[str, list]] = {N: {} for N in horizons}
    for i, code in enumerate(codes):
        df = load_kline(data_root, code)
        if df is None or len(df) < need:
            continue
        feat = precompute_features(df, periods, horizons)
        feats[code] = feat
        raw[code] = df
        for N in horizons:
            r = feat["fwd"][N]
            dts = feat["dates"]
            acc = base_acc[N]
            valid = ~np.isnan(r)
            for j in np.nonzero(valid)[0]:
                cell = acc.get(dts[j])
                if cell is None:
                    acc[dts[j]] = [float(r[j]), 1]
                else:
                    cell[0] += float(r[j]); cell[1] += 1
        if (i + 1) % 1000 == 0:
            logger.info("  ...%d/%d", i + 1, len(codes))

    baseline = {N: {d: (s / c if c else np.nan) for d, (s, c) in acc.items()}
                for N, acc in base_acc.items()}

    # 测试日窗口:用票池并集交易日,截掉尾部 maxh(留前瞻余量)
    all_days = sorted({d for f in feats.values() for d in f["dates"]})
    if end:
        all_days = [d for d in all_days if d <= end]
    usable = all_days[:-maxh] if len(all_days) > maxh else []
    if start:
        usable = [d for d in usable if d >= start]
    test_days = set(usable[::stride])
    logger.info("测试日 %d 个(stride=%d,范围 %s→%s),开始逐票扫描 ...",
                len(test_days), stride, min(test_days) if test_days else "-",
                max(test_days) if test_days else "-")

    rows = []
    n_chip = 0
    for code, feat in feats.items():
        c1, c2, c3, big = cond123(feat, periods)
        core = c1 & c2 & c3
        dts = feat["dates"]
        idxs = np.nonzero(core)[0]
        for t in idxs:
            d = dts[t]
            if d not in test_days or t < need - 1:
                continue
            if t + maxh >= feat["n"]:
                continue  # 无足够前瞻(防未来:未到期剔除)
            wr, cost95 = chip_at(raw[code], d)
            n_chip += 1
            c4 = c4_of(wr, cost95, float(feat["high"][t]))
            row = {"date": d, "code": code,
                   "C1": True, "C2": True, "C3": True, "C4": c4,
                   "select": c4, "winner_rate": wr, "cost95": cost95,
                   "close": float(feat["close"][t]), "high": float(feat["high"][t]),
                   "big": int(big[t])}
            for N in horizons:
                r = feat["fwd"][N][t]
                b = baseline[N].get(d, np.nan)
                row[f"r_{N}"] = float(r) if not np.isnan(r) else np.nan
                row[f"base_{N}"] = float(b) if not np.isnan(b) else np.nan
                row[f"alpha_{N}"] = (float(r - b) if not (np.isnan(r) or np.isnan(b)) else np.nan)
            rows.append(row)

    logger.info("扫描完成:C1∧C2∧C3 通过样本(=chip 调用)%d,入选(S05)%d",
                n_chip, sum(1 for r in rows if r["select"]))
    cols = (["date", "code", "C1", "C2", "C3", "C4", "select", "winner_rate",
             "cost95", "close", "high", "big"]
            + [f"{p}_{N}" for N in horizons for p in ("r", "base", "alpha")])
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["date", "code"]).reset_index(drop=True) if not df.empty else df


# ────────────────────────────── 分维/分桶诊断 ──────────────────────────────
def _stats(a: np.ndarray) -> dict:
    """一组 α 的均值/标准误/t 值/胜率/样本(t = mean / (std/√n),单样本对 0)。"""
    a = a[~np.isnan(a)]
    n = len(a)
    if n == 0:
        return {"n": 0, "mean": None, "t": None, "win": None}
    mean = float(a.mean())
    sd = float(a.std(ddof=1)) if n > 1 else float("nan")
    t = (mean / (sd / np.sqrt(n))) if (n > 1 and sd > 0) else None
    return {"n": n, "mean": round(mean * 100, 3), "t": (round(t, 2) if t is not None else None),
            "win": round(float((a > 0).mean()) * 100, 1)}


def diagnose(df: pd.DataFrame, horizons) -> dict:
    """Phase 1 三块:①S05 前向 α;②winner_rate 分桶 α;③C1–C4 分维叠加 α。"""
    out = {"总样本(C1∧C2∧C3)": int(len(df)), "入选(S05)": int(df["select"].sum())}

    # ① S05 入选票前向 α
    sel = df[df["select"]]
    out["①_S05前向α"] = {f"{N}日": _stats(sel[f"alpha_{N}"].to_numpy(float)) for N in horizons}
    out["①_S05原始收益均值%"] = {
        f"{N}日": (round(float(sel[f"r_{N}"].mean()) * 100, 3) if len(sel) else None)
        for N in horizons}

    # ② winner_rate 分桶(在 C1∧C2∧C3 全集内,含 C4=False 的低获利票)
    wr = df["winner_rate"].to_numpy(float)
    high = df["high"].to_numpy(float)
    cost95 = df["cost95"].to_numpy(float)
    buckets = {
        "wr>99": df[wr > 99],
        "95<wr≤99": df[(wr > 95) & (wr <= 99)],
        "80<wr≤95": df[(wr > 80) & (wr <= 95)],
        "wr≤80": df[(wr <= 80)],
        "wr缺失(chip不可用)": df[np.isnan(wr)],
    }
    out["②_winner_rate分桶α"] = {
        name: {"n": int(len(g)),
               **{f"{N}日α": _stats(g[f"alpha_{N}"].to_numpy(float)) for N in horizons}}
        for name, g in buckets.items()}
    # 入选票里"仅靠 high≥cost95 命中(wr≤95)"vs"wr>95" 两支
    sel_hi_only = sel[(sel["winner_rate"].isna()) | (sel["winner_rate"] <= 95)]
    sel_wr_hi = sel[sel["winner_rate"] > 95]
    out["②b_入选票两支"] = {
        "wr>95(高获利支)": {"n": int(len(sel_wr_hi)),
                            **{f"{N}日α": _stats(sel_wr_hi[f"alpha_{N}"].to_numpy(float)) for N in horizons}},
        "仅high≥cost95支(wr≤95/缺失)": {"n": int(len(sel_hi_only)),
                            **{f"{N}日α": _stats(sel_hi_only[f"alpha_{N}"].to_numpy(float)) for N in horizons}},
    }

    # ③ C1–C4 分维叠加(C1/C2/C3 全集已是 C1∧C2∧C3;C4 支 = select)
    #    注:本回测样本是 C1∧C2∧C3 通过集,故这里给"+C4 vs −C4"的对照(C4 的边际贡献)。
    c4t = df[df["C4"]]
    c4f = df[~df["C4"]]
    out["③_C4边际贡献"] = {
        "C1∧C2∧C3(全集)": {"n": int(len(df)),
                          **{f"{N}日α": _stats(df[f"alpha_{N}"].to_numpy(float)) for N in horizons}},
        "+C4(=S05入选)": {"n": int(len(c4t)),
                          **{f"{N}日α": _stats(c4t[f"alpha_{N}"].to_numpy(float)) for N in horizons}},
        "−C4(被C4剔除)": {"n": int(len(c4f)),
                          **{f"{N}日α": _stats(c4f[f"alpha_{N}"].to_numpy(float)) for N in horizons}},
    }
    return out


def run(data_root: str, out_dir: str, start=None, end=None, stride=1,
        horizons=(1, 5), limit=None) -> dict:
    df = run_backtest(data_root, start, end, stride, horizons, limit=limit)
    os.makedirs(out_dir, exist_ok=True)
    raw_csv = os.path.join(out_dir, "s05_backtest_rows.csv")
    df.to_csv(raw_csv, index=False, encoding="utf-8-sig")
    diag = diagnose(df, horizons) if not df.empty else {"总样本(C1∧C2∧C3)": 0}
    import json
    with open(os.path.join(out_dir, "s05_diagnose.json"), "w", encoding="utf-8") as f:
        json.dump(diag, f, ensure_ascii=False, indent=2)
    print("\n===== S05 最强选股 walk-forward 诊断 =====")
    print("(防未来函数;α=个股−全A等权;历史≠未来保证;非投资建议)\n")
    print(f"逐行样本 → {raw_csv}  ({len(df)} 行)")
    print(f"诊断摘要 → {os.path.join(out_dir, 's05_diagnose.json')}")
    if not df.empty:
        print(f"\nC1∧C2∧C3 样本 {diag['总样本(C1∧C2∧C3)']} | S05 入选 {diag['入选(S05)']}")
        for N in horizons:
            s = diag["①_S05前向α"][f"{N}日"]
            print(f"  S05 {N}日 α: n={s['n']} mean={s['mean']}% t={s['t']} 胜率={s['win']}%")
        print("  winner_rate 分桶 α:")
        for name, b in diag["②_winner_rate分桶α"].items():
            cells = " | ".join(f"{N}日 mean={b[f'{N}日α']['mean']}% t={b[f'{N}日α']['t']}" for N in horizons)
            print(f"    {name:20s} n={b['n']:5d}  {cells}")
    return diag


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="S05 最强选股 walk-forward 诊断回测")
    ap.add_argument("--data-root", required=True, help="主仓根(只读 data/master/kline)")
    ap.add_argument("--out-dir", default="/tmp/s05_backtest")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--horizons", default="1,5")
    ap.add_argument("--limit", type=int, default=None, help="只测前 N 票(冒烟)")
    a = ap.parse_args(argv)
    run(a.data_root, a.out_dir, start=a.start, end=a.end, stride=a.stride,
        horizons=tuple(int(x) for x in a.horizons.split(",")), limit=a.limit)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
