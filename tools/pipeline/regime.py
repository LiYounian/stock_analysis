"""编排层:市场状态 Market Regime(V1 模块一;2026-09-14 P0/P1 改造为 4 因子)。

数据流(读数 → 计算 → 落库):
  沪深300 指数 K线(collectors.index)              → 指数多头 + 量能
  + 全市场广度(market_forecast.breadth,最近交易日)→ 宽度(above_ma20_ratio) + 涨跌停(家数)
  → pattern_screener.regime.analyze(四因子平权 → 0–100 → 五档;描述性过热温度)
  → store.put_view("市场状态", ...)

改造(诊断 docs/计划/2026-09-14_市场状态regime诊断与优化方案.md):原「宽度」读形态选股达标占比
(历史几无数据)、「涨跌停」硬编码 None、「科技共振」龙头池空——三维形同虚设。现宽度/涨跌停统一从
breadth 取(每日可得),科技共振移除。**不接合议**(后续集成)。
入口:`python -m tools.pipeline.regime [--date YYYY-MM-DD] [--no-fetch]`。
"""
from __future__ import annotations

import logging

from tools.analysis.pattern_screener import regime
from tools.collectors import index
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.regime")

_CFG = THRESHOLDS["市场状态"]
_BENCH = "000300"        # 沪深300


def _index_df(fetch: bool):
    try:
        return index.load_index(_BENCH)
    except FileNotFoundError:
        return index.fetch_index(["沪深300"]).get(_BENCH) if fetch else None


def _breadth_signals(as_of: str | None):
    """全市场广度最近交易日(≤as_of)→ (宽度占比 above_ma20_ratio, 涨跌停{涨停,跌停})。缺→(None,None)。"""
    try:
        from tools.analysis.market_forecast import breadth as mfb
        bd = mfb.compute_breadth()
    except Exception as exc:  # noqa: BLE001
        logger.warning("广度计算失败(宽度/涨跌停降级): %s", str(exc)[:120])
        return None, None
    if bd is None or bd.empty:
        return None, None
    import pandas as pd
    if as_of is not None:
        sel = bd.loc[bd.index <= pd.Timestamp(as_of)]
        row = sel.iloc[-1] if len(sel) else bd.iloc[-1]
    else:
        row = bd.iloc[-1]
    width = float(row["above_ma20_ratio"]) if "above_ma20_ratio" in row else None
    limits = None
    if "limit_up" in row and "limit_down" in row:
        limits = {"涨停": float(row["limit_up"]), "跌停": float(row["limit_down"])}
    return width, limits


def run_regime(as_of: str | None = None, fetch: bool = True) -> dict:
    """算市场状态并落 view「市场状态」。返回结果(含情绪分/标签/因子贡献/降级)。"""
    if as_of:
        store.set_active_date(as_of)
    idx = _index_df(fetch)
    宽度占比, 涨跌停 = _breadth_signals(as_of)
    result = regime.analyze(index_df=idx, 宽度占比=宽度占比, 涨跌停=涨跌停)
    result["as_of"] = as_of
    p = store.put_view("市场状态", result)
    logger.info("市场状态:情绪分 %.1f / 标签 %s / 有效因子 %d/%d → %s",
                result["情绪分"], result["标签"], result["有效因子数"],
                result["总因子数"], p)
    for name, c in result["因子贡献"].items():
        logger.info("  因子 %s: 子分=%s 权重=%s 可用=%s(%s)",
                    name, c["子分"], c["权重"], c["可用"], c["依据"])
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse

    import pandas as pd

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="市场状态 Market Regime")
    ap.add_argument("--date", help="日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    a = ap.parse_args(argv)
    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    r = run_regime(as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:情绪分 %.1f 标签 %s(降级 %d 项)", r["情绪分"], r["标签"], len(r["降级"]))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
