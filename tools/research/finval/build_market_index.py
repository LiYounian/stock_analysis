"""构建等权全A市场基准(EW all-A index),供行业财报信号回测算超额收益。

方法:对 data/master/kline 下全部个股,按日取 pct_chg 的横截面均值(等权),
累乘成净值指数。存 data/analysis/backtest/finval/market_ew.parquet(date, ew_index, n)。

红线无关(纯价格聚合);仅作基准,不含未来函数问题(基准是同期市场,回测里用
入场日→出场日的基准区间收益做超额)。
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
KLINE_DIR = os.path.join(ROOT, "data/master/kline")
OUT = os.path.join(ROOT, "data/analysis/backtest/finval/market_ew.parquet")


def main() -> None:
    files = sorted(glob.glob(os.path.join(KLINE_DIR, "*.parquet")))
    print(f"kline files: {len(files)}")
    # 逐票收集 (date, pct_chg),避免一次性巨表:分块聚合到 date -> [sum, count]
    frames = []
    for i, f in enumerate(files):
        try:
            df = pd.read_parquet(f, columns=["date", "pct_chg"])
        except Exception:
            continue
        df = df.dropna(subset=["pct_chg"])
        if df.empty:
            continue
        frames.append(df)
        if (i + 1) % 1000 == 0:
            print(f"  read {i+1}/{len(files)}")
    big = pd.concat(frames, ignore_index=True)
    print(f"total rows: {len(big)}")
    # 极端剔除:pct_chg 落在 [-30, 30] 之外视为数据异常(A股单日涨跌幅有限),截断防污染均值
    big = big[(big["pct_chg"] >= -30) & (big["pct_chg"] <= 30)]
    g = big.groupby("date")["pct_chg"].agg(["mean", "count"]).reset_index()
    g = g.sort_values("date").reset_index(drop=True)
    g["ret"] = g["mean"] / 100.0
    g["ew_index"] = (1.0 + g["ret"]).cumprod()
    out = g[["date", "ew_index", "count"]].rename(columns={"count": "n"})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out.to_parquet(OUT, index=False)
    print(f"wrote {OUT}: {len(out)} days, {out['date'].min()} .. {out['date'].max()}")
    print(out.head(3).to_string())
    print(out.tail(3).to_string())


if __name__ == "__main__":
    main()
