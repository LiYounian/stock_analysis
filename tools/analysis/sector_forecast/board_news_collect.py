"""S3 · 定向消息采集(每日·"按表采集")——程序化流水线第三阶段(**采集层独立程序**)。

设计契约:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S3。
**采集与研判解耦**:本阶段**只抓取、不判**——读 S2 角色关系表(sector_roster_table)
拿每板块 龙头/中军/主力 code,逐板块逐角色定向抓 个股新闻/公告 + 板块政策 + 国际对标
+ 资金流(LHB龙虎榜),落原始消息过程文件(逐条可溯:来源/时间/角色/URL)。研判交 S4。

输入:S2 data/sector_roster_table/<板块>.json(缺则回退每日 roster data/sector_roster/)。
输出:data/sector_news/raw/<date>/<板块>.json(原始消息 + 来源 + 采集时刻 + LHB资金流快照)。
周期:每日(下午/EOD)。**无 LLM**(纯采集)。

角色口径:默认采 **龙头/中军/主力**(催化/大资金承载者);补涨先锋=量价跟随、无独立催化,
默认不采(可经 roles 参数开启)。防未来:新闻只保留 ≤date;LHB 用 lhb_asof(上榜日<date·T+1)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.board_news_collect")

RAW_VERSION = "v1-2026-09-16"
LOOKBACK_DAYS = 12                       # 与 news_catalyst.LOOKBACK_DAYS 对齐(近1-2周)
COLLECT_ROLES = ("龙头", "中军", "主力")   # 默认采集角色(催化/大资金承载者)
LHB_WINDOW_DAYS = 30                      # 附带的 LHB 资金流回看窗


def _read_role_table(date: str, sw: str) -> Optional[dict]:
    """读 S2 sector_roster_table/<sw>.json;缺 → 回退每日 roster/<sw>.json;都缺 → None。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for sub in ("sector_roster_table", "sector_roster"):
        for base in (settings.PROJECT_ROOT, _MAIN):
            p = Path(base) / "data" / sub / f"{sw}.json"
            if p.exists():
                try:
                    return json.loads(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
    return None


def role_picks(date: str, boards: list[str], *,
               roles: tuple[str, ...] = COLLECT_ROLES) -> dict[str, list[dict]]:
    """{板块: [{code, name, role}]}——读 S2 关系表(全板块含主力),按 roles 取、去重(一票多角色留首个)。"""
    out: dict[str, list[dict]] = {}
    for sw in boards:
        table = _read_role_table(date, sw)
        if not table:
            continue
        rmap = table.get("roles", {})
        picks, seen = [], set()
        for role in roles:
            for it in rmap.get(role, []):
                c = it.get("code")
                if not c or c in seen:
                    continue
                seen.add(c)
                picks.append({"code": c, "name": it.get("name", ""), "role": role})
        if picks:
            out[sw] = picks
    return out


def _lhb_flow(code: str, date: str, *, window_days: int = LHB_WINDOW_DAYS) -> Optional[dict]:
    """该票近 window_days 天龙虎榜资金流快照(PIT·上榜日<date)。无快照/无上榜 → None。"""
    from datetime import timedelta
    from tools.collectors import lhb
    cutoff = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        evs = lhb.lhb_asof(code, date)
    except Exception:
        return None
    win = [e for e in evs if str(e.get("list_date", ""))[:10] >= cutoff]
    if not win:
        return None
    net = sum((e.get("net_buy") or 0.0) for e in win)
    return {"累计净买亿": round(net / 1e8, 3), "上榜次数": len(win),
            "最近上榜": max((str(e.get("list_date", ""))[:10] for e in win), default=""),
            "方向": "净买入" if net > 0 else ("净卖出" if net < 0 else "中性")}


def collect_board_raw(date: str, sw: str, picks: list[dict], *,
                      lookback_days: int = LOOKBACK_DAYS) -> dict:
    """采单板块原始消息(个股新闻 + 板块政策/国际 + LHB资金流)。逐条可溯,无 LLM。"""
    from tools.collectors import news as news_col
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    import pandas as pd
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    role_of = {d["code"]: d["role"] for d in picks}
    name_of = {d["code"]: d.get("name", "") for d in picks}
    codes = [d["code"] for d in picks]

    items: list[dict] = []
    # ① 个股新闻/公告(龙头/中军/主力·近 lookback_days)
    try:
        by_code = news_col.fetch_news(codes, days=lookback_days, workers=min(4, len(codes) or 1))
    except Exception as e:
        logger.warning("S3 新闻采集失败 %s: %s", sw, e)
        by_code = {}
    for c in codes:
        for it in (by_code.get(c) or []):
            t = str(it.get("time", ""))[:10]
            if t and (t < cutoff or t > date):
                continue
            items.append({
                "date": t or "?", "role": role_of.get(c, ""), "code": c,
                "who": f"{role_of.get(c,'')}·{name_of.get(c) or c}",
                "title": it.get("title", ""), "text": (it.get("content") or "")[:300],
                "source": it.get("source", ""), "url": it.get("url", ""), "kind": "个股新闻",
            })
    # ② 板块政策 + 国际对标(sentiment_policy;region=国外→国际形势)
    p = resolve_analysis_file(date, "sentiment_policy.json")
    if p:
        try:
            from tools.analysis import industry_map
            for m in json.loads(p.read_text(encoding="utf-8")):
                inds = m.get("industries") or m.get("受影响行业") or []
                if any(industry_map.to_sw(x) == sw for x in inds):
                    intl = m.get("region") == "国外"
                    items.append({
                        "date": str(m.get("date", date))[:10],
                        "role": "国际形势" if intl else "板块政策", "code": "",
                        "who": "国际形势" if intl else "板块政策",
                        "title": m.get("title", ""), "text": (m.get("summary") or "")[:300],
                        "source": m.get("source", "sentiment_policy"), "url": m.get("url", ""),
                        "kind": "国际对标" if intl else "板块政策",
                    })
        except Exception as e:
            logger.warning("S3 政策/国际采集跳过 %s: %s", sw, e)
    # ③ 资金流(LHB 龙虎榜)——逐 code 快照
    资金流 = []
    for c in codes:
        f = _lhb_flow(c, date)
        if f:
            资金流.append({"code": c, "name": name_of.get(c, ""), "role": role_of.get(c, ""), **f})

    items.sort(key=lambda x: x["date"])
    n_news = sum(1 for it in items if it["kind"] == "个股新闻")
    return {
        "板块": sw, "as_of": date, "version": RAW_VERSION,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "角色": picks,
        "items": items, "资金流": 资金流,
        "统计": {"总条数": len(items), "个股新闻": n_news,
                "政策/国际": len(items) - n_news, "LHB覆盖票数": len(资金流),
                "时间跨度": f"{items[0]['date']}~{items[-1]['date']}" if items else None},
        "口径": "只抓不判(采集与研判解耦);龙头/中军/主力定向新闻+板块政策+国际对标+LHB资金流·逐条可溯",
        "免责": "测试环境研究模拟,非投资建议。",
    }


def raw_dir(date: str, *, out_root: Optional[str] = None) -> Path:
    from tools.config import settings
    base = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_news"
    return base / "raw" / date


def write_board_raw(date: str, sw: str, payload: dict, *, out_root: Optional[str] = None) -> Path:
    d = raw_dir(date, out_root=out_root)
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{sw}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    return out


def load_board_raw(date: str, sw: str, *, out_root: Optional[str] = None) -> Optional[dict]:
    """读 S3 已采原始消息(供 S4 研判);缺 → None(S4 回退实时采集)。"""
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    roots = [out_root] if out_root else [settings.PROJECT_ROOT, _MAIN]
    for base in roots:
        p = Path(base) / "data" / "sector_news" / "raw" / date / f"{sw}.json" if not out_root \
            else raw_dir(date, out_root=out_root) / f"{sw}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def collect_all(date: str, boards: list[str], *, roles: tuple[str, ...] = COLLECT_ROLES,
                lookback_days: int = LOOKBACK_DAYS, out_root: Optional[str] = None) -> list[Path]:
    """对全板块(读 S2 表)逐板块采集 + 落 raw 过程文件。返回落盘路径 list。"""
    picks_by_board = role_picks(date, boards, roles=roles)
    if not picks_by_board:
        logger.warning("S3 无板块角色表(先跑 S2 sector_roster_table),不采集")
        return []
    paths = []
    for sw, picks in picks_by_board.items():
        payload = collect_board_raw(date, sw, picks, lookback_days=lookback_days)
        p = write_board_raw(date, sw, payload, out_root=out_root)
        paths.append(p)
        st = payload["统计"]
        logger.info("S3 采集 %s → %s(共%d条/新闻%d/政策国际%d/LHB%d票)", sw, p,
                    st["总条数"], st["个股新闻"], st["政策/国际"], st["LHB覆盖票数"])
    return paths
