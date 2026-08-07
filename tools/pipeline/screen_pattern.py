"""编排层:形态选股(模块二)全市场/子集扫描。

数据流(采集/读数 → 策略 → 落库):
  沪深300 指数 K线(collectors.index)= RS 基准
  + 各票日 K线(collectors.market)
  → 每票 RS「个股 vs 沪深300」(当前单层降级;成分源可用后切双层,见 config RS.启用板块层)
  → pattern_screener.screen.is_qualified(硬规则 AND)
  → market_breadth(全市场达标占比)
  → store.put_view("形态选股", ...)

诚实性(本轮降级项,均在产出 view 的「降级」字段声明,不静默):
  · RS 单层:个股→板块成分映射本机无源,板块层关闭。
  · 护栏:PE分位/净利增速/公告未接入,传 None → 负向护栏不生效(guardrail 缺数据不误杀)。

采集层只取数、策略层只算、本层只编排;三层解耦。
"""
from __future__ import annotations

import logging

from tools.analysis.pattern_screener import rs, screen as ps
from tools.collectors import index, market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_pattern")

_CFG = THRESHOLDS["形态选股"]
_BENCH = "000300"        # 沪深300


def _load_or_fetch_kline(code: str, fetch: bool):
    """读本地 K线;缺失且 fetch=True 时采集。返回 df 或 None。"""
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def _benchmark_close(fetch: bool, win: int) -> list[float]:
    """沪深300 收盘序列(RS 基准)。不足/取不到抛 RuntimeError。"""
    try:
        bdf = index.load_index(_BENCH)
    except FileNotFoundError:
        bdf = index.fetch_index(["沪深300"]).get(_BENCH) if fetch else None
    if bdf is None or len(bdf) < win + 1:
        raise RuntimeError("沪深300 基准 K线不足,无法算 RS(先采集指数)")
    return bdf["close"].tolist()


def run_pattern_screen(codes: list[str], as_of: str | None = None,
                       fetch: bool = True) -> dict:
    """扫描 codes,落 view「形态选股」。返回 summary(含达标池 + 达标占比 + 降级声明)。

    fetch=True:缺 K线/基准自动采集;False:只读本地缓存(离线复算,不触网)。
    """
    if as_of:
        store.set_active_date(as_of)
    win = int(_CFG["RS"]["窗口"])
    bench_close = _benchmark_close(fetch, win)

    results: dict[str, dict] = {}
    skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < win + 1:
            skipped += 1
            logger.warning("%s K线不足(<%d),跳过", code, win + 1)
            continue
        rs_val = rs.compute(kdf["close"].tolist(), bench_close, win)   # 个股 vs 沪深300
        results[code] = ps.is_qualified(kdf, rs_stock_vs_board=rs_val,
                                        rs_board_vs_hs300=None)         # 单层:板块层降级
    breadth = ps.market_breadth(results)
    view = {
        "as_of": as_of,
        "扫描数": len(codes), "跳过数": skipped,
        "有效样本": breadth["有效样本"], "达标数": breadth["达标数"],
        "达标占比": breadth["达标占比"],
        "达标清单": [{"code": c, "命中形态": results[c]["命中形态"]}
                     for c in breadth["达标清单"]],
        "降级": {
            "RS": "单层(个股 vs 沪深300);个股→板块成分源不可用,板块层关闭",
            "护栏": "PE分位/净利增速/公告未接入,负向护栏本轮不生效",
        },
    }
    p = store.put_view("形态选股", view)
    logger.info("形态选股:扫描 %d / 有效 %d / 达标 %d(占比 %.2f%%)→ %s",
                len(codes), breadth["有效样本"], breadth["达标数"],
                breadth["达标占比"] * 100, p)
    return view
