"""消息驱动 一出 M2 —— 板块消息标签 + 龙头/跟涨候选 → 写进 sector_focus.json「消息驱动」块。

**接口契约**:对齐统筹《选股侧消费板块利好标签_接口设计》(main 007cca5)。决定:**扩进
sector_focus.json**(选股已读它、少一个源),顶层加 `消息驱动` 块:
  {as_of, 利好板块:[{board, tag, strength, 依据,
                    龙头候选:[{code,name,催化,已动}],
                    跟涨候选:[{code,name,联动依据}]}]}
每候选必带 code + 催化/联动依据(无依据不进)。

进池口径(与 P3 证伪的量价机械接法划界):
  · 龙头候选来源 = **消息催化(α源)**,进候选池带"消息催化"标签,仍走逐票研判+回踩限价(不改打分)。
  · 跟涨候选 = 同板块、龙头已动、后排待接力(联动),进"联动观察"档、不抢名额、forward 单列验。
防偷看:阈值预注册写死(news_catalyst.board_leader_catalyst 的 HI + 本模块 MOVED_PCT),不对个例调。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.news_focus_block")

BLOCK_VERSION = "v1-2026-09-16"
MOVED_PCT = 3.0          # 龙头"已动"阈值:当日涨幅 ≥ 此(预注册写死)


def _leader_moved(code: str, date: str) -> Optional[bool]:
    """龙头当日是否已动(涨幅≥MOVED_PCT)。读 master kline 当日行;缺→None。"""
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()
    from tools.store import repo as store
    try:
        df = store.get_master_kline(code)
    except Exception:
        return None
    d = df[df["date"].astype(str).str.slice(0, 10) == date]
    if d.empty:
        return None
    try:
        return float(d.iloc[-1]["pct_chg"]) >= MOVED_PCT
    except Exception:
        return None


def _followers(sw: str, date: str, *, top: int = 3) -> list[dict]:
    """跟涨候选 = 该板块 roster 的补涨先锋(同板块、低位待接力)。联动依据带板块+龙头已动语境。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    roster = None
    for base in (settings.PROJECT_ROOT, _MAIN):
        p = Path(base) / "data" / "sector_roster" / f"{sw}.json"
        if p.exists():
            try:
                roster = json.loads(p.read_text(encoding="utf-8")); break
            except Exception:
                pass
    if not roster:
        return []
    out = []
    for it in roster.get("roles", {}).get("补涨先锋", [])[:top]:
        out.append({"code": it["code"], "name": it.get("name", ""),
                    "联动依据": f"{sw}龙头消息利好且已动,同板块补涨待接力(位置低pos60={it.get('pos60')})"})
    return out


def build_news_driven_block(date: str, *, boards: Optional[list[str]] = None, client=None) -> dict:
    """产「消息驱动」块(统筹契约)。仅收 tag=利好 的板块。"""
    from tools.analysis.sector_forecast import news_catalyst as NC
    cat = NC.board_leader_catalyst(date, boards=boards, client=client)
    利好板块 = []
    for sw, c in cat.items():
        if c["消息标签"] != "利好":
            continue
        # 龙头候选:带催化 + 已动(无催化依据不进)
        leads = []
        for code, d in c.get("龙头明细", {}).items():
            if not d.get("明细") and d.get("净催化", 0) == 0:
                continue                                   # 无消息依据不进
            leads.append({"code": code, "name": d.get("name", ""),
                          "催化": d.get("净催化"), "已动": _leader_moved(code, date)})
        if not leads:
            continue
        利好板块.append({
            "board": sw, "tag": "利好", "strength": c["合并净催化"],
            "依据": f"龙头净催化{c['龙头净催化']}+政策净催化{c['政策净催化']}",
            "龙头候选": leads, "跟涨候选": _followers(sw, date),
        })
    利好板块.sort(key=lambda x: -x["strength"])
    return {"as_of": date, "version": BLOCK_VERSION, "利好板块": 利好板块,
            "口径": "消息催化(α源)驱动;龙头进候选池走逐票研判+回踩限价,跟涨进联动观察档单列验",
            "诚实边界": ["国际龙头专用源待接", "概念级成分缺(申万一级)",
                        "non-gating·实盘forward累积·未达标不gated"]}


def enrich_sector_focus(date: str, *, boards: Optional[list[str]] = None, client=None,
                        out_root: Optional[str] = None) -> Optional[Path]:
    """把「消息驱动」块合进已落盘的 sector_focus.json(选股侧单一源)。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    from tools.config import settings
    p = resolve_analysis_file(date, "sector_focus.json")
    if not p:
        logger.warning("无 sector_focus.json(%s),先跑板块面板/两步", date)
        return None
    focus = json.loads(p.read_text(encoding="utf-8"))
    focus["消息驱动"] = build_news_driven_block(date, boards=boards, client=client)
    # 写回(优先本仓 analysis;production 主仓即本仓)
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "analysis" / date
    root.mkdir(parents=True, exist_ok=True)
    out = root / "sector_focus.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(focus, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    n = len(focus["消息驱动"]["利好板块"])
    logger.info("sector_focus 扩「消息驱动」块 → %s(利好板块%d)", out, n)
    return out
