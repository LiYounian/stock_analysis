"""板块定向提示(P3 双跑的 B 组增强源)——把 sector_focus + 角色表 编成 {code: 板块定向}。

**非侵入**:不改选股 screen() 代码;本模块只产出**每只候选的板块定向 tag + 加/降分**,
供双跑 B 组对候选池旁路重排(A=原量价排序,B=量价+板块定向)。

板块定向加分(预注册·写死;默认**只加权重点、只降权规避**,不硬否——对齐用户/统筹红线):
  · 候选在**规避板块池** → −PENALTY(坏消息叠拥挤/过热,回撤空间大)。
  · 候选在**重点板块池**:base = focus_score 缩放;再按角色乘子——
      补涨先锋/中军(接力/主阵地,防踏空核心)> 龙头 > 弹性 > 无角色。
  · 其余 → 0(不动)。
角色来自 data/sector_roster/<板块>.json(候选 code 反查落在哪个角色的主/备选)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.sector_hint")

HINT_VERSION = "v1-2026-09-16"

# 加分表(0~100 量价分同量级;预注册)
FOCUS_BASE = 6.0            # 在重点池的底分
ROLE_MULT = {"补涨先锋": 2.0, "中军": 1.7, "龙头": 1.3, "弹性股": 1.0, None: 0.6}
AVOID_PENALTY = 15.0       # 规避池降分


def _latest_focus_path(date: str) -> Optional[Path]:
    """≤date 最近一份 sector_focus.json(生产语义:午盘用最新可得的昨日 EOD focus)。"""
    import glob
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    cands = []
    for base in (settings.PROJECT_ROOT, _MAIN):
        for p in glob.glob(str(Path(base) / "data" / "analysis" / "*" / "sector_focus.json")):
            d = Path(p).parent.name
            if d <= date:
                cands.append((d, p))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return Path(cands[-1][1])


def _load_focus(date: str) -> Optional[dict]:
    p = _latest_focus_path(date)
    if not p:
        return None
    try:
        obj = json.loads(p.read_text(encoding="utf-8"))
        obj["_focus_date"] = p.parent.name
        return obj
    except Exception:
        return None


def _load_roster(sw: str) -> Optional[dict]:
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "sector_roster" / f"{sw}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def _role_of(code: str, roster: dict) -> tuple[Optional[str], Optional[str]]:
    """候选 code 在该板块角色表里的 (角色, 选级);未命中 → (None, None)。"""
    for role, lst in (roster or {}).get("roles", {}).items():
        for it in lst:
            if it.get("code") == code:
                return role, it.get("选级")
    return None, None


def build_sector_hint(date: str, codes: list[str], *, focus: Optional[dict] = None,
                      membership: Optional[dict] = None) -> dict[str, dict]:
    """{code: {板块, in_focus, focus_score, in_avoid, 角色, 选级, bonus, 依据}}(只覆盖有归属的)。

    focus/membership 可传入(回测批量复用,不落盘不重载);缺省从磁盘取 ≤date 最近 focus。
    """
    from tools.analysis.sector_forecast.universe import _membership
    if focus is None:
        focus = _load_focus(date)
    if not focus:
        logger.warning("无 sector_focus.json(%s),板块定向为空", date)
        return {}
    mem = membership if membership is not None else _membership()
    focus_sw = {r["板块"]: r for r in focus.get("重点板块池", [])}
    avoid_sw = {r["板块"] for r in focus.get("规避板块池", [])}
    roster_cache: dict[str, Optional[dict]] = {}

    out: dict[str, dict] = {}
    for code in codes:
        sw = mem.get(code)
        if not sw:
            continue
        rec = {"板块": sw, "in_focus": False, "focus_score": None,
               "in_avoid": sw in avoid_sw, "角色": None, "选级": None, "bonus": 0.0}
        if sw in avoid_sw:
            rec["bonus"] = -AVOID_PENALTY
            rec["依据"] = f"{sw} 在规避池(新闻净利空叠拥挤/过热)→ 降权"
        elif sw in focus_sw:
            fr = focus_sw[sw]
            if sw not in roster_cache:
                roster_cache[sw] = _load_roster(sw)
            role, tier = _role_of(code, roster_cache[sw])
            fscore = fr.get("focus_score") or 0.0
            mult = ROLE_MULT.get(role, ROLE_MULT[None])
            tier_mult = 1.0 if tier == "主选" else (0.7 if tier == "备选" else 0.5)
            bonus = FOCUS_BASE * (0.5 + fscore) * mult * tier_mult
            rec.update({"in_focus": True, "focus_score": round(fscore, 4),
                        "角色": role, "选级": tier, "bonus": round(bonus, 2),
                        "依据": f"{sw} 重点池(focus {fscore:.2f})"
                                + (f"·{role}{tier}" if role else "·无明确角色") + " → 加权"})
        out[code] = rec
    return out
