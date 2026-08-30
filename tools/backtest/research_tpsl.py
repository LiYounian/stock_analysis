"""Research-only prototype: can per-stock take-profit / stop-loss lines be predicted?

NOT a production module. Read-only over data/master/kline. No future function:
all predictor features use data with index <= T (the entry bar); labels (MFE/MAE)
use the forward window T+1 .. T+H strictly after the entry bar.

Pipeline:
  load_kline -> compute_features (<=T) -> compute_forward (T+1..T+H) ->
  build_panel -> analyses (correlation / reasonableness / dynamic-vs-fixed exit).

Usage:
  python -m tools.backtest.research_tpsl build   --n-stocks 1200 --step 5 --horizon 20
  python -m tools.backtest.research_tpsl analyze  # uses cached panel parquet
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KLINE_DIR = os.path.join(REPO, "data", "master", "kline")
OUT_DIR = os.path.join(REPO, "data", "analysis", "research_tpsl")
os.makedirs(OUT_DIR, exist_ok=True)

# ----------------------------------------------------------------------------
# 1. IO
# ----------------------------------------------------------------------------

def list_codes(n: int | None = None, seed: int = 7) -> list[str]:
    files = sorted(glob.glob(os.path.join(KLINE_DIR, "*.parquet")))
    codes = [os.path.basename(f)[:-8] for f in files]
    if n and n < len(codes):
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(codes), size=n, replace=False)
        codes = [codes[i] for i in sorted(idx)]
    return codes


def load_kline(code: str) -> pd.DataFrame | None:
    fp = os.path.join(KLINE_DIR, f"{code}.parquet")
    if not os.path.exists(fp):
        return None
    df = pd.read_parquet(fp, columns=["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


# ----------------------------------------------------------------------------
# 2. Predictor features (only data <= T)
# ----------------------------------------------------------------------------

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    pc = c.shift(1)

    # True range / ATR(14)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    df["atr_pct"] = atr14 / c  # ATR as fraction of price

    # Historical vol: std of daily returns (20d), annualise-free (per-day)
    ret1 = c.pct_change()
    df["vol20"] = ret1.rolling(20).std()

    # Bollinger(20,2): width & percent_b
    ma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    upper = ma20 + 2 * sd20
    lower = ma20 - 2 * sd20
    df["boll_width"] = (upper - lower) / ma20
    df["percent_b"] = (c - lower) / (upper - lower)

    # Bias (乖离率) vs ma20
    df["bias20"] = c / ma20 - 1.0

    # Box (support/resistance) over 20 & 60 days, using data up to and incl T
    for n in (20, 60):
        box_hi = h.rolling(n).max()
        box_lo = l.rolling(n).min()
        df[f"box_hi_dist{n}"] = box_hi / c - 1.0   # room up to resistance (>=0)
        df[f"box_lo_dist{n}"] = c / box_lo - 1.0    # room down to support (>=0)
        df[f"box_height{n}"] = (box_hi - box_lo) / c

    # Trend: 20d momentum & log-price OLS slope (normalised per-day %)
    df["ret20"] = c / c.shift(20) - 1.0
    logc = np.log(c)
    x = np.arange(20)
    xm = x.mean()
    denom = ((x - xm) ** 2).sum()

    def _slope(a):
        if np.isnan(a).any():
            return np.nan
        return ((x - xm) * (a - a.mean())).sum() / denom

    df["trend_slope"] = logc.rolling(20).apply(_slope, raw=True)

    # liquidity (for filtering), amount may be NaN in recent rows -> use turnover
    df["turnover20"] = df["turnover"].rolling(20).mean()
    return df


FEATURES = [
    "atr_pct", "vol20", "boll_width", "percent_b", "bias20",
    "box_hi_dist20", "box_lo_dist20", "box_height20",
    "box_hi_dist60", "box_lo_dist60", "box_height60",
    "ret20", "trend_slope",
]


# ----------------------------------------------------------------------------
# 3. Forward labels (T+1 .. T+H) -- future window, used only as label
# ----------------------------------------------------------------------------

def compute_forward(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    c = df["close"].astype(float).values
    h = df["high"].astype(float).values
    l = df["low"].astype(float).values
    n = len(df)
    mfe = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    # exit-outcome helpers computed later per-scheme; here just the raw excursions
    for t in range(n):
        j0, j1 = t + 1, min(t + horizon, n - 1)
        if j0 > j1:
            continue
        base = c[t]
        mfe[t] = h[j0:j1 + 1].max() / base - 1.0
        mae[t] = l[j0:j1 + 1].min() / base - 1.0
    df[f"mfe{horizon}"] = mfe
    df[f"mae{horizon}"] = mae
    return df


# ----------------------------------------------------------------------------
# 4. Panel builder
# ----------------------------------------------------------------------------

def build_panel(n_stocks: int, step: int, horizon: int, min_turnover: float = 0.5,
                min_price: float = 2.0, seed: int = 7) -> pd.DataFrame:
    codes = list_codes(n_stocks, seed=seed)
    rows = []
    for i, code in enumerate(codes):
        df = load_kline(code)
        if df is None or len(df) < 120 + horizon:
            continue
        df = compute_features(df)
        df = compute_forward(df, horizon)
        # valid entry rows: features present, forward present, warmup >= 65, sampled by step
        df["code"] = code
        df["ridx"] = np.arange(len(df))
        mask = (
            df["ridx"] >= 65
        ) & (df["ridx"] % step == (hash(code) % step)) & \
            df[FEATURES].notna().all(axis=1) & \
            df[f"mfe{horizon}"].notna() & df[f"mae{horizon}"].notna() & \
            (df["close"] >= min_price) & (df["turnover20"].fillna(0) >= min_turnover)
        sub = df.loc[mask, ["code", "date"] + FEATURES + [f"mfe{horizon}", f"mae{horizon}", "close"]].copy()
        rows.append(sub)
        if (i + 1) % 500 == 0:
            print(f"  ...{i+1}/{len(codes)} stocks, panel rows so far ~{sum(len(r) for r in rows)}", file=sys.stderr)
    panel = pd.concat(rows, ignore_index=True)
    return panel


# ----------------------------------------------------------------------------
# 5. Analyses
# ----------------------------------------------------------------------------

def _spearman(x: pd.Series, y: pd.Series) -> float:
    """Spearman rho without scipy: Pearson on ranks, pairwise-complete."""
    d = pd.concat([x, y], axis=1).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 10:
        return float("nan")
    return float(d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank()))


def analyze_correlation(panel: pd.DataFrame, horizon: int) -> dict:
    mfe = panel[f"mfe{horizon}"]
    mae = panel[f"mae{horizon}"]
    absmae = mae.abs()
    span = mfe - mae  # total range (favorable + adverse magnitude)
    asym = mfe + mae  # positive => favorable dominates
    out = {}
    for f in FEATURES:
        x = panel[f]
        out[f] = {
            "spearman_mfe": _spearman(x, mfe),
            "spearman_absmae": _spearman(x, absmae),
            "spearman_span": _spearman(x, span),
            "spearman_asym": _spearman(x, asym),
        }
    return out


def analyze_reasonableness(panel: pd.DataFrame, horizon: int) -> dict:
    """Is a volatility-scaled predicted line stable / convergent / accurate?

    Predicted target ~ c_up * atr_pct, predicted stop ~ c_dn * atr_pct.
    Calibrate c_up, c_dn as the median ratio of actual excursion to atr_pct,
    then measure spread of the ratio (convergence) and coverage.
    """
    atr = panel["atr_pct"].replace(0, np.nan)
    mfe = panel[f"mfe{horizon}"]
    absmae = panel[f"mae{horizon}"].abs()
    r_up = mfe / atr
    r_dn = absmae / atr
    res = {}
    for name, r in (("mfe_over_atr", r_up), ("absmae_over_atr", r_dn)):
        r = r.replace([np.inf, -np.inf], np.nan).dropna()
        res[name] = {
            "median": float(r.median()),
            "iqr": float(r.quantile(0.75) - r.quantile(0.25)),
            "cv": float(r.std() / r.mean()) if r.mean() else None,  # dispersion of ratio
            "q10": float(r.quantile(0.10)),
            "q90": float(r.quantile(0.90)),
        }
    # Coverage: with stop = k*atr, fraction of trades whose MAE is worse than stop
    #   i.e. how often a k-ATR stop would be hit. Report for k in a grid.
    cov = {}
    for k in (1.0, 1.5, 2.0, 2.5, 3.0):
        stop_hit = (panel[f"mae{horizon}"] <= -k * panel["atr_pct"]).mean()
        tgt_hit = (mfe >= k * panel["atr_pct"]).mean()
        cov[f"k={k}"] = {"stop_hit_rate": float(stop_hit), "target_hit_rate": float(tgt_hit)}
    res["coverage"] = cov
    return res


def analyze_stability(n_stocks: int, horizon: int, seed: int = 7) -> dict:
    """Does a stock's atr_pct rank persist over time? (predictability precondition)

    Autocorrelation of atr_pct at lag = horizon, pooled; plus rank persistence
    between first-half and second-half medians across stocks.
    """
    codes = list_codes(n_stocks, seed=seed)
    ac = []
    firsthalf, secondhalf, codes_kept = [], [], []
    for code in codes:
        df = load_kline(code)
        if df is None or len(df) < 300:
            continue
        df = compute_features(df)
        a = df["atr_pct"].dropna()
        if len(a) < 200:
            continue
        lag = horizon
        c0 = a.iloc[:-lag].reset_index(drop=True)
        c1 = a.iloc[lag:].reset_index(drop=True)
        if len(c0) > 30:
            ac.append(float(c0.corr(c1)))
        mid = len(a) // 2
        firsthalf.append(float(a.iloc[:mid].median()))
        secondhalf.append(float(a.iloc[mid:].median()))
        codes_kept.append(code)
    fh = pd.Series(firsthalf)
    sh = pd.Series(secondhalf)
    return {
        "atr_pct_autocorr_lag_h": {"mean": float(np.nanmean(ac)), "median": float(np.nanmedian(ac)), "n": len(ac)},
        "cross_stock_rank_persistence_spearman": _spearman(fh, sh),
        "n_stocks": len(codes_kept),
    }


# ----------------------------------------------------------------------------
# 6. Dynamic vs fixed exit backtest
# ----------------------------------------------------------------------------

@dataclass
class ExitParams:
    mode: str            # "fixed" or "atr"
    tp: float            # take-profit: pct (fixed) or ATR multiple (atr)
    sl: float            # stop-loss magnitude: pct (fixed) or ATR multiple (atr)
    horizon: int
    cost: float = 0.0015  # round-trip cost (buy+sell), fraction


def _simulate_trade(h, l, c, t, base, atr_pct, p: ExitParams):
    """Path-dependent exit. Enter at close[t]=base. Priority within a bar: if both
    tp and sl touched same bar, assume worst (stop) to avoid optimism. Returns net ret."""
    if p.mode == "fixed":
        tp_px = base * (1 + p.tp)
        sl_px = base * (1 - p.sl)
    else:  # atr
        tp_px = base * (1 + p.tp * atr_pct)
        sl_px = base * (1 - p.sl * atr_pct)
    n = len(c)
    j1 = min(t + p.horizon, n - 1)
    for j in range(t + 1, j1 + 1):
        hit_sl = l[j] <= sl_px
        hit_tp = h[j] >= tp_px
        if hit_sl and hit_tp:
            exit_px = sl_px  # conservative
            return exit_px / base - 1.0 - p.cost
        if hit_sl:
            return sl_px / base - 1.0 - p.cost
        if hit_tp:
            return tp_px / base - 1.0 - p.cost
    # time exit at close[j1]
    return c[j1] / base - 1.0 - p.cost


def run_exit_backtest(panel_codes_dates: pd.DataFrame, params_list: list[ExitParams],
                      horizon: int) -> pd.DataFrame:
    """panel_codes_dates: rows with code,date to enter. Re-load kline per code and
    simulate each params scheme. Returns per-trade returns table (long)."""
    recs = []
    by_code = panel_codes_dates.groupby("code")
    for code, g in by_code:
        df = load_kline(code)
        if df is None:
            continue
        df = compute_features(df)
        date_to_idx = {d: i for i, d in enumerate(df["date"].values)}
        h = df["high"].astype(float).values
        l = df["low"].astype(float).values
        c = df["close"].astype(float).values
        atrv = df["atr_pct"].values
        for d in g["date"].values:
            t = date_to_idx.get(d)
            if t is None or t + 1 >= len(df) or np.isnan(atrv[t]):
                continue
            base = c[t]
            for p in params_list:
                r = _simulate_trade(h, l, c, t, base, atrv[t], p)
                # stop distance actually used (for R-multiple)
                sl_dist = p.sl if p.mode == "fixed" else p.sl * atrv[t]
                recs.append({"code": code, "date": d, "scheme": f"{p.mode}_tp{p.tp}_sl{p.sl}",
                             "ret": r, "R": r / sl_dist if sl_dist else np.nan,
                             "atr_pct": atrv[t]})
    return pd.DataFrame(recs)


def summarize_backtest(bt: pd.DataFrame) -> dict:
    out = {}
    # vol terciles (shared across schemes for comparability)
    q1, q2 = bt["atr_pct"].quantile([1/3, 2/3])
    for scheme, g in bt.groupby("scheme"):
        r = g["ret"].values
        R = g["R"].replace([np.inf, -np.inf], np.nan).dropna()
        # per-day clustered mean & t-stat
        daily = g.groupby("date")["ret"].mean()
        n_days = len(daily)
        t_stat = float(daily.mean() / (daily.std(ddof=1) / np.sqrt(n_days))) if n_days > 1 else None
        eq = (1 + daily.sort_index()).cumprod()
        dd = (eq / eq.cummax() - 1).min()
        # loss consistency: std of returns among losing trades, in R units
        losers = R[R < 0]
        # premature stop rate by vol tercile (a stop is "hit" if ret is very close to -sl)
        lo = g[g["atr_pct"] <= q1]["ret"]
        hi = g[g["atr_pct"] > q2]["ret"]
        out[scheme] = {
            "n_trades": int(len(r)),
            "mean_ret": float(np.mean(r)),
            "median_ret": float(np.median(r)),
            "win_rate": float((r > 0).mean()),
            "std": float(np.std(r)),
            "sharpe_per_trade": float(np.mean(r) / np.std(r)) if np.std(r) else None,
            "daily_t_stat": t_stat,
            "n_days": int(n_days),
            "max_drawdown_daily_eq": float(dd),
            "R_mean": float(R.mean()) if len(R) else None,
            "R_std": float(R.std()) if len(R) else None,
            "loser_R_std": float(losers.std()) if len(losers) > 1 else None,
            "meanret_lowvol_tercile": float(lo.mean()) if len(lo) else None,
            "meanret_highvol_tercile": float(hi.mean()) if len(hi) else None,
            "winrate_lowvol_tercile": float((lo > 0).mean()) if len(lo) else None,
            "winrate_highvol_tercile": float((hi > 0).mean()) if len(hi) else None,
        }
    return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def cmd_build(args):
    print(f"[build] n_stocks={args.n_stocks} step={args.step} horizon={args.horizon}", file=sys.stderr)
    panel = build_panel(args.n_stocks, args.step, args.horizon, seed=args.seed)
    fp = os.path.join(OUT_DIR, f"panel_h{args.horizon}.parquet")
    panel.to_parquet(fp)
    print(f"[build] panel rows={len(panel)} saved={fp}", file=sys.stderr)


def cmd_analyze(args):
    fp = os.path.join(OUT_DIR, f"panel_h{args.horizon}.parquet")
    panel = pd.read_parquet(fp)
    print(f"[analyze] panel rows={len(panel)}", file=sys.stderr)
    result = {
        "meta": {"n_rows": int(len(panel)), "n_codes": int(panel["code"].nunique()),
                 "horizon": args.horizon},
        "correlation": analyze_correlation(panel, args.horizon),
        "reasonableness": analyze_reasonableness(panel, args.horizon),
        "stability": analyze_stability(args.stab_stocks, args.horizon, seed=args.seed),
    }
    out = os.path.join(OUT_DIR, f"analysis_h{args.horizon}.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[analyze] saved={out}", file=sys.stderr)


def cmd_exit(args):
    fp = os.path.join(OUT_DIR, f"panel_h{args.horizon}.parquet")
    panel = pd.read_parquet(fp)
    cd = panel[["code", "date"]].drop_duplicates()
    # Calibrate fixed lines to match the AVERAGE atr-based distance so the only
    # difference is per-stock adaptation, not overall tightness.
    med_atr = float(panel["atr_pct"].median())
    tp_mult, sl_mult = args.tp, args.sl
    fixed_tp = tp_mult * med_atr
    fixed_sl = sl_mult * med_atr
    params = [
        ExitParams("atr", tp_mult, sl_mult, args.horizon),
        ExitParams("fixed", round(fixed_tp, 4), round(fixed_sl, 4), args.horizon),
    ]
    print(f"[exit] median_atr_pct={med_atr:.4f} -> fixed_tp={fixed_tp:.4f} fixed_sl={fixed_sl:.4f}", file=sys.stderr)
    bt = run_exit_backtest(cd, params, args.horizon)
    summ = summarize_backtest(bt)
    summ["_calib"] = {"median_atr_pct": med_atr, "tp_mult": tp_mult, "sl_mult": sl_mult,
                      "fixed_tp": fixed_tp, "fixed_sl": fixed_sl}
    out = os.path.join(OUT_DIR, f"exit_h{args.horizon}.json")
    with open(out, "w") as f:
        json.dump(summ, f, indent=2, ensure_ascii=False)
    print(json.dumps(summ, indent=2, ensure_ascii=False))
    print(f"[exit] saved={out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--n-stocks", type=int, default=1200)
    b.add_argument("--step", type=int, default=5); b.add_argument("--horizon", type=int, default=20)
    b.add_argument("--seed", type=int, default=7); b.set_defaults(func=cmd_build)
    a = sub.add_parser("analyze"); a.add_argument("--horizon", type=int, default=20)
    a.add_argument("--stab-stocks", type=int, default=400); a.add_argument("--seed", type=int, default=7)
    a.set_defaults(func=cmd_analyze)
    e = sub.add_parser("exit"); e.add_argument("--horizon", type=int, default=20)
    e.add_argument("--tp", type=float, default=3.0); e.add_argument("--sl", type=float, default=2.0)
    e.set_defaults(func=cmd_exit)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
