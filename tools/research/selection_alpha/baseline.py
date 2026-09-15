"""机械 baseline:动量代理选股(超额检验对照)。

预注册:每决策日 D,在全A(排北交所、历史充足)取 close>MA20>MA60 且 ret20>0 的动量票,
按 ret20 降序取 Top-N(N 可对齐当日我们入选规模),过同一次日执行口径 → baseline α。
这是"动量代理 × 限价回踩"机械基线,用于回答"深度选股相对它有没有真超额"。

⚠️ 测试环境研究模拟,非投资建议。只用 ≤D 信息。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def momentum_proxy_picks(universe, dates, n_per_day: dict[str, int] | int,
                         strategy: str = "mom_baseline") -> pd.DataFrame:
    """在给定决策日集合上生成动量代理选股。n_per_day: 每日取数(dict 按日或标量)。"""
    rows = []
    for d in dates:
        n = n_per_day.get(d, 0) if isinstance(n_per_day, dict) else n_per_day
        if not n:
            continue
        cands = []  # (ret20, code)
        for code, f in universe.feats.items():
            pos = universe._date_pos[code].get(d)
            if pos is None or pos + 1 >= f["n"]:
                continue
            c = f["c"][pos]; ma20 = f["ma20"][pos]; ma60 = f["ma60"][pos]; r20 = f["ret20"][pos]
            if not (np.isfinite(c) and np.isfinite(ma20) and np.isfinite(ma60) and np.isfinite(r20)):
                continue
            if c > ma20 and ma20 > ma60 and r20 > 0:
                cands.append((float(r20), code))
        cands.sort(reverse=True)
        for _, code in cands[:n]:
            rows.append((d, strategy, code))
    return pd.DataFrame(rows, columns=["date", "strategy", "code"])
