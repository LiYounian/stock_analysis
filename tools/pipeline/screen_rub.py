"""通达信「拉揉搓」全 A 日线策略。"""
from __future__ import annotations
import pandas as pd
from tools.collectors import market
from tools.store import repo as store
VIEW_NAME="拉揉搓"

def signal(df,pos):
    if pos < 249: return None
    x=df.iloc[:pos+1]; c=x.close.astype(float); o=x.open.astype(float); lo=x.low.astype(float); h=x.high.astype(float)
    ma=lambda n:float(c.iloc[-n:].mean())
    cond1=ma(10)>ma(20)>ma(30)>ma(60)>ma(200)
    h52=float(h.iloc[-250:].max()); cond2=float(c.iloc[-1])>h52*.9 and float(c.iloc[-1])<h52*1.2
    yang=int((c.iloc[-10:]>o.iloc[-10:]).sum()); yin=int((c.iloc[-10:]<o.iloc[-10:]).sum()); cond3=yang>yin
    dip=(float(lo.iloc[-1])/float(c.iloc[-2])-1)<=-.04; cond4=dip and float(c.iloc[-1])>float(o.iloc[-1])
    return bool(cond1 and cond2 and cond3 and cond4),{"close":round(float(c.iloc[-1]),2),"yang":yang,"yin":yin,"dip%":round((float(lo.iloc[-1])/float(c.iloc[-2])-1)*100,2),"checks":{"均线多头":bool(cond1),"高位区间":bool(cond2),"阳线多于阴线":bool(cond3),"下跌4%后收阳":bool(cond4)}}

def backfill(codes,start,end):
    byday={}; eligible={}
    for code in codes:
      try: df=market.load_kline(code).reset_index(drop=True)
      except FileNotFoundError: continue
      for pos,d in enumerate(df.date):
        day=pd.Timestamp(d).strftime('%Y-%m-%d')
        if not start<=day<=end: continue
        r=signal(df,pos)
        if r is None: continue
        eligible[day]=eligible.get(day,0)+1
        if r[0]: byday.setdefault(day,[]).append({"code":code,"明细":r[1]})
    for day,n in eligible.items():
      sel=byday.get(day,[]); store.put_view(VIEW_NAME,{"as_of":day,"策略":VIEW_NAME,"范围":"全A（除北交所）","扫描数":len(codes),"有效样本":n,"入选数":len(sel),"占比%":round(len(sel)/n*100,2),"入选清单":sel,"口径":"MA10>20>30>60>200；250日高点90%-120%；10日阳线多于阴线；当日最低较昨收跌≥4%且收阳。"},date=day)
    return len(eligible)
