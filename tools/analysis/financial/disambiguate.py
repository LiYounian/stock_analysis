"""行业归属消歧层(财报路由地基)。

背景(线 fin-disambig):个股的「行业归属」在真实市场里是**时变 / 模糊 / 市场定义≠正式分类**的:
  - 时变:借壳 / 转型后正式分类会变,回测按「当时」而非「现在」取行业(去前视偏差);
  - 模糊:主业跨多个申万一级(硬件 vs 软件定性不清),证监会门类与细分主业不一致;
  - 市场定义≠正式分类:市场把某票当某「概念 / 板块」交易(如把某锂电材料票当锂矿随锂价波动),
    这跟它的正式申万一级可以完全不同。

本模块对 (code, as_of) 汇聚三种口径并给出**消歧结论**,供 analyzer 路由决定「按哪个/哪些行业口径看财报」。
**只做标注与候选聚合,不做多视角分析本身**(那是后续线的事)。

三种口径来源(诚实标注 provenance):
  1) **时变正式行业**(formal):`collectors.industry_history.industry_at(code, as_of)` 取当时证监会门类,
     再经 `industry_map.to_sw` 对齐到申万一级。**严格按 as_of,绝不用最新行业回填历史**(防未来函数)。
     无历史记录时**不静默用现状顶替**:formal 留空,另给 board 现状(单独标注,含前视风险)。
  2) **市场定义 / 概念标注**(market):仓库目前**无独立的概念板块成分 / 资金流板块归属数据**
     (fundflow 采集是个股级净额,非板块成分;board_membership 是申万/证监会正式分类,非"市场概念")。
     现阶段唯一可用的「市场当它是什么」信号是自选池人工维护的 `sector`(大类板块,如"半导体/机器人/光模块"),
     它带人的市场认知、比正式分类更贴近"市场当它是X交易"。**据实使用并标注 provenance 为人工分类**;
     池外票 / 无 sector → **诚实降级**为"市场概念数据缺,待补",绝不硬编。
  3) **细分主业口径**(meta):自选池人工填的 `industry`(细分行业自由文本),经 `to_sw` 对齐。

模糊判定:上述口径映射到申万一级后**指向 ≥2 个不同行业** → `ambiguous=True` + 候选行业列表。

依赖方向:import industry_map(纯) / industry_history(采集读) / stock_pool / board;**不 import analyzer**
(analyzer 反向 import 本模块),避免环。
"""
from __future__ import annotations

import logging

from tools.analysis import industry_map

logger = logging.getLogger("analysis.financial.disambiguate")


def _safe(fn):
    try:
        return fn()
    except Exception:                                       # noqa: BLE001
        return None


def _formal_sw_at(code: str, as_of: str | None) -> tuple[str | None, str | None]:
    """时变正式行业(申万一级)+ 证监会原名。严格按 as_of,无则 None(不回填现状)。

    Returns: (申万一级 or None, 证监会原名 or None)。
    """
    if as_of is None:
        return None, None                                   # 无锚点不谈"当时",交由 board 现状口径
    from tools.collectors import industry_history as ih
    raw = _safe(lambda: ih.industry_at(code, as_of))        # industry_at 内部已按 date<=as_of 过滤
    if not raw:
        return None, None
    return industry_map.to_sw(raw), raw


def _board_sw_now(code: str) -> str | None:
    """现状口径(board_membership,申万/证监会正式分类)→ 申万一级。**含前视风险**,仅作 formal 缺失时的兜底口径。"""
    from tools.collectors import board
    return industry_map.to_sw(_safe(lambda: board.board_of(code)) or "")


def _pool_fields(code: str, industry: str | None, sector: str | None) -> tuple[str | None, str | None]:
    """取(细分行业 industry, 市场大类 sector)。显式传入优先;缺则回退自选池。池外 → (None,None)。"""
    if industry is not None or sector is not None:
        return industry, sector
    from tools.config import stock_pool
    s = _safe(lambda: stock_pool.get(code))
    if s is None:
        return None, None
    return s.industry, s.sector


def disambiguate(code: str, as_of: str | None = None,
                 industry: str | None = None, sector: str | None = None) -> dict:
    """对 (code, as_of) 汇聚三口径并给消歧结论。

    Args:
        code: 6 位代码。
        as_of: 可见性锚;时变正式行业严格按此取(None=不谈"当时",走现状口径)。
        industry: 细分行业自由文本(record.meta.industry);None 时回退自选池 industry。
        sector: 市场大类板块(record.meta.sector);None 时回退自选池 sector。
    Returns dict:
        {
          code, as_of,
          formal_sw:  时变正式申万一级(严格 as_of;无历史→None),
          formal_raw: 时变正式证监会原名,
          board_sw:   现状 board 申万一级(formal 缺失兜底;含前视风险),
          market_sw:  市场概念对齐的申万一级(来自 sector),
          market_label: "市场把它当「X」板块交易" or None,
          market_source: 市场概念数据来源标注(人工分类 / 缺待补),
          meta_sw:    细分主业申万一级(来自 industry),
          primary:    主行业申万一级(向后兼容:meta 优先,回退 formal/board),
          candidates: 去重后的候选申万一级列表(供多路由),
          ambiguous:  bool,三口径指向 ≥2 个不同申万一级,
          ambiguity_reason: str or None,
          notes:      list[str] 诚实降级 / 前视风险等标注,
        }
    """
    code = str(code).zfill(6)
    notes: list[str] = []

    industry, sector = _pool_fields(code, industry, sector)

    # 1) 时变正式行业(严格 as_of;防未来函数)
    formal_sw, formal_raw = _formal_sw_at(code, as_of)
    board_sw = None
    if formal_sw is None:
        board_sw = _board_sw_now(code)
        if as_of is not None and board_sw is not None:
            notes.append("时变正式行业无 as_of 当时记录,已回退 board 现状口径(含前视风险,仅参考)")

    # 2) 市场定义 / 概念标注
    market_sw = industry_map.to_sw(sector) if sector else None
    if sector:
        market_label = f"市场把它当「{sector}」板块交易"
        market_source = "stock_pool.sector(人工大类分类,非独立概念/资金流板块成分)"
    else:
        market_label = None
        market_source = "缺(仓库无概念板块/资金流板块成分数据,待补)"
        notes.append("市场概念数据缺,已诚实降级(未硬编)")

    # 3) 细分主业口径
    meta_sw = industry_map.to_sw(industry) if industry else None

    # 主行业(向后兼容 analyzer._industry_key:meta 优先,回退现状 board)
    primary = meta_sw or formal_sw or board_sw

    # 候选:主行业 + 时变正式 + 市场概念 +(formal 缺时)board 现状,按序去重、去 None
    candidates: list[str] = []
    for c in (primary, formal_sw, market_sw, board_sw):
        if c and c not in candidates:
            candidates.append(c)

    # 模糊:三口径(meta / 时变正式或其兜底 / 市场)映射到 ≥2 个不同申万一级
    formal_effective = formal_sw or board_sw
    distinct = {c for c in (meta_sw, formal_effective, market_sw) if c}
    ambiguous = len(distinct) >= 2
    ambiguity_reason = None
    if ambiguous:
        parts = []
        if meta_sw:
            parts.append(f"细分主业口径={meta_sw}")
        if formal_effective:
            tag = "时变正式" if formal_sw else "现状"
            parts.append(f"{tag}分类={formal_effective}")
        if market_sw:
            parts.append(f"市场概念={market_sw}")
        ambiguity_reason = "口径分歧:" + " / ".join(parts)

    return {
        "code": code,
        "as_of": as_of,
        "formal_sw": formal_sw,
        "formal_raw": formal_raw,
        "board_sw": board_sw,
        "market_sw": market_sw,
        "market_label": market_label,
        "market_source": market_source,
        "meta_sw": meta_sw,
        "primary": primary,
        "candidates": candidates,
        "ambiguous": ambiguous,
        "ambiguity_reason": ambiguity_reason,
        "notes": notes,
    }
