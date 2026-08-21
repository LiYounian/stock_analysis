"""三套通达信量价策略的全 A 日线回放。

所有策略产出统一的 view schema，页面和 K 线详情页因此可复用，不为每套策略复制展示代码。
"""
from __future__ import annotations

from collections import defaultdict
import pandas as pd

from tools.collectors import market
from tools.store import repo as store

SINGLE = "单日放量"
LOW = "低位单日放量"
CONTINUOUS = "连续放量"


def _ma(c: pd.Series, n: int) -> float:
    return float(c.iloc[-n:].mean())


def _weekly_ma30_dynamic(df: pd.DataFrame) -> list[float | None]:
    """一次构建整段动态 30 周线，避免全 A 回放中按日重复 groupby。

    当前周使用当天收盘，其余 29 周使用已结束周的最终收盘，因此不引用未来价格。
    """
    weeks = pd.to_datetime(df.date).dt.to_period("W-FRI")
    close = df.close.astype(float).tolist()
    ids, finals, current = [], [], None
    for i, week in enumerate(weeks):
        if week != current:
            ids.append(i); current = week
    ids.append(len(df))
    for left, right in zip(ids, ids[1:]):
        finals.append(close[right - 1])
    week_index = pd.Series(weeks).factorize(sort=True)[0]
    prefix = [0.0]
    for x in finals:
        prefix.append(prefix[-1] + x)
    out = []
    for i, wi in enumerate(week_index):
        if wi < 29:
            out.append(None)
        else:
            prior_sum = prefix[wi] - prefix[wi - 29]
            out.append((prior_sum + close[i]) / 30)
    return out


def single_signal(df: pd.DataFrame, pos: int, turnover: float | None, prior_turnover: float | None):
    if pos < 200 or turnover is None or prior_turnover is None or prior_turnover <= 0:
        return None
    x = df.iloc[:pos + 1]; c = x.close.astype(float)
    ma200, prior_ma200 = _ma(c, 200), float(c.iloc[-201:-1].mean())
    ma50 = _ma(c, 50)
    close, prior_close = float(c.iloc[-1]), float(c.iloc[-2])
    checks = {
        "换手放大": turnover > 1.7 * prior_turnover,
        "收盘涨超3%": close > prior_close * 1.03,
        "MA200上行": ma200 > prior_ma200,
        "MA50在MA200上": ma50 > ma200,
    }
    return all(checks.values()), {"close": round(close, 2), "turnover": round(turnover, 2),
                                   "prior_turnover": round(prior_turnover, 2), "checks": checks}


def low_signal(df: pd.DataFrame, pos: int, weekly_ma30: list[float | None]):
    if pos < 200:
        return None
    x = df.iloc[:pos + 1]; c=x.close.astype(float); v=x.volume.astype(float)
    ma5,ma10,ma20,ma30,ma200=(_ma(c,n) for n in (5,10,20,30,200))
    weekly, prior_weekly = weekly_ma30[pos], weekly_ma30[pos - 1]
    if weekly is None or prior_weekly is None:
        return None
    close, prior_close=float(c.iloc[-1]),float(c.iloc[-2])
    checks={
        "站上日线均线": close>ma5 and close>ma10 and close>ma20 and close>ma30 and close>ma200,
        "上穿30周线": close>weekly and prior_close<=prior_weekly,
        "10日最大成交量": float(v.iloc[-1])>=float(v.iloc[-10:].max()),
    }
    return all(checks.values()), {"close":round(close,2),"ma30w":round(weekly,2),
                                  "volume":round(float(v.iloc[-1])),"checks":checks}


def continuous_signal(df: pd.DataFrame, pos: int):
    if pos < 200:
        return None
    x=df.iloc[:pos+1]; c=x.close.astype(float); v=x.volume.astype(float)
    close,p1,p2=float(c.iloc[-1]),float(c.iloc[-2]),float(c.iloc[-3])
    checks={
        "连续收盘走高": close>p1 and close>p2,
        "相对昨日前日涨超4%": (close/p1-1)>.04 and (close/p2-1)>.04,
        "成交量递增": float(v.iloc[-1])>float(v.iloc[-2]),
        "站上20_50_200均线": close>_ma(c,20) and close>_ma(c,50) and close>_ma(c,200),
        "MA5_MA10高于MA20": _ma(c,5)>_ma(c,20) and _ma(c,10)>_ma(c,20),
    }
    return all(checks.values()), {"close":round(close,2),"rise1%":round((close/p1-1)*100,2),
                                  "rise2%":round((close/p2-1)*100,2),"checks":checks}


def _daily_turnover(days: set[str]) -> dict[str, dict[str, float]]:
    out = {}
    for day in days:
        try:
            x=store.get_master_daily_basic(day)
        except FileNotFoundError:
            continue
        out[day]={str(r.code).zfill(6):float(r.turnover) for r in x.itertuples() if pd.notna(r.turnover)}
    return out


def backfill(codes: list[str], start: str, end: str, kinds: tuple[str, ...] = (SINGLE, LOW, CONTINUOUS)) -> dict[str, int]:
    """回放三个策略；单日放量依赖已经补齐的 daily_basic 快照。"""
    days = {pd.Timestamp(d).strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)}
    turnover = _daily_turnover(days)
    selected = {k: defaultdict(list) for k in kinds}; eligible = {k: defaultdict(int) for k in kinds}
    for code in codes:
        try: df=market.load_kline(code).reset_index(drop=True)
        except FileNotFoundError: continue
        weekly_ma30 = _weekly_ma30_dynamic(df) if LOW in kinds else []
        for pos, raw_day in enumerate(df.date):
            day=pd.Timestamp(raw_day).strftime("%Y-%m-%d")
            if not start <= day <= end:
                continue
            prior=pd.Timestamp(df.date.iloc[pos-1]).strftime("%Y-%m-%d") if pos else ""
            evaluators = {
                SINGLE: single_signal(df,pos,turnover.get(day,{}).get(code),turnover.get(prior,{}).get(code)),
                LOW: low_signal(df,pos,weekly_ma30),
                CONTINUOUS: continuous_signal(df,pos),
            }
            for name, result in evaluators.items():
                if name not in kinds or result is None:
                    continue
                eligible[name][day] += 1
                if result[0]: selected[name][day].append({"code":code,"明细":result[1]})
    for name in kinds:
        for day, n in eligible[name].items():
            rows=selected[name][day]
            store.put_view(name, {"as_of":day,"策略":name,"范围":"全A（除北交所）","扫描数":len(codes),
                "有效样本":n,"入选数":len(rows),"占比%":round(len(rows)/n*100,2),"入选清单":rows,
                "口径":"按通达信公式逐日回放；单日放量的 HSL 使用 Tushare daily_basic 换手率（百分比）。"}, date=day)
    return {name: len(eligible[name]) for name in kinds}
