"""Weak-strategy triage walk-forward harness (S02/S03/S04).

No future function: signal_at only uses <=t; forward returns close[t+k]/close[t]-1 are labels only.
Baseline = equal-weight mean forward return across ALL sampled codes on the same test day
(proxy for market direction/level, same spirit as prior reports' "baseline全样本").
Writes JSON results to scratchpad. Read-only on data/master.
"""
from __future__ import annotations
import glob, json, os, sys, time, random
import numpy as np
import pandas as pd

random.seed(7)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tools.pipeline import screen_s02, screen_max_range, screen_volume

HORIZONS = (1, 5, 10, 20)
STEP = 12                # test-day stride
START_IDX = 255          # need >=250 history for S03
N_CODES = int(os.environ.get("N_CODES", "1200"))

def list_codes(n):
    fs = sorted(glob.glob("data/master/kline/*.parquet"))
    codes = [os.path.basename(f)[:6] for f in fs]
    # drop 北交所 (8*/4*) to match universe
    codes = [c for c in codes if not (c.startswith("8") or c.startswith("4"))]
    if n and n < len(codes):
        random.seed(7); codes = random.sample(codes, n)
    return sorted(codes)

def load(code):
    f = f"data/master/kline/{code}.parquet"
    if not os.path.exists(f): return None
    df = pd.read_parquet(f)
    if len(df) < START_IDX + max(HORIZONS) + 1: return None
    df = df.reset_index(drop=True)
    return df

def fwd(close, t, k):
    if t + k >= len(close): return None
    if close[t] <= 0: return None
    return close[t + k] / close[t] - 1.0

def mfe(close, high, t, k):
    """max favorable excursion pct over next k days (intraday high)."""
    if t + k >= len(close) or close[t] <= 0: return None
    seg = high[t+1:t+1+k]
    if len(seg) == 0: return None
    return float(seg.max()) / close[t] - 1.0

def build_market_regime(codes):
    """Equal-weight market index from mean daily pct_chg; regime = index vs its MA60 at t.
    Returns dict {pd.Timestamp(date) -> 'bull'|'bear'} using only data up to that date (no future)."""
    ssum = {}; scnt = {}
    for code in codes:
        f = f"data/master/kline/{code}.parquet"
        if not os.path.exists(f): continue
        df = pd.read_parquet(f, columns=["date", "pct_chg"])
        for d, p in zip(df["date"], df["pct_chg"].to_numpy(float)):
            if p != p: continue
            ssum[d] = ssum.get(d, 0.0) + p
            scnt[d] = scnt.get(d, 0) + 1
    dates = sorted(ssum.keys())
    idx = []; lvl = 100.0
    for d in dates:
        lvl *= (1.0 + (ssum[d]/scnt[d])/100.0)
        idx.append(lvl)
    idx = np.array(idx)
    reg = {}
    for i, d in enumerate(dates):
        if i < 60: reg[pd.Timestamp(d)] = "bull"; continue
        ma = idx[i-59:i+1].mean()
        reg[pd.Timestamp(d)] = "bull" if idx[i] >= ma else "bear"
    return reg

def summarize(rets):
    rets = [r for r in rets if r is not None]
    if not rets: return {"n": 0}
    a = np.array(rets)
    return {"n": len(a), "mean": round(float(a.mean())*100, 3),
            "win": round(float((a > 0).mean())*100, 2),
            "median": round(float(np.median(a))*100, 3)}

def excess(hit, base):
    out = {}
    for h in HORIZONS:
        hs, bs = summarize(hit[h]), summarize(base[h])
        out[f"T{h}"] = {"hit": hs, "base": bs,
                        "excess_mean": (round(hs.get("mean",0)-bs.get("mean",0),3) if hs["n"] and bs["n"] else None),
                        "excess_win": (round(hs.get("win",0)-bs.get("win",0),2) if hs["n"] and bs["n"] else None)}
    return out

def main():
    codes = list_codes(N_CODES)
    print(f"codes={len(codes)} step={STEP}", flush=True)
    print("building market regime...", flush=True)
    regime = build_market_regime(codes)
    # per test-day baseline forward returns, aggregated over all codes
    base = {h: [] for h in HORIZONS}
    base_bull = {h: [] for h in HORIZONS}
    # bull-regime-gated variants (salvage test for S03/S04)
    s03_bull = {h: [] for h in HORIZONS}
    s04_bull = {h: [] for h in HORIZONS}
    s02_up_bull = {h: [] for h in HORIZONS}
    # collectors
    s02_hit = {h: [] for h in HORIZONS}
    s02_mfe5 = []                       # MFE over 5d for S02 hits
    s02_up = {h: [] for h in HORIZONS}  # variant: S02 + close>MA50 (uptrend context)
    s03_hit = {h: [] for h in HORIZONS}
    s04_all = {h: [] for h in HORIZONS}
    s04_sub = {s: {h: [] for h in HORIZONS} for s in ("单日放量","低位放量","连续放量")}

    t0 = time.time(); nproc = 0
    for code in codes:
        df = load(code)
        if df is None: continue
        nproc += 1
        close = df["close"].to_numpy(float)
        high = df["high"].to_numpy(float)
        n = len(df)
        dates = df["date"].tolist()
        for t in range(START_IDX, n - 1, STEP):
            reg = regime.get(pd.Timestamp(dates[t]), "bull")
            is_bull = reg == "bull"
            # baseline: this code's forward return contributes to the day's market pool
            for h in HORIZONS:
                r = fwd(close, t, h)
                if r is not None:
                    base[h].append(r)
                    if is_bull: base_bull[h].append(r)
            # S02
            r02 = screen_s02.signal_at(df, t)
            if r02.get("SELECT"):
                for h in HORIZONS:
                    v = fwd(close, t, h)
                    if v is not None: s02_hit[h].append(v)
                m = mfe(close, high, t, 5)
                if m is not None: s02_mfe5.append(m)
                ma50 = screen_max_range.ind.ma(close, t, 50)
                if ma50 is not None and close[t] > ma50:
                    for h in HORIZONS:
                        v = fwd(close, t, h)
                        if v is not None:
                            s02_up[h].append(v)
                            if is_bull: s02_up_bull[h].append(v)
            # S03
            r03 = screen_max_range.signal_at(df, t, code=code)
            if r03.get("SELECT"):
                for h in HORIZONS:
                    v = fwd(close, t, h)
                    if v is not None:
                        s03_hit[h].append(v)
                        if is_bull: s03_bull[h].append(v)
            # S04
            r04 = screen_volume.signal_at(df, t)
            if r04.get("SELECT"):
                for h in HORIZONS:
                    v = fwd(close, t, h)
                    if v is not None:
                        s04_all[h].append(v)
                        if is_bull: s04_bull[h].append(v)
                for s in r04.get("组合", []):
                    for h in HORIZONS:
                        v = fwd(close, t, h)
                        if v is not None: s04_sub[s][h].append(v)
        if nproc % 200 == 0:
            print(f"  {nproc} codes, {time.time()-t0:.0f}s", flush=True)

    result = {
        "meta": {"codes_processed": nproc, "step": STEP, "start_idx": START_IDX,
                 "baseline_n_T1": len(base[1])},
        "S02": excess(s02_hit, base),
        "S02_mfe5_pct": summarize(s02_mfe5),
        "S02_uptrend_MA50": excess(s02_up, base),
        "S03": excess(s03_hit, base),
        "S04_all": excess(s04_all, base),
        "S04_单日放量": excess(s04_sub["单日放量"], base),
        "S04_低位放量": excess(s04_sub["低位放量"], base),
        "S04_连续放量": excess(s04_sub["连续放量"], base),
        "_bull_regime_gated (vs bull baseline)": {
            "S02_uptrend_MA50_bull": excess(s02_up_bull, base_bull),
            "S03_bull": excess(s03_bull, base_bull),
            "S04_all_bull": excess(s04_bull, base_bull),
        },
    }
    out = "scratchpad/triage_result.json"
    with open(out, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("WROTE", out, f"{time.time()-t0:.0f}s", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
