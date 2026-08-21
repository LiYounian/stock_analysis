"""通达信「最强选股」：全 A 日线 + Tushare 筹码获利比例。"""
from __future__ import annotations
import logging
import pandas as pd
from tools.collectors import market, tushare_daily
from tools.store import repo as store

VIEW_NAME = "最强选股"
logger = logging.getLogger("pipeline.screen_strong")

def _signal(df, pos, chip):
    if pos < 249: return None
    x = df.iloc[:pos+1]; c=x.close.astype(float); h=x.high.astype(float)
    ma=lambda n: c.iloc[-n:].mean()
    long = ma(5)>ma(10)>ma(20)>ma(30)>ma(60)>ma(200)
    rise = int((c.iloc[-11:] / c.shift(1).iloc[-11:] >= 1.05).sum()) >= 2
    hi=h.iloc[-250:].max(); close=float(c.iloc[-1]); high=float(h.iloc[-1])
    zone=close>hi*.9 and close<hi*1.2
    # Tushare winner_rate 是 WINNER(CLOSE) 的百分比；cost_95pct 是 WINNER(price)=95% 的价格。
    win=chip is not None and (float(chip.winner_rate)>95 or high>=float(chip.cost_95pct))
    return bool(win and long and rise and zone), {"close":round(close,2),"winner_rate":None if chip is None else float(chip.winner_rate),"cost_95pct":None if chip is None else float(chip.cost_95pct),"high":round(high,2),"ma_long":bool(long),"rise_5pct":bool(rise),"price_zone":bool(zone),"chip_win":bool(win)}

def backfill(codes, start, end):
    frames={}
    for code in codes:
      try: frames[code]=market.load_kline(code).reset_index(drop=True)
      except FileNotFoundError: pass
    days=sorted({pd.Timestamp(d).strftime('%Y-%m-%d') for df in frames.values() for d in df.date if start<=pd.Timestamp(d).strftime('%Y-%m-%d')<=end})
    for ix,day in enumerate(days,1):
      chip=tushare_daily._pro().cyq_perf(trade_date=day.replace('-',''),fields='ts_code,trade_date,winner_rate,cost_95pct')
      chip['code']=chip.ts_code.str.split('.').str[0].str.zfill(6); chips=chip.set_index('code')
      selected=[]; eligible=0
      for code,df in frames.items():
        hit=df.index[pd.to_datetime(df.date).dt.strftime('%Y-%m-%d')==day]
        if not len(hit): continue
        r=_signal(df,int(hit[-1]),chips.loc[code] if code in chips.index else None)
        if r is None: continue
        eligible+=1
        if r[0]: selected.append({'code':code,'明细':r[1]})
      store.put_view(VIEW_NAME,{'as_of':day,'策略':VIEW_NAME,'范围':'全A','扫描数':len(codes),'有效样本':eligible,'入选数':len(selected),'占比%':round(len(selected)/eligible*100,2) if eligible else 0,'入选清单':selected,'口径':'WINNER(CLOSE)=winner_rate；WINNER(HIGH) 以 HIGH≥cost_95pct 严格判定。'},date=day)
      logger.info('最强选股 %d/%d %s: %d',ix,len(days),day,len(selected))
    return len(days)
