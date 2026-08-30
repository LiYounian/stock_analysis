"""构建行业财报信号回测面板(防未来函数:信号锚定披露日,只取披露后前向收益)。

对每个有 financial_report 的票:
  analyzer.analyze(code, as_of=None) → 逐报告期取 {披露日, quality_score, 评级,
  高危红旗?, 红旗数, 五维, 行业专家}。**信号内在于该报告期,与 as_of 无关**
  (analyze 的 as_of 只做可见性过滤;每期 flags/score 仅用该期及其去年同期数据)。

对每条 (code, period):
  入场日 = 严格晚于披露日的**首个交易日**(跳过披露日跳空,保守、无未来函数)。
  前向收益 = close[入场+H]/close[入场]-1(H=20/60 交易日,后复权 close)。
  区间最大回撤 = 窗口内 close 相对入场后滚动峰值的最深回撤。
  超额 = 个股前向收益 − 等权全A基准同区间收益(market_ew)。

产出 data/analysis/backtest/finval/panel.parquet。
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
KLINE_DIR = os.path.join(ROOT, "data/master/kline")
MKT = os.path.join(ROOT, "data/analysis/backtest/finval/market_ew.parquet")
OUT = os.path.join(ROOT, "data/analysis/backtest/finval/panel.parquet")
# code -> 证监会行业文本(baostock 一次性抓,防未来函数无关:仅行业归属,非价格/财务量;
# 用当前行业近似历史行业——行业变更罕见,注为假设)。缺该文件则不注入,退回通用兜底。
CODE2CSRC = os.path.join(ROOT, "data/analysis/backtest/finval/code2csrc.json")

HORIZONS = [20, 60]


def _list_fr_codes() -> list[str]:
    codes = set()
    for f in glob.glob(os.path.join(ROOT, "data/raw/*/financial_report/*.json")):
        b = os.path.basename(f)
        if b.endswith(".meta.json"):
            continue
        codes.add(b[:-5])
    return sorted(codes)


def _load_kline(code: str) -> pd.DataFrame | None:
    f = os.path.join(KLINE_DIR, f"{code}.parquet")
    if not os.path.exists(f):
        return None
    df = pd.read_parquet(f, columns=["date", "close", "pct_chg"])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def main() -> None:
    import json
    from tools.analysis.financial import analyzer
    from tools.analysis.financial import flags as flags_mod
    from tools.analysis import industry_map

    code2csrc = {}
    if os.path.exists(CODE2CSRC):
        code2csrc = json.load(open(CODE2CSRC, encoding="utf-8"))
        print(f"industry map loaded: {len(code2csrc)} codes")

    mkt = pd.read_parquet(MKT)
    mkt["date"] = pd.to_datetime(mkt["date"])
    mkt = mkt.sort_values("date").reset_index(drop=True)
    mkt_dates = mkt["date"].values
    mkt_idx = mkt["ew_index"].values

    codes = _list_fr_codes()
    print(f"codes with financial_report: {len(codes)}")
    rows = []
    n_ok = 0
    for ci, code in enumerate(codes):
        csrc = code2csrc.get(code)
        try:
            res = analyzer.analyze(code, as_of=None, persist=False, industry=csrc)
        except Exception as e:
            continue
        kl = _load_kline(code)
        if kl is None or kl.empty:
            continue
        kd = kl["date"].values
        kc = kl["close"].values
        expert = res.get("行业专家")
        sw = industry_map.to_sw(csrc) if csrc else None
        for period, p in res.get("periods", {}).items():
            disc = p.get("disclosure_date")
            if not disc:
                continue
            if p.get("is_forecast"):
                continue  # 只用已实现报告,不用预告
            disc_ts = pd.Timestamp(disc)
            # 入场日 = 严格晚于披露日的首个交易日
            pos = int(np.searchsorted(kd, np.datetime64(disc_ts), side="right"))
            if pos >= len(kd):
                continue  # 披露日在样本末尾之后,无前向数据
            entry_i = pos
            entry_date = kd[entry_i]
            entry_close = kc[entry_i]
            if not np.isfinite(entry_close) or entry_close <= 0:
                continue
            flist = p.get("flags", []) or []
            has_high = flags_mod.has_high_severity(flist)
            n_flags = len(flist)
            n_high = sum(1 for f in flist if f.get("严重度") == "高")
            fd = p.get("five_dims") or {}
            rec = {
                "code": code, "period": period, "disclosure_date": disc,
                "entry_date": pd.Timestamp(entry_date), "entry_close": float(entry_close),
                "report_type": p.get("report_type"), "expert": expert, "sw": sw,
                "quality_score": p.get("quality_score"), "rating": p.get("评级"),
                "has_high_flag": bool(has_high), "n_flags": int(n_flags),
                "n_high_flags": int(n_high),
            }
            # 市场基准入场净值
            mpos = int(np.searchsorted(mkt_dates, entry_date, side="left"))
            m_entry = mkt_idx[mpos] if mpos < len(mkt_idx) else np.nan
            for H in HORIZONS:
                exit_i = entry_i + H
                if exit_i < len(kd) and np.isfinite(kc[exit_i]) and kc[exit_i] > 0:
                    fwd = kc[exit_i] / entry_close - 1.0
                    # 窗口最大回撤(入场后到出场)
                    win = kc[entry_i:exit_i + 1]
                    win = win[np.isfinite(win)]
                    if len(win) > 1:
                        peak = np.maximum.accumulate(win)
                        mdd = float(np.min(win / peak - 1.0))
                        wmin = float(np.min(win) / entry_close - 1.0)
                    else:
                        mdd = np.nan; wmin = np.nan
                    # 基准同区间收益
                    exit_date = kd[exit_i]
                    mpos2 = int(np.searchsorted(mkt_dates, exit_date, side="left"))
                    m_exit = mkt_idx[mpos2] if mpos2 < len(mkt_idx) else np.nan
                    mret = (m_exit / m_entry - 1.0) if (np.isfinite(m_entry) and np.isfinite(m_exit)) else np.nan
                    rec[f"fwd{H}"] = float(fwd)
                    rec[f"exc{H}"] = float(fwd - mret) if np.isfinite(mret) else np.nan
                    rec[f"mdd{H}"] = mdd
                    rec[f"min{H}"] = wmin
                else:
                    rec[f"fwd{H}"] = np.nan; rec[f"exc{H}"] = np.nan
                    rec[f"mdd{H}"] = np.nan; rec[f"min{H}"] = np.nan
            rows.append(rec)
        n_ok += 1
        if (ci + 1) % 40 == 0:
            print(f"  {ci+1}/{len(codes)} processed")
    panel = pd.DataFrame(rows)
    panel["disclosure_date"] = pd.to_datetime(panel["disclosure_date"])
    panel["disc_month"] = panel["disclosure_date"].dt.to_period("M").astype(str)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    panel.to_parquet(OUT, index=False)
    print(f"wrote {OUT}: {len(panel)} obs, codes with data={n_ok}")
    print("disclosure range:", panel["disclosure_date"].min(), "..", panel["disclosure_date"].max())
    print("obs with fwd20:", panel["fwd20"].notna().sum(), " fwd60:", panel["fwd60"].notna().sum())
    print("has_high_flag obs:", int(panel["has_high_flag"].sum()),
          " n_flags==0 obs:", int((panel["n_flags"] == 0).sum()))


if __name__ == "__main__":
    main()
