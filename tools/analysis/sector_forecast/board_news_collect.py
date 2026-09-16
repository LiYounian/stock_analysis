"""S3 · 定向消息采集(每日·"按表采集")——程序化流水线第三阶段(**采集层独立程序**)。

设计契约:docs/计划/2026-09-16_消息板块选股_程序化流水线_设计.md §1 S3。
**采集与研判解耦**:本阶段**只抓取、不判**——读 S2 角色关系表(sector_roster_table)
拿每板块 龙头/中军/主力 code,逐板块逐角色定向抓 个股新闻/公告 + 板块政策 + 国际对标
+ 资金流(LHB龙虎榜),落原始消息过程文件(逐条可溯:来源/时间/角色/URL)。研判交 S4。

输入:S2 data/sector_roster_table/<板块>.json(缺则回退每日 roster data/sector_roster/)。
输出:data/sector_news/raw/<date>/<板块>.json(原始消息 + 来源 + 采集时刻 + LHB资金流快照)。
周期:每日(下午/EOD)。**无 LLM**(纯采集)。

角色口径:默认采 **龙头/中军/主力**(催化/大资金承载者);补涨先锋=量价跟随、无独立催化,
默认不采(可经 roles 参数开启)。LHB 用 lhb_asof(上榜日<date·T+1)。

新闻时效口径(allow_future 开关·防未来硬红线):
  · **allow_future=False(默认·严格 as-of)**:新闻只保留 ≤date(剔除 t>date 的未来新闻)。
    **forward-shadow / 回测 / as-of 复盘的调用方必须走默认严格**——否则那些验证数值全废。
  · **allow_future=True(『最大可得新闻·live 口径·非防未来』)**:跳过 t>date 剔除,用当前能抓到的
    最多新闻(下限窗口仍生效)。**仅供当日最优选股(live 生产)**——S3 每日 runner 传 True。
量价/财报仍严格 as-of≤date(不受本开关影响)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.board_news_collect")

RAW_VERSION = "v3-2026-09-17-deep"       # v3:新闻正文加深(300→500)+ 截断标记(供 S4 可选LLM压缩)
LOOKBACK_DAYS = 12                       # 与 news_catalyst.LOOKBACK_DAYS 对齐(近1-2周)
COLLECT_ROLES = ("龙头", "中军", "主力")   # 默认采集角色(催化/大资金承载者)
LHB_WINDOW_DAYS = 30                      # 附带的 LHB 资金流回看窗
NEWS_TEXT_MAX = 500                       # 正文保留上限(§4.7②加深:300→500;S3 只截断不压缩·保持无LLM)


def _clip_text(s: str) -> tuple[str, int, bool]:
    """正文加深:短文全放、长文截到 NEWS_TEXT_MAX 并标 truncated。返回 (文本, 原长, 是否截断)。

    S3 保持"只抓不判·无 LLM"——超长只截断+标记,真正的 LLM 逐条压缩放 S4 前置(解耦)。
    """
    s = s or ""
    n = len(s)
    if n <= NEWS_TEXT_MAX:
        return s, n, False
    return s[:NEWS_TEXT_MAX], n, True

# ════════════════════ P1 · 榜单/数据表噪音过滤 ════════════════════
# 依据 DeepSeek-vs-Opus 对比(docs/计划/2026-09-17_...优化建议.md):全市场排行/数据表文章
# 仅因个股代码出现在表里就被挂到该股 → 纯噪音、还带一串数字,淹没真催化。这里按**标题**
# 剔除这类"市场级榜单/资金流向表/解禁一览/筹码换手榜"。**保守取向**:只打明确的榜单/数据
# 表措辞,真催化标题(中标/集采/订单/业绩/技术落地/公告)不含这些词、不会被误杀。
import re as _re

_NOISE_TITLE_RE = _re.compile("|".join([
    r"附股",                                   # (附股) 榜单标配
    r"一览", r"图谱", r"数据丨",                # 汇总/图谱/数据栏目
    r"资金流向?日报", r"资金净流[出入]", r"主力.{0,6}净流[出入]", r"净流[出入]超",
    r"\d+\s*股.{0,4}净流",                      # "8股主力资金净流出"
    r"筹码大换手", r"每笔成交", r"成交量增长",
    r"股东户数", r"户数下降", r"户数增长",
    r"解禁市值", r"解禁比例", r"限售股.{0,8}解禁", r"\d+\s*股.{0,4}解禁",
    r"融资余额", r"杠杆资金", r"融资.{0,4}[增减]仓",
    r"概念.{0,8}(下跌|上涨|拉升|走强|走弱)\s*[\d一二三四五六七八九十]",  # "X概念下跌1.05%"
    r"站上.{0,3}均线", r"短线走稳",
    r"\d+\s*只股", r"\d+\s*股(涨停|跌停|上榜|大涨|大跌)",
    r"收盘涨停", r"涨停潮", r"涨停.{0,3}附股", r"涨停(板)?(一览|名单)",
    r"市值居前", r"[涨跌]幅居前", r"增幅居前", r"[涨跌]幅榜",
    r"回购图谱", r"回购一览", r"龙虎榜.{0,4}(一览|名单|数据)",
    r"减持.{0,4}(一览|名单|榜)",
]))


def _is_noise_title(title: str) -> bool:
    """P1:标题命中市场级榜单/数据表措辞 → 判噪音(市场级、非本股催化)。"""
    return bool(title) and bool(_NOISE_TITLE_RE.search(title))


# ════════════════════ P4 · 跨股误挂修正 ════════════════════
# 现象(P1 残余):个股新闻页/按名过滤偶尔返回**主体是另一只股**的文章(如"威尔高"页返回
# "摩尔线程20cm跌停")。保守判据:本股名(≥2字)在**标题与正文都不出现** = 这条压根没讲本股
# → 剔。只在本股名**完全缺席**时剔(出现在任一处即留·防误杀真讲本股但用简称/代码的)。
def _name_core(name: str) -> str:
    """取简称核心(去 *ST/ST/XD/XR/N/中国 等前缀),用于宽松匹配防"中国长城 vs 长城"误杀。"""
    n = (name or "").strip().replace(" ", "")
    for pre in ("*ST", "ST", "XD", "XR", "N", "中国"):
        if n.startswith(pre) and len(n) - len(pre) >= 2:
            n = n[len(pre):]
            break
    return n


from functools import lru_cache as _lru_cache


@_lru_cache(maxsize=1)
def _name_set() -> frozenset:
    """全A 正式简称集合(≥3字·防高频短词撞名)。无网络/无映射 → 空集(P4 降级为不判·不误杀)。"""
    try:
        from tools.analysis.sector_forecast.roster import _name_map
        return frozenset(n for n in _name_map().values() if isinstance(n, str) and len(n.replace(" ", "")) >= 3)
    except Exception:
        return frozenset()


def _other_subject_in_title(title: str, own: str, own_core: str) -> bool:
    """标题**开头段**是否点了**另一只具名股**(≥3字·全A名集)。有=正面证据本条讲别人。"""
    ns = _name_set()
    if not ns:
        return False
    head = (title or "").replace(" ", "")[:16]         # 只看标题主体区(开头),防顺带提及
    for i in range(len(head)):
        for L in (4, 3):
            cand = head[i:i + L]
            if len(cand) == L and cand in ns and cand != own and cand not in own and own not in cand \
                    and cand != own_core:
                return True
    return False


def _is_misattached(name: str, title: str, text: str) -> bool:
    """P4:**正面证据式**跨股误挂——本股名(全名或核心)在标题+正文均缺席 **且** 标题开头点了
    另一只具名股 → 判误挂剔除。

    只在有另一主体正面证据时才剔(不再"名缺席即剔"——个股新闻常是行业/事件标题不含股名、
    实为本股新闻,误剔=踏空)。宽松匹配防"中国长城 vs 长城"误杀;名缺失/无名集 → 不判。
    """
    full = (name or "").strip().replace(" ", "")
    if len(full) < 2:
        return False
    blob = ((title or "") + (text or "")).replace(" ", "")
    core = _name_core(name)
    if full in blob or (len(core) >= 2 and core in blob):
        return False                                    # 本股名出现在任一处 → 留
    return _other_subject_in_title(title, full, core)   # 本股缺席 且 标题点了别人 → 剔


# ════════════════════ P2 · 事件级去重(近重复转载只留一条)════════════════════
# _news_key(title[:12]) 只抓前缀精确重复,抓不住多家媒体**近乎逐字转载**同一条(解禁/终止
# 各被转发多次)。这里用**中文字符 bigram Jaccard**聚类:相似度≥阈值=近重复,留正文最长一条。
# **保守取向**:阈值偏高,只合近逐字转载(占噪音大头·安全不误合);语义改写的同事件合并
# 需 LLM 语义(超字符串能力·留后续 LLM 压缩支处理),本层不强合、宁漏不误。
_DEDUP_SIM = 0.70                          # bigram Jaccard 阈值(预注册·偏高防误合不同事件)


def _cn_bigrams(s: str) -> set:
    s = _re.sub(r"[^一-鿿]", "", s or "")     # 只留中文字,去数字/标点/英文
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def _sim(a: str, b: str) -> float:
    A, B = _cn_bigrams(a), _cn_bigrams(b)
    if not A or not B:
        return 0.0
    inter = len(A & B)
    sm = min(len(A), len(B))
    # 短核心(≥3 bigram)几乎全含于另一条 → 近重复(来源前缀/截断转载,如"【基金报】终止收购")。
    if sm >= 3 and inter / sm >= 0.85:
        return 1.0
    return inter / len(A | B)             # 否则 Jaccard(近逐字转载)


def _dedup_events(items: list[dict]) -> tuple[list[dict], int]:
    """P2:板块内同事件多标题去重(相似度聚类,留正文最长一条)。返回 (去重后, 合并掉的条数)。"""
    kept: list[dict] = []
    merged = 0
    for it in items:
        hit = None
        for k in kept:
            if _sim(it.get("title", ""), k.get("title", "")) >= _DEDUP_SIM:
                hit = k
                break
        if hit is None:
            kept.append(it)
        else:
            merged += 1
            # 保留信息量更高(正文更长)的一条
            if len(it.get("text", "") or "") > len(hit.get("text", "") or ""):
                hit.update(it)
    return kept, merged


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
                      lookback_days: int = LOOKBACK_DAYS,
                      allow_future: bool = False) -> dict:
    """采单板块原始消息(个股新闻 + 板块政策/国际 + LHB资金流)。逐条可溯,无 LLM。

    allow_future=False(默认·严格):新闻剔除 t>date(防未来·回测/forward 必走此支)。
    allow_future=True(live 口径):跳过 t>date 剔除,取当前最多新闻(仅当日最优选股用)。
    """
    from tools.collectors import news as news_col
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    import pandas as pd
    cutoff = (pd.Timestamp(date) - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    role_of = {d["code"]: d["role"] for d in picks}
    name_of = {d["code"]: d.get("name", "") for d in picks}
    codes = [d["code"] for d in picks]

    items: list[dict] = []
    n_noise = 0                          # P1:被过滤的榜单/数据表噪音条数(可审)
    n_misattach = 0                      # P4:被剔的跨股误挂条数(可审)
    # ① 个股新闻/公告(龙头/中军/主力·近 lookback_days)
    try:
        by_code = news_col.fetch_news(codes, days=lookback_days, workers=min(4, len(codes) or 1))
    except Exception as e:
        logger.warning("S3 新闻采集失败 %s: %s", sw, e)
        by_code = {}
    for c in codes:
        for it in (by_code.get(c) or []):
            t = str(it.get("time", ""))[:10]
            # 下限窗口始终生效;未来上限(t>date)仅在严格 as-of 口径剔除(allow_future=False)。
            if t and (t < cutoff or (not allow_future and t > date)):
                continue
            title = it.get("title", "")
            if _is_noise_title(title):    # P1:市场级榜单/数据表噪音 → 剔除(非本股催化)
                n_noise += 1
                continue
            content = it.get("content") or ""
            if _is_misattached(name_of.get(c, ""), title, content):   # P4:本股名全缺席=跨股误挂 → 剔
                n_misattach += 1
                continue
            body, full_len, truncated = _clip_text(content)
            items.append({
                "date": t or "?", "role": role_of.get(c, ""), "code": c,
                "who": f"{role_of.get(c,'')}·{name_of.get(c) or c}",
                "title": title, "text": body, "text_full_len": full_len, "text_truncated": truncated,
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
                    p_body, p_len, p_trunc = _clip_text(m.get("summary") or "")
                    items.append({
                        "date": str(m.get("date", date))[:10],
                        "role": "国际形势" if intl else "板块政策", "code": "",
                        "who": "国际形势" if intl else "板块政策",
                        "title": m.get("title", ""), "text": p_body,
                        "text_full_len": p_len, "text_truncated": p_trunc,
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

    # P2:事件级去重(同事件多标题只留信息量最高一条)——在噪音已过滤(P1)基础上再去重
    items, n_merged = _dedup_events(items)
    items.sort(key=lambda x: x["date"])
    n_news = sum(1 for it in items if it["kind"] == "个股新闻")
    return {
        "板块": sw, "as_of": date, "version": RAW_VERSION,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
        "lookback_days": lookback_days,
        "allow_future": allow_future,
        "新闻口径": ("最大可得新闻·live(非防未来)" if allow_future else "严格 as-of≤date(防未来)"),
        "角色": picks,
        "items": items, "资金流": 资金流,
        "统计": {"总条数": len(items), "个股新闻": n_news,
                "政策/国际": len(items) - n_news, "LHB覆盖票数": len(资金流),
                "P1过滤噪音": n_noise, "P2去重合并": n_merged, "P4跨股误挂": n_misattach,
                "时间跨度": f"{items[0]['date']}~{items[-1]['date']}" if items else None},
        "口径": "只抓不判(采集与研判解耦);龙头/中军/主力定向新闻+板块政策+国际对标+LHB资金流·"
                "逐条可溯;已 P1 榜单噪音过滤 + P2 事件去重(洗输入)",
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
                lookback_days: int = LOOKBACK_DAYS, out_root: Optional[str] = None,
                allow_future: bool = False) -> list[Path]:
    """对全板块(读 S2 表)逐板块采集 + 落 raw 过程文件。返回落盘路径 list。

    allow_future 透传 collect_board_raw:默认严格(回测/forward);live 每日 runner 传 True。
    """
    picks_by_board = role_picks(date, boards, roles=roles)
    if not picks_by_board:
        logger.warning("S3 无板块角色表(先跑 S2 sector_roster_table),不采集")
        return []
    paths = []
    for sw, picks in picks_by_board.items():
        payload = collect_board_raw(date, sw, picks, lookback_days=lookback_days,
                                    allow_future=allow_future)
        p = write_board_raw(date, sw, payload, out_root=out_root)
        paths.append(p)
        st = payload["统计"]
        logger.info("S3 采集 %s → %s(共%d条/新闻%d/政策国际%d/LHB%d票)", sw, p,
                    st["总条数"], st["个股新闻"], st["政策/国际"], st["LHB覆盖票数"])
    return paths
