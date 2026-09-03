"""全 A 股票池(universe)采集:V1 全市场扫描的票池来源。

V1 定稿:覆盖全 A 5000+,**不覆盖北交所**。数据源当前用 **akshare**(Tushare 以后再切,
接口保持 `fetch_universe()` 不变、届时换实现即可)。
需求见 docs/计划/V1_形态选股与市场状态系统.md F0/F2.6。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("collectors.universe")


def _is_bj(code: str) -> bool:
    """北交所代码(8/4 开头)——V1 不覆盖。

    ⚠️ **已知缺口(2026-09-03,有意未修)**:只认 8/4 历史段,漏掉北交所**现行** 920 段
    → `exclude_bj=True` 时排不掉 920 段(主档 333 只)。改了就是票池口径变更(全A票池
    规模会变),故本轮不动,另开任务拍板。届时应改成委托 `tools.config.exchange.is_bj`
    (那里 920/43/83/87 全段判对)。判据为什么必须只有一份,见该模块 docstring。
    """
    return code[:1] in ("8", "4")


def fetch_universe(limit: int | None = None, exclude_bj: bool = True) -> list[dict]:
    """全 A 股票池 [{code, name}, ...]。exclude_bj 排除北交所(V1 不覆盖)。

    limit:仅取前 N 只(开发/联调用,避免每次全量);None=全量。
    akshare 源;失败抛错(不静默返回空)。
    """
    import akshare as ak
    import pandas as pd
    pd.set_option("future.infer_string", False)
    df = ak.stock_info_a_code_name()
    if df is None or len(df) == 0:
        raise ConnectionError("akshare 全A清单为空")
    out = [{"code": str(r["code"]), "name": str(r["name"])} for _, r in df.iterrows()]
    if exclude_bj:
        out = [x for x in out if not _is_bj(x["code"])]
    if limit:
        out = out[:limit]
    logger.info("全A票池:%d 只(排除北交所=%s)", len(out), exclude_bj)
    return out


def universe_codes(limit: int | None = None, exclude_bj: bool = True) -> list[str]:
    """只要代码列表。"""
    return [x["code"] for x in fetch_universe(limit=limit, exclude_bj=exclude_bj)]
