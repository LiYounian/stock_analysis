"""消息面回灌前向评测 v2 —— 只读历史落盘 council.default 块(as-of、防未来),不跑 convene。

数据面综合分 = record['council']['default']['综合分'](生产时按默认8专家组算,as-of 快照)。
消息面分 = 从同一 default 归因里抽 {情绪三层,事件驱动,资金流} 子集,按合议同口径
          S_msg=Σ贡献/Σ(权重×置信度),方向阈 τ=0.2,再 msg_score 映射(slope=1,cap=1)。
  —— 与 convene(MSG_EXPERTS) 逐值等价(单专家 verdict 与召集分组无关),且瞬时。
前向收益 fwd_k = close[d+k]/close[d]-1,master kline,严格 d 之后。防未来:sentiment 锁定≤d。

用法(供 40–60 日后攒够样本复评):
    ~/.conda/envs/stock_analysis/bin/python tools/eval/msg_forward_eval_v2.py
    # 可选:STOCK_ANALYSIS_ROOT=/path/to/repo 覆盖仓库根;EVAL_OUT_CSV=... 覆盖导出路径。
注意:此评测口径里 MSG 仍含「资金流」以复现历史 double-count 的量级;这是**度量口径**,
     不代表生产回灌口径(生产已把资金流移出消息面消费者专家,见 tools/config/strategy.py「消息面回灌」)。
"""
import json, os, glob, math
from pathlib import Path
import pandas as pd, numpy as np

ROOT = os.environ.get("STOCK_ANALYSIS_ROOT") or str(Path(__file__).resolve().parents[2])
ANA = os.path.join(ROOT, "data/analysis"); KL = os.path.join(ROOT, "data/master/kline")
OUT_CSV = os.environ.get("EVAL_OUT_CSV") or os.path.join(ROOT, "data/analysis/backtest/msg_eval_rows.csv")
MSG = {"情绪三层", "事件驱动", "资金流"}; TAU = 0.2; SLOPE = 1.0; CAP = 1.0

def clamp(x, lo, hi): return lo if x < lo else (hi if x > hi else x)
def msg_score(S):
    d = 1.0 if S >= TAU else (-1.0 if S <= -TAU else 0.0)
    return round(clamp(d * min(abs(S), 1.0) * SLOPE, -CAP, CAP), 4)

_kl = {}
def load_kl(code):
    if code in _kl: return _kl[code]
    p = os.path.join(KL, f"{code}.parquet"); df = None
    if os.path.exists(p):
        df = pd.read_parquet(p)[["date", "close"]].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d"); df = df.reset_index(drop=True)
    _kl[code] = df; return df
def fwd(code, d, ks=(1, 2, 3, 5)):
    df = load_kl(code)
    if df is None: return {}
    idx = df.index[df["date"] == d]
    if len(idx) == 0: return {}
    i = idx[0]; c0 = df.at[i, "close"]; out = {}
    for k in ks:
        j = i + k
        if j < len(df) and c0 > 0: out[k] = df.at[j, "close"] / c0 - 1.0
    return out

dates = sorted([d for d in os.listdir(ANA) if d.startswith("2026-")
                and glob.glob(os.path.join(ANA, d, "??????.json"))])
rows = []
for d in dates:
    for f in glob.glob(os.path.join(ANA, d, "??????.json")):
        code = os.path.basename(f)[:-5]
        try: rec = json.load(open(f))
        except Exception: continue
        cc = (rec.get("council") or {}).get("default") or {}
        if "综合分" not in cc: continue
        ds = float(cc.get("综合分") or 0.0)
        num = den = 0.0
        for a in cc.get("归因", []):
            if a.get("专家") in MSG:
                num += float(a.get("贡献", 0) or 0); den += float(a.get("权重", 0) or 0) * float(a.get("置信度", 0) or 0)
        S_msg = (num / den) if den > 0 else 0.0
        ms = msg_score(S_msg)
        fr = fwd(code, d)
        if not fr: continue
        rows.append({"date": d, "code": code, "msg": ms, "S_msg": round(S_msg, 4), "data": ds,
                     **{f"fwd{k}": fr.get(k, np.nan) for k in (1, 2, 3, 5)}})

df = pd.DataFrame(rows)
print("样本(有council.default且有前向收益):", len(df), "| 交易日:", df['date'].nunique())
print("发声(msg!=0)占比 %.1f%%  看多%d 看空%d" % (100 * (df['msg'] != 0).mean(),
      int((df['msg'] > 0).sum()), int((df['msg'] < 0).sum())))
print("完整分基数 data 分布: mean=%.3f std=%.3f  msg分布 mean=%.3f std=%.3f(仅发声 std=%.3f)" % (
      df['data'].mean(), df['data'].std(), df['msg'].mean(), df['msg'].std(), df[df['msg'] != 0]['msg'].std()))
print()
def sp(a, b):
    m = (~a.isna()) & (~b.isna())
    if m.sum() < 10: return np.nan, int(m.sum())
    return a[m].rank().corr(b[m].rank()), int(m.sum())

print("=== A) 消息面分 vs 前向收益 Spearman IC ===")
print("--全样本--")
for k in (1, 2, 3, 5):
    ic, n = sp(df["msg"], df[f"fwd{k}"]); print(f"  T+{k}: IC={ic:+.4f} n={n}")
sub = df[df["msg"] != 0]
print(f"--仅发声(n={len(sub)}), 用连续 S_msg--")
for k in (1, 2, 3, 5):
    ic, n = sp(sub["S_msg"], sub[f"fwd{k}"]); print(f"  T+{k}: IC={ic:+.4f} n={n}")
print("--按日 RankIC 均值(每日≥20票)--")
for k in (1, 2, 3, 5):
    ics = [sp(g["msg"], g[f"fwd{k}"])[0] for _, g in df.groupby("date") if sp(g["msg"], g[f"fwd{k}"])[1] >= 20]
    ics = [x for x in ics if not math.isnan(x)]
    if ics: print(f"  T+{k}: meanIC={np.mean(ics):+.4f} t≈{np.mean(ics)/(np.std(ics)/math.sqrt(len(ics))+1e-9):+.2f} days={len(ics)}")

print("\n=== B) 消息面分方向分档 → 平均前向收益% ===")
def bkt(x):
    if x >= 0.3: return "1_强看多≥.3"
    if x > 0: return "2_弱看多(0,.3)"
    if x == 0: return "3_中性0"
    if x > -0.3: return "4_弱看空(-.3,0)"
    return "5_强看空≤-.3"
df["b"] = df["msg"].apply(bkt); g = df.groupby("b")
tab = g[["fwd1", "fwd3", "fwd5"]].mean() * 100; cnt = g.size()
for b in sorted(df["b"].unique()):
    print(f"  {b:14s} n={cnt[b]:5d} T+1={tab.loc[b,'fwd1']:+.2f} T+3={tab.loc[b,'fwd3']:+.2f} T+5={tab.loc[b,'fwd5']:+.2f}")

print("\n=== C) 回灌权重 Top-N 前向收益(每日按 data+w*msg 排序取TopN 再平均)===")
for N in (5, 10, 15):
    print(f"--Top-{N}--")
    for w in (0.0, 0.3, 0.5, 0.8, 1.2):
        pd_ = {1: [], 3: [], 5: []}
        for d, gg in df.groupby("date"):
            gg = gg.assign(full=gg["data"] + w * gg["msg"]).sort_values("full", ascending=False).head(N)
            for k in (1, 3, 5):
                v = gg[f"fwd{k}"].mean()
                if not math.isnan(v): pd_[k].append(v)
        print(f"  w={w}: " + " ".join(f"T+{k}={100*np.mean(pd_[k]):+.2f}%" for k in (1, 3, 5)) + f"  days={len(pd_[5])}")

print("\n=== D) 基线 ===")
for k in (1, 3, 5): print(f"  全样本 T+{k} 均值={100*df[f'fwd{k}'].mean():+.2f}%  中位={100*df[f'fwd{k}'].median():+.2f}%")
os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
df.to_csv(OUT_CSV, index=False)
print(f"\nrows saved -> {OUT_CSV}")
