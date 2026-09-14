"""一次扫全 master/kline → 回测所需的 date×code 宽面板(缓存到隔离目录)。

产出面板(index=date, columns=code):
  close / open / ma3 / ma5 / vol_ratio / prev_high  (float)
  oversold                                          (bool,H1 选股)
均由 indicators.compute_code_indicators 逐票算好后拼装(复用 technical 算子)。
只**读** master/kline;缓存写入隔离目录(worktree data/backtest_local),绝不污染主仓。
"""
from __future__ import annotations

import os
import pandas as pd

from tools.analysis.market_forecast.dataroot import ensure_data_root
from tools.store import repo as store
from tools.backtest.capitulation.indicators import compute_code_indicators

PANEL_FIELDS = ["close", "open", "ma3", "ma5", "vol_ratio", "prev_high", "oversold"]


def build_panels(cache_dir: str, data_root=None, force: bool = False,
                 codes=None) -> dict[str, pd.DataFrame]:
    """构建/加载所有面板。cache_dir 下每字段一个 parquet。"""
    os.makedirs(cache_dir, exist_ok=True)
    paths = {f: os.path.join(cache_dir, f"panel_{f}.parquet") for f in PANEL_FIELDS}
    if not force and all(os.path.exists(p) for p in paths.values()):
        return {f: pd.read_parquet(p) for f, p in paths.items()}

    ensure_data_root(str(data_root) if data_root else None)
    codes = codes or store.list_master_codes()
    cols = {f: {} for f in PANEL_FIELDS}
    n_used = 0
    for code in codes:
        try:
            kl = store.get_master_kline(code)
        except Exception:
            continue
        ind = compute_code_indicators(kl)
        if ind is None:
            continue
        for f in PANEL_FIELDS:
            cols[f][code] = ind[f]
        n_used += 1

    panels = {}
    for f in PANEL_FIELDS:
        df = pd.DataFrame(cols[f]).sort_index()
        if f == "oversold":
            df = df.fillna(False).astype(bool)
        df.index.name = "date"
        df.to_parquet(paths[f])
        panels[f] = df
    print(f"[panels] 用票 {n_used} · 交易日 {len(panels['close'])} · "
          f"{panels['close'].index.min().date()}→{panels['close'].index.max().date()}",
          flush=True)
    return panels
