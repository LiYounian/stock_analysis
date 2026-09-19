"""news_raw（②消息塔层）· 按 code 查当日个股新闻**原文条目**。

背景：三个合成 Agent 此前只能拿到 per-stock 情绪净分标量，拿不到带 source/url 的
新闻原文——辩证查证"看来源、辨真假利好"缺物质基础。本工具是**只读下钻查询接口**：
把当日该票的新闻条目原样 surface（不打分、不改写标签、不编造）。

数据源（防未来：只取 publish 日期 ≤ as_of 的条目）：
  主源 data/raw/<as_of>/baidu_news/<code>.json（list，每条含
       title/source/publish_time/publish_ts/benefit_label(利好/利空/中性)/abstract/url）。
  兜底 data/raw/<as_of>/news/<code>.json（仅 title/content/time/source/url，无利好利空标签）。
  主源缺 → 用兜底；兜底无 benefit_label → 标"未标注"、abstract 用 content。

分层输出（统筹裁定 A·2026-09-20）：
  · 浓缩块 = 紧凑摘要（≤8 行）：header 1 行给"近 N 条：利好x/利空y/中性z"，
    再列最近 TOP_N 条，每条仅 `{标签} {标题}｜{来源}·{时间}`——**url 与长摘要不进浓缩块**。
  · fields["news"] = 全量条目（title/source/url/publish_time/benefit_label/abstract 全留），
    供 agent 下钻看来源/url/全文摘要；CLI `tool news_raw --json` 可 dump 出来。

三态缺数据（诚实）：两源文件全缺→missing(该票无覆盖)；文件在但 0 条→fresh(已落盘·0 条)；
有条目→fresh。绝不因缺数据编造新闻。
"""
from __future__ import annotations

from typing import Optional, Any
import datetime as _dt
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

# ── 展示参数（非阈值·仅浓缩块紧凑度；全量始终在 fields）──
TOP_N = 5            # 浓缩块最多列几条（最近优先）；全量在 fields["news"]
标题截断 = 30         # 浓缩块标题最大字数（防单行过长）
标签枚举 = ("利好", "利空", "中性", "未标注")  # 语义锁：benefit_label 原样 + 兜底"未标注"


def _as_of_cutoff(as_of: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(as_of)
    except ValueError:
        return None


def _parse_date(s: Any) -> Optional[_dt.date]:
    """从 publish_time / time 字符串取日期（前 10 字 YYYY-MM-DD）。失败→None。"""
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _sort_ts(item: dict) -> float:
    """排序键：优先 publish_ts；否则由 publish_time/time 解析；均缺→0（沉底）。"""
    ts = item.get("publish_ts")
    if isinstance(ts, (int, float)) and ts > 0:
        return float(ts)
    t = item.get("publish_time") or item.get("time")
    try:
        return _dt.datetime.fromisoformat(str(t)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _load_json_list(path: str) -> Optional[list]:
    """读一个新闻 json；文件缺→None；解析失败或非 list→[]（视作已落盘但 0 条）。"""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return []
    return d if isinstance(d, list) else []


def _normalize(items: list, from_baidu: bool) -> list[dict]:
    """把两源条目归一成统一字段：标题/来源/时间/标签/摘要/url/ts。

    baidu 源有 benefit_label/abstract；news 兜底源无标签(标"未标注")、摘要用 content。
    """
    out = []
    for e in items:
        if not isinstance(e, dict):
            continue
        if from_baidu:
            标签 = e.get("benefit_label") or "未标注"
            摘要 = e.get("abstract") or ""
            时间 = e.get("publish_time") or ""
        else:
            标签 = "未标注"  # 兜底 news 源无利好利空标签
            摘要 = e.get("content") or ""
            时间 = e.get("time") or ""
        if 标签 not in 标签枚举:
            标签 = "未标注"  # 未知标签值也归"未标注"，绝不臆测正负
        out.append({
            "标题": e.get("title") or "",
            "来源": e.get("source") or "",
            "时间": 时间,
            "标签": 标签,
            "摘要": 摘要,
            "url": e.get("url") or "",
            "_ts": _sort_ts({**e, "publish_time": 时间}),
        })
    return out


class NewsRawTool:
    name = "news_raw"
    塔层 = "②消息"
    面 = "消息情绪面"  # 消息真实性·原文下钻（辩证查证用）
    source = "data/raw/<date>/baidu_news/<code>.json（主）+ news/<code>.json（兜底）"

    def run(self, as_of: str, code: Optional[str] = None, root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("news_raw 需 --code")
        base = os.path.join(data_root(root), "data", "raw", as_of)
        p_baidu = os.path.join(base, "baidu_news", f"{code}.json")
        p_news = os.path.join(base, "news", f"{code}.json")

        raw = _load_json_list(p_baidu)
        used = "baidu_news"
        if raw is None:  # 主源文件缺 → 兜底
            raw = _load_json_list(p_news)
            used = "news"
        elif len(raw) == 0:  # 主源在但 0 条 → 也看兜底能否补
            alt = _load_json_list(p_news)
            if alt:
                raw, used = alt, "news"

        # 两源文件皆缺 → missing（该票无覆盖），绝不编
        if raw is None:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="新闻原文: 该票无新闻覆盖（baidu_news / news 均未落盘）·人工确认",
                fields={"条数": 0, "news": [], "source_used": None,
                        "利好": 0, "利空": 0, "中性": 0, "未标注": 0},
                freshness="missing", 防未来=True, source=self.source,
            )

        items = _normalize(raw, from_baidu=(used == "baidu_news"))
        # 防未来：只保留 publish 日期 ≤ as_of 的条目（防个别条目 post-date）
        cutoff = _as_of_cutoff(as_of)
        if cutoff is not None:
            items = [it for it in items if (_parse_date(it["时间"]) is None) or (_parse_date(it["时间"]) <= cutoff)]
        items.sort(key=lambda it: it["_ts"], reverse=True)

        # 标签计数
        cnt = {k: 0 for k in 标签枚举}
        for it in items:
            cnt[it["标签"]] = cnt.get(it["标签"], 0) + 1
        n = len(items)

        # 文件在但（过滤后）0 条 → fresh·已落盘 0 条（样本无≠缺失）
        if n == 0:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块=f"新闻原文: 该票当日无新闻条目（{used} 已落盘·0 条）",
                fields={"条数": 0, "news": [], "source_used": used,
                        "利好": 0, "利空": 0, "中性": 0, "未标注": 0},
                freshness="fresh", 防未来=True, source=self.source,
            )

        # 浓缩块：header + 最近 TOP_N 条（紧凑·不含 url/摘要）
        head = (f"新闻原文({used}): 近 {n} 条 利好{cnt['利好']}/利空{cnt['利空']}/"
                f"中性{cnt['中性']}" + (f"/未标注{cnt['未标注']}" if cnt['未标注'] else "")
                + (f"（下列最近 {min(TOP_N, n)} 条·url与摘要见 fields）" if n > TOP_N else ""))
        lines = [head]
        for it in items[:TOP_N]:
            标题 = it["标题"][:标题截断] + ("…" if len(it["标题"]) > 标题截断 else "")
            时间 = str(it["时间"])[:16]  # 到分钟即可
            lines.append(f"[{it['标签']}] {标题}｜{it['来源']}·{时间}")

        # fields["news"] 全量条目（含 url/摘要）——机读下钻源（去掉内部 _ts）
        news_full = [{k: v for k, v in it.items() if k != "_ts"} for it in items]
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
            fields={
                "条数": n, "source_used": used,
                "利好": cnt["利好"], "利空": cnt["利空"], "中性": cnt["中性"], "未标注": cnt["未标注"],
                "news": news_full,
            },
            freshness="fresh", 防未来=True, source=self.source,
        )


register(NewsRawTool())
