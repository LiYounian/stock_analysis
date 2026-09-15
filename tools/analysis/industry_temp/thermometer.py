"""板块环境层(生产·native 口径)——多策略可复用的行业冷热信号。

契约见 docs/参考/板块环境层接口契约.md。**因果 PIT**:一切值只用 ≤date 数据。
信号:①拥挤度(分位+A/B,已立有预测力) ②动量冷热(20日动量因果分位+档,描述性)
      ③情绪(P-B·LLM,未就绪=None)。
生产用 native 口径(申万指数+成分当日快照聚合,干净);deep(个股聚合2018+弱前视)仅研究,
见 tools/backtest/industry_thermometer/deep.py,**不在本生产层暴露**。
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from tools.analysis import industry_map
from tools.analysis.industry_temp import temperature as TEMP
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PIPE

MOM_WIN = 20                 # 动量回看(交易日)
COLD, HOT = 1 / 3, 2 / 3     # 动量档阈值(冷/温/热)


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


def _mom_pctile(close: pd.Series, *, mom_win: int = MOM_WIN,
                pctile_win: int = TEMP.PCTILE_WIN,
                pctile_min: int = TEMP.PCTILE_MIN) -> pd.Series:
    """20日动量的因果历史分位(与 pattern 手搓的申万一级版对齐:行业指数N日动量因果分位)。"""
    mom = close / close.shift(mom_win) - 1.0
    return TEMP.causal_rolling_pctile(mom, win=pctile_win, min_periods=pctile_min)


def _band(pctile: Optional[float]) -> Optional[str]:
    if pctile is None or pd.isna(pctile):
        return None
    return "冷" if pctile < COLD else ("热" if pctile > HOT else "温")


def build_thermometer_panel(dates: list[str], membership: dict, *,
                            口径: str = "native") -> pd.DataFrame:
    """批量:各行业各 date 的板块环境信号(因果 PIT)。消费方回测优先用这个一次性构。

    返回 wide [date, industry, 拥挤分位, 拥挤, 动量冷热分位, 动量档, 情绪, n_members]。
    """
    if 口径 != "native":
        raise ValueError("生产层只暴露 native 口径;deep 为研究口径,见 backtest/industry_thermometer/deep.py")
    panel = PIPE.build_panel_snapshot(membership, dates)
    if panel.empty:
        return pd.DataFrame(columns=["date", "industry", "拥挤分位", "拥挤",
                                     "动量冷热分位", "动量档", "情绪", "n_members"])
    # 拥挤:换手因果分位(复用温度序列的 turn_pctile / turn_ab)
    temp = TEMP.build_temperature_series(panel, lambda i, d: None)  # 动量此处不需要,注入 None
    temp = temp.merge(panel[["date", "industry", "n_members"]],
                      on=["date", "industry"], how="left")
    # 动量冷热:各行业 board 指数 20 日动量因果分位
    mom_lookup = {}
    for ind in temp["industry"].unique():
        close = _board_close(ind)
        if close is None or close.empty:
            continue
        mom_lookup[ind] = _mom_pctile(close).dropna()

    def _mom_at(ind, date):
        mp = mom_lookup.get(ind)
        if mp is None or mp.empty:
            return None
        sub = mp[mp.index <= date]
        return float(sub.iloc[-1]) if not sub.empty else None

    rows = []
    for _i, r in temp.iterrows():
        d, ind = r["date"], r["industry"]
        mp = _mom_at(ind, d)
        rows.append({
            "date": d, "industry": ind,
            "拥挤分位": _f(r.get("turn_pctile")), "拥挤": r.get("turn_ab"),
            "动量冷热分位": _f(mp), "动量档": _band(mp),
            "情绪": None,                       # P-B 未就绪
            "n_members": int(r.get("n_members") or 0),
        })
    return pd.DataFrame(rows, columns=["date", "industry", "拥挤分位", "拥挤",
                                       "动量冷热分位", "动量档", "情绪", "n_members"])


def _trailing_dates(date: str, span_days: int = 420) -> list[str]:
    """截至 date 的交易日历(足够 250 日因果分位预热)。"""
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
            "动量冷热分位": last["动量冷热分位"], "动量档": last["动量档"],
            "情绪": last["情绪"], "n_members": int(last["n_members"]),
            "asof": last["date"],
        }
    return out


def industry_heat(code: str, date: str, *, 口径: str = "native") -> Optional[dict]:
    """个股 code 所属行业在 date 的冷热(pattern 最小接口)。因果 PIT;无归属/数据→None。"""
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
    return {"industry": ind, "冷热档": sig["动量档"],
            "动量冷热分位": sig["动量冷热分位"], "拥挤": sig["拥挤"], "asof": sig["asof"]}


def _f(v):
    return None if v is None or pd.isna(v) else float(v)
