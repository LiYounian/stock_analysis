"""诊断2:池外universe基线 mean_r + 精炼/组合召回通道的 mean_r/precision(找能跑赢基线的)。"""
import json, csv, statistics, sys
from pathlib import Path
WT="/Users/yqg/Documents/projects/worktrees/stock_analysis/shadow-pool-v2"; sys.path.insert(0,WT)
from tools.analysis import shadow_pool
DR=Path("/Users/yqg/Documents/projects/stock_analysis/data/analysis")
ART=Path(WT)/"docs/计划/2026-09-14_候选池v2_AB_artifacts"
DAYS=["2026-08-11","2026-08-13","2026-08-14","2026-08-18","2026-08-20","2026-08-24","2026-08-26","2026-08-28","2026-08-31","2026-09-01","2026-09-02","2026-09-03","2026-09-04","2026-09-08","2026-09-09","2026-09-10"]
def load_sc(p):
    out={}
    with open(p,encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d,c=row.get("date"),row.get("code")
            if not d or not c: continue
            def num(k):
                v=row.get(k)
                try: return float(v) if v not in (None,"") else None
                except: return None
            out.setdefault(d,{})[c]={"r_1":num("r_1"),"r_5":num("r_5")}
    return out
SC=load_sc("/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv")
def rec(d,c):
    p=DR/d/f"{c}.json"
    if not p.exists(): return {}
    try: return json.loads(p.read_text(encoding="utf-8"))
    except: return {}
def uni(d): return [f.stem for f in (DR/d).glob("*.json") if len(f.stem)==6 and f.stem.isdigit()]

# candidate channels: name -> predicate(record, hit_count)->bool
def mk():
    def g(r,path,default=None):
        cur=r
        for k in path.split("."):
            cur=(cur or {}).get(k) if isinstance(cur,dict) else None
        return cur
    chans={}
    chans["screen>=2"]      =lambda r,h: h>=2
    chans["screen>=3"]      =lambda r,h: h>=3
    chans["ff_strong"]      =lambda r,h: isinstance(g(r,"fundflow.主力连续净流入天数"),(int,float)) and g(r,"fundflow.主力连续净流入天数")>=5 and isinstance(g(r,"fundflow.今日主力净占比"),(int,float)) and g(r,"fundflow.今日主力净占比")>=0.15
    chans["screen2+volconf"]=lambda r,h: h>=2 and isinstance(g(r,"snapshot.vol_ratio"),(int,float)) and g(r,"snapshot.vol_ratio")>=1.5
    chans["screen2+ffpos"]  =lambda r,h: h>=2 and isinstance(g(r,"fundflow.今日主力净流入"),(int,float)) and g(r,"fundflow.今日主力净流入")>0
    chans["volconf+ffpos"]  =lambda r,h: isinstance(g(r,"snapshot.vol_ratio"),(int,float)) and g(r,"snapshot.vol_ratio")>=2.0 and isinstance(g(r,"snapshot.pct_chg"),(int,float)) and 2.0<=g(r,"snapshot.pct_chg")<=9.5 and isinstance(g(r,"fundflow.今日主力净流入"),(int,float)) and g(r,"fundflow.今日主力净流入")>0
    chans["ma_trend+ffpos"] =lambda r,h: (g(r,"signals.trend") in ("多头","偏多","上升","强势") ) and isinstance(g(r,"fundflow.主力连续净流入天数"),(int,float)) and g(r,"fundflow.主力连续净流入天数")>=3
    return chans

CH=mk()
base_r=[]; stats={k:{"r":[],"win5":0,"n":0} for k in CH}
for d in DAYS:
    pool=set(json.loads((ART/f"pool_v2_{d}.json").read_text())["pool"])
    sc=SC.get(d,{})
    for c in uni(d):
        if c in pool: continue
        r1=sc.get(c,{}).get("r_1")
        if r1 is not None: base_r.append(r1)
        # hit count
        pass
# recompute with screens per day
stats={k:{"r":[],"win5":0,"n":0} for k in CH}
for d in DAYS:
    pool=set(json.loads((ART/f"pool_v2_{d}.json").read_text())["pool"])
    sc=SC.get(d,{})
    screens=shadow_pool._screens(str(DR),d); hit={}
    for nm,codes in screens.items():
        for c in set(codes): hit[c]=hit.get(c,0)+1
    for c in uni(d):
        if c in pool: continue
        r=rec(d,c); h=hit.get(c,0); r1=sc.get(c,{}).get("r_1")
        for k,pred in CH.items():
            try: on=pred(r,h)
            except: on=False
            if on:
                stats[k]["n"]+=1
                if r1 is not None:
                    stats[k]["r"].append(r1)
                    if r1>=5.0: stats[k]["win5"]+=1
bm=statistics.mean(base_r); bwin=sum(1 for x in base_r if x>=5.0)/len(base_r)
print(f"池外universe基线: n={len(base_r)} mean_r={bm:+.3f} win5_rate={bwin:.3f}")
print(f"{'channel':18} fire  mean_r   Δvs_base  win5_rate  win5_lift")
for k,s in stats.items():
    if not s['r']: print(f"{k:18} 0"); continue
    m=statistics.mean(s['r']); wr=s['win5']/len(s['r'])
    print(f"{k:18} {s['n']:4d} {m:+.3f}  {m-bm:+.3f}   {wr:.3f}     {wr/bwin:.2f}x")
