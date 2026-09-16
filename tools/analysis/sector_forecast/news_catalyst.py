"""消息驱动板块选股 · 一入 M1 —— 定向龙头股新闻采集 + LLM 影响分析 → 板块龙头催化。

设计见 docs/计划/2026-09-16_消息驱动板块选股_一入一出_设计.md。
**修正重心(用户 2026-09-16)**:消息(新闻/政策)是板块轮动主力信号,量价退辅助。本模块做"一入"的
定向部分:对各板块**龙头股**定向抓新闻 → LLM 判影响(利好/利空/强度)→ 汇成**板块龙头净催化**,
叠加已有板块级政策催化 → 板块消息标签(供"一出"选股)。

复用(不重造):
  · 龙头名单 = data/sector_roster/<板块>.json 的 roles.龙头(本线 P1 产出)。
  · 定向新闻采集 = tools.collectors.news.fetch_news(codes)(个股多源新闻,单一真源)。
  · LLM 判影响 = tools.analysis.event._cached_extract + tools.llm.prompts.POLICY_SCORE_SCHEMA
    (与政策打分同款"影响方向/强度",口径一致、带缓存)。真 LLM 走网关(zsh -ic env)。
  · 板块级政策催化 = sector_forecast.focus.news_catalyst_by_sector(已有)。

防未来:只判 ≤date 已发布新闻(fetch_news 落盘按时间;评估用 date 当日快照)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.news_catalyst")

CATALYST_VERSION = "v1-2026-09-16"
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}


def leader_codes(date: str, boards: Optional[list[str]] = None) -> dict[str, list[dict]]:
    """{板块: [{code, name}(龙头主选/备选)]}。boards 缺省 = 种子板块;读 P1 角色表。"""
    from tools.analysis.sector_forecast.roles import SEED_SW
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    import json
    boards = boards or SEED_SW
    out: dict[str, list[dict]] = {}
    for sw in boards:
        roster = None
        for base in (settings.PROJECT_ROOT, _MAIN):
            p = Path(base) / "data" / "sector_roster" / f"{sw}.json"
            if p.exists():
                try:
                    roster = json.loads(p.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass
        if not roster:
            continue
        leads = [{"code": it["code"], "name": it.get("name", "")}
                 for it in roster.get("roles", {}).get("龙头", [])]
        if leads:
            out[sw] = leads
    return out


def score_leader_news(codes: list[str], *, date: str, client=None,
                      per_code_top: int = 5) -> dict[str, dict]:
    """对给定龙头 codes 定向抓新闻 + LLM 判影响 → {code: {净催化, n条, 利好, 利空, 明细}}。

    真 LLM 走网关(client=None → lc.get_client());无网关/失败 → 该条标 error 不计入,不崩。
    """
    from tools.collectors import news as news_col
    from tools.analysis import event
    from tools.llm import prompts
    from tools.llm import client as lc

    client = client or lc.get_client()
    try:
        news_by_code = news_col.fetch_news(codes, workers=min(4, len(codes) or 1))
    except Exception as e:
        logger.warning("龙头新闻采集失败:%s", e)
        news_by_code = {}

    out: dict[str, dict] = {}
    for code in codes:
        items = (news_by_code.get(code) or [])[:per_code_top]
        net, good, bad, detail = 0.0, 0, 0, []
        for it in items:
            text = f"标题:{it.get('title','')}\n摘要:{it.get('content','') or ''}"[:1200]
            instr = prompts.policy_score_instruction(None)
            try:
                r = event._cached_extract(client, text, instr, prompts.POLICY_SCORE_SCHEMA)
            except Exception as e:
                r = {"error": str(e)[:60]}
            if "影响方向" not in r:
                continue
            sign = _DIR_SIGN.get(r.get("影响方向"), 0)
            strength = float(r.get("影响强度") or 0)
            net += sign * strength
            if sign > 0:
                good += 1
            elif sign < 0:
                bad += 1
            detail.append({"title": it.get("title", "")[:40], "方向": r.get("影响方向"),
                           "强度": r.get("影响强度")})
        out[code] = {"净催化": round(net, 1), "n条": len(items), "利好": good,
                     "利空": bad, "明细": detail}
    return out


def board_leader_catalyst(date: str, *, boards: Optional[list[str]] = None,
                          client=None) -> dict[str, dict]:
    """板块级龙头催化:对各板块龙头定向抓+判影响 → 汇成板块龙头净催化 + 消息标签。

    返回 {板块: {龙头净催化, 龙头明细, 政策净催化, 合并净催化, 消息标签}}。
    消息标签(预注册):合并净催化 ≥ +HI → 利好;≤ −HI → 利空;否则中性。
    """
    from tools.analysis.sector_forecast import focus as F
    HI = 3.0
    leaders = leader_codes(date, boards)
    policy_cat = F.news_catalyst_by_sector(date)     # 板块级政策/新闻催化(已有)

    out: dict[str, dict] = {}
    for sw, leads in leaders.items():
        codes = [d["code"] for d in leads]
        scored = score_leader_news(codes, date=date, client=client)
        lead_net = round(sum(v["净催化"] for v in scored.values()), 1)
        pol_net = round(policy_cat.get(sw, {}).get("净催化", 0.0), 1)
        merged = round(lead_net + pol_net, 1)
        tag = "利好" if merged >= HI else ("利空" if merged <= -HI else "中性")
        out[sw] = {
            "龙头净催化": lead_net, "政策净催化": pol_net, "合并净催化": merged,
            "消息标签": tag,
            "龙头明细": {c: {"name": next((d["name"] for d in leads if d["code"] == c), ""),
                          **scored[c]} for c in scored},
        }
    return out
