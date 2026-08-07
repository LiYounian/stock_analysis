"""北向资金采集:个股北向净流入趋势(多因子的"资金流"因子 D8)。

用途:多因子 score 的资金流维度 = 个股近 N 日(5–10)北向净流入趋势。
现状(本机实测):东财北向**个股明细**接口签名/返回不稳定(见 docs/参考 §四"需实测确认"),
按 I4 **降级为"缺失"其余因子照算**——`trend()` 取不到即返 None,不抛、不阻断截面打分。
接口一旦可用(或改 Tushare),只需在 `_fetch_individual` 里补实现,上层零改动。

依赖方向:采集层。失败静默降级(返回 None),由多因子 score 按缺失处理。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("collectors.northbound")


def _fetch_individual(code: str, win: int):
    """尝试取单票近 win 日北向持股/净流入序列 → 净流入趋势(斜率符号或增量)。

    本机东财个股明细接口当前不可用 → 抛出让上层降级。可用时在此补实现。
    """
    import akshare as ak  # noqa: F401  (占位:接口可用后在此调用并解析)
    raise NotImplementedError("北向个股明细本机不可用,按 I4 降级缺失")


def trend(code: str, win: int = 10) -> float | None:
    """个股近 win 日北向净流入趋势;取不到→None(I4 降级缺失,不抛)。"""
    try:
        return _fetch_individual(code, win)
    except Exception as e:                       # 含 NotImplementedError / 网络墙
        logger.debug("北向 %s 趋势不可得(降级缺失): %s", code, type(e).__name__)
        return None


def trend_map(codes: list[str], win: int = 10) -> dict[str, float]:
    """批量北向趋势 {code: 趋势};全不可得则返回空 dict(多因子资金流维度整体缺失)。"""
    out = {}
    for c in codes:
        t = trend(c, win)
        if t is not None:
            out[c] = t
    if not out:
        logger.info("北向个股趋势整体不可得,多因子资金流维度降级缺失(I4)")
    return out
