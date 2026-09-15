"""构建行业级 4 数值维面板(估值/动量/拥挤/波动),口径A = native 申万指数(~1.5yr)。

复用:
  - iet_probe.pipeline.build_panel_snapshot → 行业 pe_median / turnover_mean(快照成分,弱前视)
  - industry_temp.temperature.build_temperature_series → 因果分位 + A/B(动量/估值/换手)
  - industry_temp.volatility → 波动维(新建,从 board 指数收盘算已实现波动分位)

输出统一 long 表:[date, industry, dim, value, ab]
  dim ∈ {估值, 动量, 拥挤, 波动};value=连续量(供 rank-IC);ab ∈ {"A","B",None}(供 A/B 分组)。

维度→连续量约定:
  估值 = val_pctile(高=贵=过热侧A)     动量 = RS-Momentum 原值(高=强动量=A侧,中枢100)
  拥挤 = turn_pctile(高=拥挤=过热侧A)   波动 = vol_pctile(高波动=A侧,过热/过冷由回测揭示)
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from tools.analysis import industry_map
from tools.analysis.industry_temp import temperature as TEMP
from tools.analysis.industry_temp import volatility as VOL
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PIPE

logger = logging.getLogger("industry_thermometer.dims")

DIMS = ("估值", "动量", "拥挤", "波动")


def _board_close(industry: str) -> Optional[pd.Series]:
    """行业 native 申万指数收盘序列(index=date 升序);缺 → None。"""
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
    s = s.dropna().sort_values("date").set_index("date")["close"]
    return s.astype(float)


def build_dim_panel(
    membership: dict,
    target_dates: list[str],
    *,
    vol_win: int = VOL.VOL_WIN,
    pctile_win: int = TEMP.PCTILE_WIN,
    pctile_min: int = TEMP.PCTILE_MIN,
) -> pd.DataFrame:
    """快照成分 + 目标交易日 → 4 维 long 面板 [date, industry, dim, value, ab]。

    membership: {code: 申万一级名}(静态快照)。
    """
    panel = PIPE.build_panel_snapshot(membership, target_dates)
    if panel.empty:
        return pd.DataFrame(columns=["date", "industry", "dim", "value", "ab"])

    # 动量/估值/换手 的因果分位 + A/B(复用温度序列)
    temp = TEMP.build_temperature_series(
        panel, D.rs_momentum_of, win=pctile_win, min_periods=pctile_min,
    )
    # temp: [date, industry, k, mom_ab, val_ab, turn_ab, val_pctile, turn_pctile]
    temp = temp.copy()
    temp["mom_raw"] = [
        D.rs_momentum_of(ind, d) for ind, d in zip(temp["industry"], temp["date"])
    ]

    # 波动维:各行业 board 指数收盘 → vol_pctile(因果),再 as-of 对齐到温度序列的 date
    vol_lookup: dict[str, pd.Series] = {}
    for ind in temp["industry"].unique():
        close = _board_close(ind)
        if close is None or close.empty:
            continue
        vp = VOL.vol_pctile_series(
            close, vol_win=vol_win, pctile_win=pctile_win, pctile_min=pctile_min,
        )
        vol_lookup[ind] = vp.dropna()

    def _vol_at(ind: str, date: str) -> Optional[float]:
        vp = vol_lookup.get(ind)
        if vp is None or vp.empty:
            return None
        sub = vp[vp.index <= date]
        if sub.empty:
            return None
        return float(sub.iloc[-1])

    rows = []
    for _i, r in temp.iterrows():
        d, ind = r["date"], r["industry"]
        val_p = r.get("val_pctile")
        turn_p = r.get("turn_pctile")
        mom = r.get("mom_raw")
        vol_p = _vol_at(ind, d)
        rows.append({"date": d, "industry": ind, "dim": "估值",
                     "value": _f(val_p), "ab": r.get("val_ab")})
        rows.append({"date": d, "industry": ind, "dim": "动量",
                     "value": _f(mom), "ab": r.get("mom_ab")})
        rows.append({"date": d, "industry": ind, "dim": "拥挤",
                     "value": _f(turn_p), "ab": r.get("turn_ab")})
        rows.append({"date": d, "industry": ind, "dim": "波动",
                     "value": _f(vol_p), "ab": VOL.ab_volatility(vol_p)})
    return pd.DataFrame(rows, columns=["date", "industry", "dim", "value", "ab"])


def _f(v):
    return None if v is None or pd.isna(v) else float(v)
