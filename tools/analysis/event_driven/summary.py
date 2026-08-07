"""汇成"事件驱动专家输入":某票某日的净事件方向/强度/置信度。

数据优先级(D:数值优先、缺则降级):
  1) **精数值**(采集缓存 yjyg/yjkb/ggcg):业绩超预期用增速判、增减持用规模判 → 强信号、置信度高。
  2) **公告粗判**(record['events'] 里已分类的 业绩预告/快报/增持/减持/回购):只有方向、无数值 → 降级。
  3) 两者皆无 → None(专家弃权)。

实时专家强度只用**事件属性**(超预期幅度、规模),绝不用未来收益(防未来函数;前瞻收益验证在
tools/backtest/event_study.py)。阈值真源 THRESHOLDS["事件驱动"] + 置信度映射 THRESHOLDS["合议"]。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.event_driven import judge
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.event_driven.summary")

_C = THRESHOLDS["事件驱动"]
_SUFF = THRESHOLDS["合议"]["置信度映射"]["数据充分度"]

# 公告 fallback 关注的事件驱动类型
_EARNINGS_TYPES = ("业绩预告", "业绩快报")
_ACTION_TYPES = ("增持", "回购", "减持")


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def _within(days_diff: int, window: int) -> bool:
    return 0 <= days_diff <= window


def _quarter_ends_before(as_of: pd.Timestamp, back_days: int) -> list[str]:
    """as_of 往前 back_days 内的季度末报告期 "YYYYMMDD" 列表。"""
    ends = []
    for y in (as_of.year, as_of.year - 1):
        for md in ("0331", "0630", "0930", "1231"):
            d = pd.to_datetime(f"{y}{md}")
            if 0 <= (as_of - d).days <= back_days:
                ends.append(f"{y}{md}")
    return ends


def _load_precise(code: str, as_of: pd.Timestamp) -> list[dict]:
    """从采集缓存取该票近期精数值事件 → 打分事件列表。无缓存/降级 → []。"""
    from tools.collectors import event_driven as col
    events: list[dict] = []
    try:
        for period in _quarter_ends_before(as_of, _C["漂移窗_天"] + 45):
            for kind in ("yjyg", "yjkb"):
                df = col.load_earnings(period, kind)
                if df is None or df.empty or "code" not in df.columns:
                    continue
                hit = df[df["code"] == code]
                for _, r in hit.iterrows():
                    v = judge.judge_pead(r.get("增速"))
                    events.append({"来源": f"{kind}:{period}", "方向": v["方向"],
                                   "强度": v["超预期度"], "达显著线": v["达显著线"],
                                   "依据": v["依据"], "类别": "业绩"})
    except Exception as e:                       # noqa: BLE001
        logger.debug("精数值业绩事件加载降级: %s", e)
    try:
        df = col.load_insider_trades("latest")
        if df is not None and not df.empty and "code" in df.columns:
            hit = df[df["code"] == code]
            for _, r in hit.iterrows():
                atype = r.get("方向")
                if atype in ("增持", "减持"):
                    v = judge.judge_corporate_action(atype, None)
                    events.append({"来源": "ggcg", "方向": v["方向"], "强度": v["强度"],
                                   "达显著线": not v["象征性"], "依据": v["依据"], "类别": "公司行为"})
    except Exception as e:                       # noqa: BLE001
        logger.debug("精数值增减持加载降级: %s", e)
    return events


def _coarse_from_announcements(announcements, as_of: pd.Timestamp) -> list[dict]:
    """公告粗判(无数值):按类型/impact/关键字给方向,强度固定较低,置信度降级。"""
    if not announcements:
        return []
    good = _C["利好关键词"]
    bad = _C["利空关键词"]
    events: list[dict] = []
    for a in announcements:
        atype = a.get("type") or ""
        title = a.get("title") or ""
        is_earn = atype in _EARNINGS_TYPES
        is_act = atype in _ACTION_TYPES
        if not (is_earn or is_act):
            continue
        # 时间窗
        window = _C["漂移窗_天"] if is_earn else _C["公司行为窗_天"]
        try:
            dd = (as_of - pd.to_datetime(a.get("date"))).days
        except Exception:                        # noqa: BLE001
            dd = 0
        if not _within(dd, window):
            continue
        # 方向:impact 优先,其次关键字
        impact = a.get("impact")
        if impact == "利好" or any(k in (atype + title) for k in good):
            方向, s = "看多", 0.4
        elif impact == "利空" or any(k in (atype + title) for k in bad):
            方向, s = "看空", -0.4
        else:
            方向, s = "中性", 0.0
        events.append({"来源": "公告", "方向": 方向, "强度": s, "达显著线": False,
                       "依据": f"{atype}·{title[:16]}", "类别": "业绩" if is_earn else "公司行为"})
    return events


def summarize(code: str, as_of, announcements=None) -> dict | None:
    """汇成事件驱动专家输入。无任何事件 → None(弃权)。

    Returns:
        {方向, 强度[-1,1], 数据充分度, 置信度[0,1], 依据[list], 原始} 或 None。
    """
    try:
        t = pd.to_datetime(as_of)
    except Exception:                            # noqa: BLE001
        return None

    precise = _load_precise(code, t)
    used_precise = bool(precise)
    events = precise if used_precise else _coarse_from_announcements(announcements, t)
    if not events:
        return None

    net = _clamp(sum(e["强度"] for e in events))
    if net > 0.05:
        方向 = "看多"; 强度 = abs(net)
    elif net < -0.05:
        方向 = "看空"; 强度 = -abs(net)
    else:
        方向, 强度 = "中性", 0.0

    # 数据充分度:精数值且有事件达显著线 → 充分;精数值不显著 或 公告粗判 → 部分降级
    significant = any(e.get("达显著线") for e in events)
    充分度 = "充分" if (used_precise and significant) else "部分降级"
    置信度 = _SUFF[充分度]

    依据 = [e["依据"] for e in events][:6]
    return {"方向": 方向, "强度": round(强度, 6), "数据充分度": 充分度,
            "置信度": 置信度, "依据": 依据,
            "原始": {"事件数": len(events), "净分": round(net, 6),
                    "数据源": "精数值" if used_precise else "公告粗判", "达显著线": significant}}
