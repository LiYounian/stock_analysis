"""编排层:市场状态 Market Regime(V1 模块一)。

数据流(读数 → 计算 → 落库):
  沪深300 指数 K线(collectors.index)         → 指数多头 + 量能
  + 模块二「形态选股」view 的达标占比(store 只读) → 宽度
  + 核心龙头池当日涨跌(config + collectors.market)→ 科技共振(池空则降级)
  + 涨跌停家数(暂无宽度采集 → None,降级)         → 涨跌停
  → pattern_screener.regime.analyze(五因子平权 → 0–100 → 五档)
  → store.put_view("市场状态", ...)

运行时耦合(#18):模块二先跑落达标占比,模块一后跑读之(双方不互相 import,只经 store view)。
本轮只产"市场状态标签+分"落 view,**不接合议**(后续集成)。
入口:`python -m tools.pipeline.regime [--date YYYY-MM-DD] [--no-fetch]`。
"""
from __future__ import annotations

import logging

from tools.analysis.pattern_screener import regime
from tools.collectors import index, market
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


def _breadth() -> float | None:
    """读模块二「形态选股」view 的达标占比(宽度信号)。缺 → None。"""
    try:
        return store.get_view("形态选股").get("达标占比")
    except FileNotFoundError:
        return None


def _leader_pcts(fetch: bool) -> list | None:
    """核心龙头池当日涨跌幅列表(config 名单)。池空 → None(科技共振降级)。"""
    pool = _CFG.get("核心龙头池") or []
    if not pool:
        return None
    pcts = []
    for code in pool:
        try:
            kdf = market.load_kline_recent(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is not None and len(kdf) and "pct_chg" in kdf.columns:
            v = kdf["pct_chg"].iloc[-1]
            if v == v:                       # 非 NaN
                pcts.append(float(v))
    return pcts or None


def run_regime(as_of: str | None = None, fetch: bool = True) -> dict:
    """算市场状态并落 view「市场状态」。返回结果(含情绪分/标签/因子贡献/降级)。"""
    if as_of:
        store.set_active_date(as_of)
    idx = _index_df(fetch)
    达标占比 = _breadth()
    leaders = _leader_pcts(fetch)
    result = regime.analyze(index_df=idx, 达标占比=达标占比, leader_pcts=leaders,
                            涨跌停=None)          # 涨跌停家数暂无宽度采集 → 降级
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
