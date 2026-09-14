"""诊断:16日 shadow 样本里,v2 池外的"赢家"分布 + 各召回通道的覆盖/噪声(不调参,只统计)。"""
import json, csv, statistics
from pathlib import Path
import sys
WT = "/Users/yqg/Documents/projects/worktrees/stock_analysis/shadow-pool-v2"
sys.path.insert(0, WT)
from tools.analysis import shadow_pool

DR = Path("/Users/yqg/Documents/projects/stock_analysis/data/analysis")
ART = Path(WT) / "docs/计划/2026-09-14_候选池v2_AB_artifacts"
DAYS = ["2026-08-11","2026-08-13","2026-08-14","2026-08-18","2026-08-20","2026-08-24",
        "2026-08-26","2026-08-28","2026-08-31","2026-09-01","2026-09-02","2026-09-03",
        "2026-09-04","2026-09-08","2026-09-09","2026-09-10"]

# scorecard
def load_scorecard(path):
    out={}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            d,c=row.get("date"),row.get("code")
            if not d or not c: continue
            def num(k):
                v=row.get(k)
                try: return float(v) if v not in (None,"") else None
                except: return None
            out.setdefault(d,{})[c]={"r_1":num("r_1"),"r_5":num("r_5")}
    return out
SC = load_scorecard("/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv")

def rec(date, code):
    p = DR/date/f"{code}.json"
    if not p.exists(): return {}
    try: return json.loads(p.read_text(encoding="utf-8"))
    except: return {}

def records_universe(date):
    return [f.stem for f in (DR/date).glob("*.json") if len(f.stem)==6 and f.stem.isdigit()]

WIN_CUT = 0.05   # 赢家定义:r_1 >= 5%
per_day=[]
chan_stats={c:{"catch_win":0,"fire_total":0,"fire_r":[]} for c in
            ["screen>=2","fundflow_surge","sentiment_cat","vol_anom","pctchg_strong"]}
tot_missed_win=0
for d in DAYS:
    pool = set(json.loads((ART/f"pool_v2_{d}.json").read_text())["pool"])
    day_sc = SC.get(d,{})
    uni = records_universe(d)
    screens = shadow_pool._screens(str(DR), d)
    hit={}
    for name,codes in screens.items():
        for c in set(codes): hit[c]=hit.get(c,0)+1
    winners = [c for c in uni if (day_sc.get(c,{}).get("r_1") is not None and day_sc[c]["r_1"]>=WIN_CUT)]
    missed = [c for c in winners if c not in pool]
    tot_missed_win += len(missed)
    # channel fire predicates (as-of pick-day signals)
    def fires(code):
        r=rec(d,code); out={}
        out["screen>=2"]= hit.get(code,0)>=2
        ff=r.get("fundflow") or {}
        days_in=ff.get("主力连续净流入天数"); occ=ff.get("今日主力净占比")
        out["fundflow_surge"]= (isinstance(days_in,(int,float)) and days_in>=3) and (isinstance(occ,(int,float)) and occ>=0.10)
        se=r.get("sentiment") or {}
        out["sentiment_cat"]= (isinstance(se.get("净情绪分"),(int,float)) and se["净情绪分"]>=0.5) and (isinstance(se.get("利好数"),(int,float)) and se["利好数"]>=3)
        sn=r.get("snapshot") or {}
        vr=sn.get("vol_ratio"); pc=sn.get("pct_chg")
        out["vol_anom"]= (isinstance(vr,(int,float)) and vr>=2.0) and (isinstance(pc,(int,float)) and 2.0<=pc<=9.5)
        out["pctchg_strong"]= (isinstance(pc,(int,float)) and pc>=5.0)
        return out
    # channel coverage on missed winners + noise on full non-pool universe
    for code in uni:
        if code in pool: continue
        f=fires(code)
        rr=day_sc.get(code,{}).get("r_1")
        is_win = code in missed
        for ch,on in f.items():
            if on:
                chan_stats[ch]["fire_total"]+=1
                if rr is not None: chan_stats[ch]["fire_r"].append(rr)
                if is_win: chan_stats[ch]["catch_win"]+=1
    per_day.append((d,len(uni),len(winners),len(missed)))

print(f"WIN_CUT r_1>={WIN_CUT:.0%}")
print(f"{'date':12} uni win missed(池外赢家)")
for d,u,w,m in per_day: print(f"{d:12} {u:4d} {w:3d} {m:3d}")
print(f"\n总池外赢家(16日): {tot_missed_win}")
print(f"\n各通道(在池外universe上): catch_win/命中赢家  fire_total/触发数  fire_mean_r  precision(赢家占触发)")
for ch,s in chan_stats.items():
    fr=s["fire_r"]; mean_r=statistics.mean(fr) if fr else None
    prec = s["catch_win"]/s["fire_total"] if s["fire_total"] else None
    cov = s["catch_win"]/tot_missed_win if tot_missed_win else None
    print(f"  {ch:16} catch={s['catch_win']:3d}(cov {cov:.2f})  fire={s['fire_total']:4d}  mean_r={mean_r:+.3f}  win_prec={prec:.3f}" if mean_r is not None and prec is not None else f"  {ch:16} catch={s['catch_win']} fire={s['fire_total']}")
