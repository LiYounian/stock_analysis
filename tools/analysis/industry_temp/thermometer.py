"""板块环境层(生产·native 口径)——多策略可复用的行业冷热信号。

契约见 docs/参考/板块环境层接口契约.md。**因果 PIT**:一切值只用 ≤date 数据。
信号:
  ①拥挤度(时序分位+A/B,已立**截面前瞻IC有预测力**)
  ②动量冷热(20日行业指数动量;**两种分位都出**:时序=vs自身历史、截面=当日vs其它行业)
  ③情绪(P-B·LLM,未就绪=None)
⚠️ 用法边界:截面前瞻 IC 有效 ≠ 组合可交易(冷/低拥挤信号弱市扎堆=集中度风险);只宜作过滤/降权辅助。
生产用 native 口径(申万指数+成分**as-of快照**聚合,比 pattern 的当前映射套历史更干净);deep 研究口径不在本层暴露。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from tools.analysis import industry_map
from tools.analysis.industry_temp import temperature as TEMP
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PIPE

MOM_WIN = 20                      # 动量回看(交易日)
TS_COLD, TS_HOT = 1 / 3, 2 / 3    # 时序档阈值
CS_COLD, CS_HOT = 0.2, 0.8        # 截面档阈值(与 pattern 对齐)

PANEL_COLS = ["date", "industry", "拥挤分位", "拥挤",
              "动量_时序分位", "动量_时序档", "动量_截面分位", "动量_截面档",
              "情绪", "n_members"]


def _board_close(industry: str) -> Optional[pd.Series]:
    from tools.collectors import board
    sw = industry_map.to_sw(industry) or industry
    try:
        bk = board.load_board_kline(sw)
    except Exception:
        return None
    if bk is None or bk.empty:
        return None
    s = bk[["date", "close"]].copy()
    s["date"] = pd.to_datetime(s["date"]).dt.strftime("%Y-%m-%d")
    return s.dropna().sort_values("date").set_index("date")["close"].astype(float)


def _mom20(close: pd.Series, mom_win: int = MOM_WIN) -> pd.Series:
    """20日行业指数收益(因果):close[t]/close[t-win]-1。"""
    return close / close.shift(mom_win) - 1.0


def _band(pctile: Optional[float], cold: float, hot: float) -> Optional[str]:
    if pctile is None or pd.isna(pctile):
        return None
    return "冷" if pctile < cold else ("热" if pctile > hot else "温")


def build_thermometer_panel(dates: list[str], membership: dict, *,
                            口径: str = "native") -> pd.DataFrame:
    """批量:各行业各 date 的板块环境信号(因果 PIT)。消费方回测优先用这个一次性构。"""
    if 口径 != "native":
        raise ValueError("生产层只暴露 native 口径;deep 为研究口径,见 backtest/industry_thermometer/deep.py")
    panel = PIPE.build_panel_snapshot(membership, dates)
    if panel.empty:
        return pd.DataFrame(columns=PANEL_COLS)

    # 拥挤:换手因果时序分位(复用温度序列,已验证口径)
    temp = TEMP.build_temperature_series(panel, lambda i, d: None)
    temp = temp.merge(panel[["date", "industry", "n_members"]],
                      on=["date", "industry"], how="left")

    # 动量:各行业 board 指数 mom20;时序分位(vs自身历史)+ 原值(供截面 rank)
    mom_raw_lookup, mom_ts_lookup = {}, {}
    for ind in temp["industry"].unique():
        close = _board_close(ind)
        if close is None or close.empty:
            continue
        m20 = _mom20(close)
        mom_raw_lookup[ind] = m20.dropna()
        mom_ts_lookup[ind] = TEMP.causal_rolling_pctile(m20).dropna()

    def _asof(lookup, ind, date):
        s = lookup.get(ind)
        if s is None or s.empty:
            return None
        sub = s[s.index <= date]
        return float(sub.iloc[-1]) if not sub.empty else None

    # 先取每 (date,industry) 的 mom20 原值,再按 date 截面 rank → 截面分位
    tmp_rows = []
    for _i, r in temp.iterrows():
        d, ind = r["date"], r["industry"]
        tmp_rows.append({
            "date": d, "industry": ind,
            "拥挤分位": _f(r.get("turn_pctile")), "拥挤": r.get("turn_ab"),
            "动量_时序分位": _asof(mom_ts_lookup, ind, d),
            "_mom_raw": _asof(mom_raw_lookup, ind, d),
            "n_members": int(r.get("n_members") or 0),
        })
    df = pd.DataFrame(tmp_rows)
    # 截面分位:当日各行业 mom20 原值的 rank pct(vs 其它行业)
    df["动量_截面分位"] = (df.groupby("date")["_mom_raw"]
                        .rank(pct=True, method="average"))
    rows = []
    for _i, r in df.iterrows():
        ts, cs = r["动量_时序分位"], r.get("动量_截面分位")
        rows.append({
            "date": r["date"], "industry": r["industry"],
            "拥挤分位": r["拥挤分位"], "拥挤": r["拥挤"],
            "动量_时序分位": ts, "动量_时序档": _band(ts, TS_COLD, TS_HOT),
            "动量_截面分位": _f(cs), "动量_截面档": _band(cs, CS_COLD, CS_HOT),
            "情绪": None,                       # P-B 未就绪
            "n_members": r["n_members"],
        })
    return pd.DataFrame(rows, columns=PANEL_COLS)


def _trailing_dates(date: str, span_days: int = 420) -> list[str]:
    from datetime import datetime, timedelta
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=span_days)).strftime("%Y-%m-%d")
    return PIPE.trading_calendar(start=start, end=date)


def get_industry_thermometer(date: str, *, 口径: str = "native",
                             membership: Optional[dict] = None) -> dict:
    """申万一级全行业在 date 的板块环境信号(因果 PIT)。契约见接口文档 §2。"""
    D.bind_main_repo()
    if membership is None:
        from tools.collectors import code_industry
        snap = code_industry.load()
        membership = {c: industry_map.to_sw(r) for c, r in snap.items()
                      if r and industry_map.to_sw(r)}
    dates = _trailing_dates(date)
    panel = build_thermometer_panel(dates, membership, 口径=口径)
    out = {}
    for ind, g in panel.groupby("industry"):
        g = g[g["date"] <= date]
        if g.empty:
            continue
        last = g.iloc[-1]
        out[ind] = {
            "拥挤分位": last["拥挤分位"], "拥挤": last["拥挤"],
            "动量_时序分位": last["动量_时序分位"], "动量_时序档": last["动量_时序档"],
            "动量_截面分位": last["动量_截面分位"], "动量_截面档": last["动量_截面档"],
            "情绪": last["情绪"], "n_members": int(last["n_members"]),
            "asof": last["date"],
        }
    return out


def industry_heat(code: str, date: str, *, 口径: str = "native") -> Optional[dict]:
    """个股 code 所属行业在 date 的冷热(pattern 最小接口)。因果 PIT;无归属/数据→None。

    冷热档默认给**截面档**(与 pattern rotation 语义一致);时序分位一并返回供备选。
    """
    from tools.collectors import code_industry
    snap = code_industry.load()
    raw = snap.get(code)
    ind = industry_map.to_sw(raw) if raw else None
    if not ind:
        return None
    therm = get_industry_thermometer(date, 口径=口径)
    sig = therm.get(ind)
    if sig is None:
        return None
    return {"industry": ind,
            "冷热档": sig["动量_截面档"], "动量_截面分位": sig["动量_截面分位"],
            "动量_时序分位": sig["动量_时序分位"], "拥挤": sig["拥挤"], "asof": sig["asof"]}


def _f(v):
    return None if v is None or pd.isna(v) else float(v)
