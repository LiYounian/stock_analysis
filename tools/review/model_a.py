"""Model-A 统一口径事后收益（跨版/跨线唯一可比）。

**撮合复用** `tools/research/sector_news_forward._entry_labels`（限价=D 收盘、D+1 `low≤限价`成交、
成交价=`min(限价, D+1 open)`、未触发不进分母——两条语义已被 `tests/test_sector_news_forward.py`
锁死）。本模块在其上**只加 r_exit 层**：D+1 收盘为正且未破 MA5 → 持到 D+2（r_exit=r_d2）；
否则当日 D+1 离场（r_exit=r_d1）。

胜率用 r_d1（复用 `nextday_scorecard.is_close_positive`）；收益榜用 r_exit（统筹拍板主榜）。
α 用全A等权 market_ew.parquet 同期超额（复用 sector_news_forward `_load_ew`/`excess_*`）；
**源缺失 → α=null 有声缺失**（绝不顶替/编）。

防未来：收益只用 ≤到期日 master kline（无未来 bar=天然护栏）；MA5 取信号日 D 的 `形态.ma5`（PIT）。
盘中离场价用日线收盘代理（v1 不重建盘中止损成交价，避免臆造亚日内成交；见设计 §待确认）。
"""
from __future__ import annotations

import logging
import re

from tools.analysis.nextday_scorecard import is_close_positive
from tools.research import sector_news_forward as snf
from tools.review.types import ModelALabels, Pick

logger = logging.getLogger("review.model_a")

_PRICE_RE = re.compile(r"(\d+\.\d+)")


def ensure_ew(data_root: str | None, build_if_missing: bool = True) -> dict[str, float]:
    """全A等权基准 {date: ew_index}。缺 parquet 且 build_if_missing → 尝试 build_market_index 构建；
    构建不可得（无 breadth 源等）→ 返回 {}（α 列 null 有声缺失，绝不顶替/编）。"""
    ew = snf._load_ew(data_root)
    if ew:
        return ew
    if build_if_missing:
        logger.warning("market_ew.parquet 缺失 → 尝试 build_market_index 构建…")
        try:
            from tools.research.finval import build_market_index
            build_market_index.main()
            ew = snf._load_ew(data_root)
        except Exception as e:  # noqa: BLE001 - 构建不可得：保守降级
            logger.warning("build_market_index 不可得（α 列将 null 有声缺失）：%s", e)
            ew = {}
    if not ew:
        logger.warning("全A等权基准不可用 → 本轮 α（超额）列全 null 有声缺失")
    return ew


def parse_self_entry(text: str | None) -> float | None:
    """从入场自由文本解析自报回踩价（次列敏感性）。取首个小数（如"回踩MA5(19.5)限价"→19.5）。
    解析不到 → None（有声缺失）。"""
    if not text:
        return None
    m = _PRICE_RE.search(text)
    return float(m.group(1)) if m else None


def _d1_low(code: str, signal_date: str, cache: dict) -> float | None:
    """D+1 最低价（用于判盘中破 MA5）。缺票/未到期 → None。"""
    rows, d2i = snf._kline(code, cache)
    if rows is None:
        return None
    i = d2i.get(signal_date)
    if i is None or i + 1 >= len(rows):
        return None
    return rows[i + 1][2]     # (date, open, low, close) → low


def compute_labels(pick: Pick, cache: dict, ew: dict) -> ModelALabels:
    """单 Pick → Model-A 事后收益标签。cache/ew 跨 pick 复用（KlineBook 式缓存）。"""
    base = snf._entry_labels(pick.code, pick.date, cache, ew)   # 撮合口径复用（已被测试锁死）
    entry, labels = base["entry"], base["labels"]

    lab = ModelALabels(
        filled=entry["filled"],
        limit=entry["limit"],
        entry_price=entry["price"],
        r_d1=labels.get("r_d1"),
        r_d2=labels.get("r_d2"),
        alpha_d1=labels.get("excess_d1"),
        note=entry.get("note"),
        status=snf._rec_status(base),
        p_entry_self=parse_self_entry(pick.入场_text),
    )
    lab.untriggered = (entry["filled"] is False)
    lab.close_positive = is_close_positive(lab.r_d1)     # None=未触发/未到期（剔出胜率分母）

    # —— 踏空候选：未触发且 D+1 收盘 > 限价（票高开冲走、回踩没等到）——
    if lab.filled is False and isinstance(lab.limit, (int, float)):
        rows, d2i = snf._kline(pick.code, cache)
        if rows is not None:
            i = d2i.get(pick.date)
            if i is not None and i + 1 < len(rows):
                lab.runaway_up = rows[i + 1][3] > lab.limit   # D+1 close > 限价

    # —— 盘中破 MA5（形态.ma5 为信号日 D PIT 值）——
    ma5 = pick.形态.get("ma5")
    d1_low = _d1_low(pick.code, pick.date, cache)
    if lab.filled is True and isinstance(ma5, (int, float)) and d1_low is not None:
        lab.stop_flag = d1_low < ma5

    # —— r_exit：D+1 收盘为正且未破 MA5 → 持到 D+2；否则当日 D+1 离场 ——
    if lab.filled is True:
        hold = (lab.r_d1 is not None and lab.r_d1 > 0) and not lab.stop_flag
        lab.hold_to_d2 = hold
        if hold:
            lab.exit_horizon = "d2"
            lab.r_exit = lab.r_d2                    # D+2 未到期 → None（pending）
            lab.alpha_exit = labels.get("excess_d2")
        else:
            lab.exit_horizon = "d1"
            lab.r_exit = lab.r_d1                    # 当日离场（含破 MA5/收盘为负），日线收盘代理
            lab.alpha_exit = labels.get("excess_d1")
    return lab


def compute_all(picks: list[Pick], data_root: str | None = None,
                build_if_missing: bool = True) -> list[tuple[Pick, ModelALabels]]:
    """批量：共享 kline 缓存 + 一次性 ew。返回 [(pick, labels)]。"""
    try:                                   # market.load_kline 需主档数据根（worktree data/ 为空）
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)
    except Exception as e:  # noqa: BLE001
        logger.warning("ensure_data_root 失败（kline 可能取不到、收益列将 pending）：%s", e)
    cache: dict = {}
    ew = ensure_ew(data_root, build_if_missing=build_if_missing)
    return [(p, compute_labels(p, cache, ew)) for p in picks]
