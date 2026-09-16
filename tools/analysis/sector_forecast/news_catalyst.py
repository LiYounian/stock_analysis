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

CATALYST_VERSION = "v3-2026-09-16-rubric"   # v3:用户指正——先定义等级rubric再文字归类、去重、来源一手性+执行度
_DIR_SIGN = {"利好": 1, "利空": -1, "中性": 0}   # (旧数值路径遗留,主路径已弃数值)

# 近期新闻回看窗(用户:结合新旧,历史新闻也有影响;1-2 周较好)
LOOKBACK_DAYS = 12

# ════════════════════ 等级定义(rubric·先定义再归类·全文字不打分)════════════════════
# 用户要求:先明确"可信/不可信、影响大/小"的定义,让 LLM 按定义归类到文字档,绝不给数值。
RUBRIC = {
    "可信度": {  # 一手根源性 → 更可信、执行度更大
        "可信": "一手/官方/权威主流媒体,有明确出处(部委·交易所公告·公司正式公告·头部财经媒体)",
        "存疑": "二手转载/解读、无明确一手出处、机构观点或预期(评级/研报口径)",
        "不可信": "小道消息/传闻/营销号/无法核实来源",
    },
    "影响程度": {
        "大": "直接改变板块景气/基本面:政策落地、大额订单/中标、技术突破、产能/价格拐点",
        "中": "边际影响:单个公司事件、评级调整、非核心业务进展",
        "小": "常规波动/无实质:股价异动播报、日常经营、事务性公告",
    },
    "执行度": {  # 消息兑现的确定性(用户:一手消息执行度通常更大)
        "高": "已落地/已签约/官方已发布,确定性高",
        "中": "规划/意向/预期,方向明确但未兑现",
        "低": "传闻/概念炒作/仅题材,兑现不确定",
    },
    "来源": {"一手": "根源性(官方/当事方直接发布)", "二手": "转载/解读/引述"},
}

# 板块级研判:全部**文字档**(按 RUBRIC 归类),无任何数值
BOARD_VERDICT_SCHEMA = {
    "消息面": "利好 | 利空 | 中性 | 分歧 之一(对该板块整体)",
    "强弱": "强 | 中 | 弱(文字档;由影响程度+执行度+可信度综合,非数值)",
    "关键事件": ("list,每条:{时间:YYYY-MM-DD, 事件:简述, 方向:利好/利空, "
                "可信度:可信/存疑/不可信, 影响程度:大/中/小, 执行度:高/中/低, 来源:一手/二手}"),
    "持续性": "一句话:近1-2周是否多条同向(如『持续利好·两周多条算力订单』/『零星单条』/『近日转弱』)",
    "时效": "一句话:消息新鲜度(本周新/偏旧上周)与是否仍有效",
    "可靠性综述": "一句话:整体来源可靠性(一手为主/多为转载)+ 需人工进一步探查核实的点",
    "理由": "一句话综合研判(≤40字)",
}


def board_verdict_instruction(sw: str, prior_summary: str = "") -> str:
    rubric_txt = "\n".join(
        f"  【{dim}】" + "；".join(f"{k}={v}" for k, v in levels.items())
        for dim, levels in RUBRIC.items())
    prior = f"\n\n【此前已归纳(近期,勿重复分析,只在其上增量更新)】\n{prior_summary}" if prior_summary else ""
    return (
        f"你是「{sw}」板块消息面研判员。下面是该板块**龙头+中军(板块主力)个股**、板块政策、"
        "及**国际形势**的**近 1-2 周**新闻/公告(每条带日期与来源角色)。"
        "请综合**新旧新闻 + 国际对标**(如半导体/算力看海外龙头动向、有色看大宗价格)判断该板块当前"
        "**消息面**(不是基本面数值)。"
        "\n\n**分级定义(务必按此归类,只输出文字档、绝不打分/给数值——数值不可信)**:\n"
        + rubric_txt +
        "\n\n要求:①每条关键事件**必给时间节点** + 按上面定义标 可信度/影响程度/执行度/来源;"
        "②优先看**一手·根源性**消息(执行度更大),二手转载/传闻要在可靠性里点明需核实;"
        "③关注**持续利好**(近1-2周多条同向叠加=更大/持续利好);龙头与中军同向则更实;"
        "④**只用给定文本、禁止编造、禁止用日期之后的信息**。" + prior
    )


# 定向抓新闻的角色(用户 2026-09-16:龙头之外也抓中军=板块大资金主力,其公告/新闻亦是催化源)
NEWS_ROLES = ("龙头", "中军")


def role_codes(date: str, boards: Optional[list[str]] = None, *,
               roles: tuple[str, ...] = NEWS_ROLES) -> dict[str, list[dict]]:
    """{板块: [{code, name, role}]}(龙头+中军,带角色标签)。读 P1 角色表。"""
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
        picks, seen = [], set()
        for role in roles:
            for it in roster.get("roles", {}).get(role, []):
                if it["code"] in seen:
                    continue
                seen.add(it["code"])
                picks.append({"code": it["code"], "name": it.get("name", ""), "role": role})
        if picks:
            out[sw] = picks
    return out


def leader_codes(date: str, boards: Optional[list[str]] = None) -> dict[str, list[dict]]:
    """{板块: [{code, name}(仅龙头)]}(向后兼容)。"""
    return {sw: [{"code": d["code"], "name": d["name"]} for d in picks if d["role"] == "龙头"]
            for sw, picks in role_codes(date, boards, roles=("龙头",)).items()}


def score_leader_news(codes: list[str], *, date: str, client=None,
                      per_code_top: int = 5) -> dict[str, dict]:
    """对给定龙头 codes 定向抓新闻 + LLM 判影响 → {code: {净催化, n条, 利好, 利空, 明细}}。

    真 LLM 走网关(client=None → lc.get_client());无网关/失败 → 该条标 error 不计入,不崩。
    """
    from tools.collectors import news as news_col
    from tools.analysis import event
    from tools.llm import prompts
    from tools.llm import client as lc
    from tools.llm import rubric_map as rm

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
            strength = rm.strength_to_num(r.get("影响强度"), default=0.0)  # 文字档→数值(兼容legacy)
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


def _collect_board_news(date: str, sw: str, leads: list[dict], *,
                        lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """收该板块**近 lookback_days 天**新闻(龙头个股新闻 + 板块政策命中),带日期,去 date 之后。"""
    from tools.collectors import news as news_col
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    import json as _json
    import pandas as pd
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    items: list[dict] = []
    # 龙头个股新闻(近2周)
    codes = [d["code"] for d in leads]
    try:
        by_code = news_col.fetch_news(codes, days=lookback_days, workers=min(4, len(codes) or 1))
    except Exception as e:
        logger.warning("龙头新闻采集失败 %s: %s", sw, e)
        by_code = {}
    for d in leads:
        for it in (by_code.get(d["code"]) or []):
            t = str(it.get("time", ""))[:10]
            if t and (t < cutoff or t > date):
                continue
            who = f"{d.get('role','')}·{d['name'] or d['code']}"    # 标注龙头/中军
            items.append({"date": t or "?", "who": who,
                          "title": it.get("title", ""), "text": (it.get("content") or "")[:300]})
    # 板块政策命中 + 国际消息(sentiment_policy;region=国外 标为国际,用户:结合国际形势)
    p = resolve_analysis_file(date, "sentiment_policy.json")
    if p:
        try:
            from tools.analysis import industry_map
            for m in _json.loads(p.read_text(encoding="utf-8")):
                inds = m.get("industries") or m.get("受影响行业") or []
                if any(industry_map.to_sw(x) == sw for x in inds):
                    who = "国际形势" if m.get("region") == "国外" else "板块政策"
                    items.append({"date": m.get("date", date), "who": who,
                                  "title": m.get("title", ""), "text": (m.get("summary") or "")[:300]})
        except Exception:
            pass
    return items


def _news_key(it: dict) -> str:
    """新闻去重键:标题归一(去重复采集/重复分析用)。"""
    import hashlib
    t = (it.get("title") or "").strip()
    return hashlib.md5(t.encode("utf-8")).hexdigest()[:12] if t else ""


def _analyzed_path(sw: str, *, out_root: Optional[str] = None):
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_news" / "analyzed"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{sw}.json"


def _load_analyzed(sw: str) -> dict:
    import json as _json
    p = _analyzed_path(sw)
    if p.exists():
        try:
            d = _json.loads(p.read_text(encoding="utf-8"))
            d.setdefault("events", [])          # B:滚动关键事件累积(近2周)
            return d
        except Exception:
            pass
    return {"seen_keys": [], "summary": "", "updated": None, "events": []}


def _save_analyzed(sw: str, seen_keys: list[str], summary: str, date: str,
                   events: Optional[list] = None) -> None:
    import json as _json
    p = _analyzed_path(sw)
    p.write_text(_json.dumps({"seen_keys": seen_keys[-500:], "summary": summary,
                              "updated": date, "events": (events or [])[-60:]},
                             ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_events(prior: list, new: list, date: str, lookback_days: int = LOOKBACK_DAYS) -> list:
    """B:累积近 lookback_days 天关键事件(去重+按日剪枝),供每天看完整两周全貌(用户口径)。"""
    import pandas as pd
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for e in (prior or []) + (new or []):
        if not isinstance(e, dict):
            continue
        t = str(e.get("时间", ""))[:10]
        if t and t < cutoff:                    # 剪掉两周前的
            continue
        k = (t, (e.get("事件") or "")[:30])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    out.sort(key=lambda e: str(e.get("时间", "")))
    return out


def board_news_verdict(date: str, sw: str, leads: list[dict], *, client=None,
                       items: Optional[list[dict]] = None) -> dict:
    """板块级**描述性**研判(rubric 文字归类·无数值)。**去重增量**:旧新闻不重复分析,在此前归纳上增量更新。

    items:S3 采集层已落的原始消息(采集与研判解耦·S4 传入)。None → 回退实时采集(向后兼容)。
    """
    from tools.analysis import event
    from tools.llm import client as lc
    client = client or lc.get_client()
    items = items if items is not None else _collect_board_news(date, sw, leads)
    prior = _load_analyzed(sw)
    seen = set(prior.get("seen_keys") or [])
    # 只分析**新**新闻(去重:已分析过的不重复送 LLM)
    new_items = [it for it in items if _news_key(it) and _news_key(it) not in seen]
    new_items.sort(key=lambda x: x["date"])
    if not items:
        return {"消息面": "中性", "强弱": "弱", "关键事件": [], "持续性": "近1-2周无相关新闻",
                "时效": "无", "可靠性综述": "无数据", "理由": "无消息", "n条": 0, "新增": 0}
    if not new_items and prior.get("summary"):
        # 无新增新闻 → 沿用此前归纳(不重复烧 LLM);仍呈现累积的近2周关键事件(B)
        v = _summary_to_verdict(prior["summary"])
        v["关键事件"] = _merge_events(prior.get("events", []), [], date)   # 剪枝到近2周
        return {**v, "n条": len(items), "新增": 0,
                "时间跨度": f"{items[0]['date']}~{items[-1]['date']}", "备注": "无新增新闻,沿用此前归纳"}
    blob = "\n".join(f"[{it['date']}·{it['who']}] {it['title']} {it['text']}" for it in new_items[:40])
    try:
        r = event._cached_extract(client, blob[:6000],
                                  board_verdict_instruction(sw, prior.get("summary", "")),
                                  BOARD_VERDICT_SCHEMA)
    except Exception as e:
        r = {"消息面": "中性", "强弱": "弱", "关键事件": [], "持续性": "研判失败",
             "时效": "?", "可靠性综述": f"error: {str(e)[:50]}", "理由": "LLM失败降级"}
    r["n条"] = len(items)
    r["新增"] = len(new_items)
    r["时间跨度"] = f"{new_items[0]['date']}~{new_items[-1]['date']}" if new_items else "?"
    # B:把本次新事件并入滚动累积 → 每天呈现完整近2周关键事件(用户口径:去重只是不重复分析,展示保留全量)
    merged = _merge_events(prior.get("events", []), r.get("关键事件", []), date)
    r["关键事件"] = merged
    # 更新去重集 + 滚动归纳 + 累积事件(供次日增量,不丢历史)
    new_keys = list(seen | {_news_key(it) for it in new_items if _news_key(it)})
    summary = f"{date} 消息面={r.get('消息面')}·{r.get('强弱','')}:{r.get('理由','')}｜持续性:{r.get('持续性','')}"
    _save_analyzed(sw, new_keys, summary, date, events=merged)
    return r


def _summary_to_verdict(summary: str) -> dict:
    """从滚动归纳文本回填一个最小 verdict(无新增新闻时用)。"""
    面 = "利好" if "利好" in summary else ("利空" if "利空" in summary else "中性")
    return {"消息面": 面, "强弱": None, "关键事件": [], "持续性": summary,
            "时效": "无新增", "可靠性综述": "沿用此前归纳", "理由": summary[:40]}


def board_leader_catalyst(date: str, *, boards: Optional[list[str]] = None,
                          client=None) -> dict[str, dict]:
    """板块级消息面**描述性**研判(v2:文字不数值·结合近1-2周·识别持续利好)。

    返回 {板块: {消息标签(=消息面), 强弱, 关键事件[带时间], 持续性, 时效与可靠性, 理由, n条, 时间跨度, 龙头}}。
    标签直接取 LLM 的描述性"消息面"(利好/利空/中性/分歧),**不做数值加权**。
    """
    leaders = role_codes(date, boards)          # 龙头 + 中军(用户:中军新闻也是催化源)
    out: dict[str, dict] = {}
    for sw, leads in leaders.items():
        v = board_news_verdict(date, sw, leads, client=client)   # client 惰性(board_news_verdict 内取)
        out[sw] = {
            "消息标签": v.get("消息面", "中性"), "强弱": v.get("强弱"),
            "关键事件": v.get("关键事件", []), "持续性": v.get("持续性"),
            "时效": v.get("时效"), "可靠性综述": v.get("可靠性综述"), "理由": v.get("理由"),
            "n条": v.get("n条", 0), "新增": v.get("新增", 0), "时间跨度": v.get("时间跨度"),
            "龙头": [{"code": d["code"], "name": d["name"]} for d in leads if d.get("role") == "龙头"],
            "中军": [{"code": d["code"], "name": d["name"]} for d in leads if d.get("role") == "中军"],
        }
    return out
