"""量化 double-count:数据面综合分里消息面专家的份额;并测「base 剔除消息面专家」变体。

用法(供复评):
    ~/.conda/envs/stock_analysis/bin/python tools/eval/msg_doublecount.py
    # 可选:STOCK_ANALYSIS_ROOT=/path/to/repo 覆盖仓库根。
背景:数据面综合分(默认8专家组)本已含 情绪三层/事件驱动/资金流;消息面分又单独合议这三位
     并以回灌权重 w 回灌 → 同批专家算两次。本脚本量化「消息面专家在 base 分母的占比」(den_share),
     消息面块有效系数 ≈ den_share(在base) + w(reflow),据此校准 w 与是否移出资金流。
"""
import json, os, glob, math
from pathlib import Path
import pandas as pd, numpy as np

ROOT = os.environ.get("STOCK_ANALYSIS_ROOT") or str(Path(__file__).resolve().parents[2])
ANA = os.path.join(ROOT, "data/analysis"); KL = os.path.join(ROOT, "data/master/kline")
MSG = {"情绪三层", "事件驱动", "资金流"}; TAU = 0.2
def clamp(x, lo, hi): return lo if x < lo else (hi if x > hi else x)
def mscore(S):
    d = 1.0 if S >= TAU else (-1.0 if S <= -TAU else 0.0)
    return round(clamp(d * min(abs(S), 1.0), -1.0, 1.0), 4)
_kl = {}
def load_kl(code):
    if code in _kl: return _kl[code]
    p = os.path.join(KL, f"{code}.parquet"); df = None
    if os.path.exists(p):
        df = pd.read_parquet(p)[["date", "close"]].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"); df = df.reset_index(drop=True)
    _kl[code] = df; return df
def fwd(code, d, ks=(1, 3, 5)):
    df = load_kl(code)
    if df is None: return {}
    idx = df.index[df["date"] == d]
    if len(idx) == 0: return {}
    i = idx[0]; c0 = df.at[i, "close"]; o = {}
    for k in ks:
        j = i + k
        if j < len(df) and c0 > 0: o[k] = df.at[j, "close"] / c0 - 1
    return o
rows = []
for d in sorted(os.listdir(ANA)):
    if not d.startswith("2026-"): continue
    for f in glob.glob(os.path.join(ANA, d, "??????.json")):
        code = os.path.basename(f)[:-5]
        try: rec = json.load(open(f))
        except Exception: continue
        cc = (rec.get("council") or {}).get("default") or {}
        if "综合分" not in cc: continue
        num_a = den_a = 0.0; num_m = den_m = 0.0; num_x = den_x = 0.0
        for a in cc.get("归因", []):
            w = float(a.get("权重", 0) or 0); c = float(a.get("置信度", 0) or 0); g = float(a.get("贡献", 0) or 0)
            num_a += g; den_a += w * c
            if a.get("专家") in MSG: num_m += g; den_m += w * c
            else: num_x += g; den_x += w * c
        S_all = (num_a / den_a) if den_a > 0 else 0.0        # = 数据面综合分
        S_msg = (num_m / den_m) if den_m > 0 else 0.0        # 消息面专家自身合议
        S_ex = (num_x / den_x) if den_x > 0 else 0.0         # 剔除消息面专家的数据面
        msg_contrib_share = (num_m / num_a) if abs(num_a) > 1e-9 else np.nan  # 贡献占比(带符号)
        den_share = (den_m / den_a) if den_a > 0 else np.nan                  # 分母权重占比
        fr = fwd(code, d)
        if not fr: continue
        rows.append({"date": d, "code": code, "S_all": S_all, "S_ex": S_ex, "S_msg": S_msg,
                     "msg": mscore(S_msg), "den_share": den_share,
                     **{f"fwd{k}": fr.get(k, np.nan) for k in (1, 3, 5)}})
df = pd.DataFrame(rows)
print("n=", len(df), "days=", df['date'].nunique())
print("\n=== double-count 量级 ===")
print("消息面专家在数据面分母(权重×置信度)的占比: mean=%.3f median=%.3f" % (df['den_share'].mean(), df['den_share'].median()))
print("corr(数据面综合分 S_all, 剔除msg后 S_ex) = %.3f" % df['S_all'].corr(df['S_ex']))
print("S_all std=%.3f, S_ex std=%.3f, S_msg std=%.3f" % (df['S_all'].std(), df['S_ex'].std(), df['S_msg'].std()))
print("=> 完整分=S_all + w*msg. 消息面专家有效影响系数 ≈ den_share(在base) + w(reflow)。")
for w in (0.3, 0.5, 0.8):
    print(f"   w={w}: 消息面块有效系数 ≈ {df['den_share'].mean():.2f}+{w} = {df['den_share'].mean()+w:.2f}  (单算应为 {df['den_share'].mean():.2f})")

def sp(a, b):
    m = (~a.isna()) & (~b.isna())
    return (a[m].rank().corr(b[m].rank()), int(m.sum())) if m.sum() >= 10 else (np.nan, int(m.sum()))
print("\n=== 各基数 daily RankIC(自身预测力对比)===")
for name, col in [("数据面S_all", 'S_all'), ("剔除msg S_ex", 'S_ex'), ("消息面S_msg", 'S_msg')]:
    line = []
    for k in (1, 3, 5):
        ics = [sp(g[col], g[f"fwd{k}"])[0] for _, g in df.groupby("date") if sp(g[col], g[f"fwd{k}"])[1] >= 20]
        ics = [x for x in ics if not math.isnan(x)]
        line.append(f"T+{k}={np.mean(ics):+.4f}(t{np.mean(ics)/(np.std(ics)/math.sqrt(len(ics))+1e-9):+.1f})")
    print(f"  {name:14s} " + " ".join(line))

print("\n=== C2) 两种 base 的 Top-10 前向收益对比 ===")
for base, bl in [("现状 base=S_all(含msg)", 'S_all'), ("变体 base=S_ex(剔msg)", 'S_ex')]:
    print(f"-- {base} --")
    for w in (0.0, 0.3, 0.5, 0.8):
        pdk = {1: [], 3: [], 5: []}
        for d, g in df.groupby("date"):
            g = g.assign(full=g[bl] + w * g["msg"]).sort_values("full", ascending=False).head(10)
            for k in (1, 3, 5):
                v = g[f"fwd{k}"].mean()
                if not math.isnan(v): pdk[k].append(v)
        print(f"   w={w}: " + " ".join(f"T+{k}={100*np.mean(pdk[k]):+.2f}%" for k in (1, 3, 5)))
