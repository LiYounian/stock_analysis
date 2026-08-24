"""UGC 舆情采集:东财股吧(市场评论、散户/大V 讨论)。

情绪三层中最难的「舆情」层:强反爬、信噪比低。问题台账 R3/Q3 已定:
**本轮只抓东财股吧**(反爬低、与资金流同源),雪球需登录态,本轮不做(见文末)。

取数机制:复用 fundflow 的 curl_cffi 指纹伪装(impersonate="chrome")拉
东财股吧列表页 `guba.eastmoney.com/list,{code}.html`。该页服务端把帖子列表
以 `var article_list = {...}` 直接内联在 HTML 里(SSR),curl_cffi 实测可拿到
(200,含 80+ 帖),无需浏览器/登录态。解析该 JSON 即得帖子。

情感打分是后续 LLM 的事(见 analysis/sentiment);本模块只负责
**采集 + 量化热度指标(纯代码)**。

落盘走 store 层(依赖方向 collectors→store 合规):
`store.put_raw("ugc", code, items, meta=...)` / `store.get_raw("ugc", code)`,
不再直读写 data/raw/ugc/{code}.json 路径(store 内部收敛为 json kind)。

时间窗:仿 news.py,只保留发帖日期在 `today - NEWS_LOOKBACK_DAYS` 之后的帖子,
使跨票热度可比(否则各票取到的帖子时间跨度不一,热度绝对量无意义)。
契约:fetch_ugc(codes, limit) -> {code: [帖子...]};compute_heat(code) -> 热度指标。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, timedelta

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.ugc")

_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))  # 被墙机快速失败降级(curl_cffi 单独传参)

# 东财股吧列表页(SSR 内联帖子 JSON,curl_cffi 可直取,见模块 docstring)
_LIST_URL = "https://guba.eastmoney.com/list,{code}.html"
# 内联帖子列表的正则锚点:var article_list = {...};
_ARTICLE_RE = re.compile(r"var\s+article_list\s*=\s*(\{.*?\});", re.S)
# 帖子时间解析:带年份 "YYYY-MM-DD ..." 与无年份 "MM-DD ..." 两种股吧常见格式
_DATE_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})(?:\s|$)")


def _http_get(code: str) -> str:
    """curl_cffi 伪装 chrome 拉东财股吧列表页 HTML。抽出便于测试 mock。"""
    from curl_cffi import requests as creq

    r = creq.get(_LIST_URL.format(code=code), impersonate="chrome", timeout=_TIMEOUT)
    r.raise_for_status()
    return r.text


def _post_date(t: str) -> str | None:
    """从股吧帖子 time 字段抽出 `YYYY-MM-DD`;解析不出返回 None。

    东财列表页时间格式不统一:
      - `YYYY-MM-DD HH:MM[:SS]`(带年份)→ 直取前 10 位;
      - `MM-DD HH:MM`(无年份,列表页最常见)→ 补**最近合理年份**:先按今年组装,
        若得到的日期落在未来(跨年边界,如今年 1 月看到去年 12 月的帖)则回退去年。
    解析不出(空串/非法月日/其它格式)→ 返回 None,由调用方选择**保留**该帖
    (宁保留勿误删:时间窗过滤只丢弃"能确认早于窗口"的帖,存疑一律留下)。
    """
    t = (t or "").strip()
    m = _DATE_FULL_RE.match(t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_MMDD_RE.match(t)
    if m:
        today = date.today()
        mm, dd = int(m.group(1)), int(m.group(2))
        try:
            d = date(today.year, mm, dd)
        except ValueError:
            return None
        if d > today:                    # 跨年:今年组装成了未来 → 实为去年
            try:
                d = date(today.year - 1, mm, dd)
            except ValueError:
                return None
        return d.isoformat()
    return None


def _cutoff(days: int | None = None) -> str:
    """时间窗下界 `YYYY-MM-DD` = today - days(缺省取 settings.NEWS_LOOKBACK_DAYS)。"""
    days = settings.NEWS_LOOKBACK_DAYS if days is None else days
    return (date.today() - timedelta(days=days)).isoformat()


def _filter_recent(posts: list[dict], cutoff: str) -> list[dict]:
    """丢弃发帖日期早于 cutoff 的帖子;日期解析不出的**保留**(宁保留勿误删)。"""
    kept = []
    for p in posts:
        d = _post_date(p.get("time", ""))
        if d is not None and d < cutoff:
            continue
        kept.append(p)
    return kept


def _extract_one(p: dict) -> dict:
    """把股吧一条帖子(re[] 元素)归一成契约字段。

    东财 re[] 结构不一致:热帖/精华帖作者在 `post_user` 子对象(带 user_v),
    普通帖作者在顶层 `user_nickname` + `v_user_code`。两处都兜。
    is_v:加V认证(user_v>0)或顶层 v_user_code>0 即视为大V。
    text:列表页仅给标题(post_title),正文(post_content)仅热帖有,优先正文回落标题。
    """
    pu = p.get("post_user") or {}
    author = pu.get("user_nickname") or p.get("user_nickname") or ""

    def _int(x) -> int:
        try:
            return int(x or 0)
        except (TypeError, ValueError):
            return 0

    is_v = bool(_int(pu.get("user_v")) or _int(p.get("v_user_code")))
    text = (p.get("post_content") or "").strip() or (p.get("post_title") or "").strip()
    return {
        "time": str(p.get("post_publish_time") or p.get("post_display_time") or ""),
        "author": str(author),
        "is_v": is_v,
        "text": text[:2000],
        "likes": _int(p.get("post_like_count") or p.get("source_post_like_count")),
        "replies": _int(p.get("post_comment_count") or p.get("source_post_comment_count")),
    }


def _parse(html: str, limit: int | None = None) -> list[dict]:
    """从股吧列表页 HTML 抽 `var article_list` 并归一成帖子列表(按发帖时间倒序)。"""
    m = _ARTICLE_RE.search(html or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    items = [_extract_one(p) for p in (data.get("re") or []) if isinstance(p, dict)]
    # 过滤纯空帖(无标题/正文),按时间倒序
    items = [it for it in items if it["text"]]
    items.sort(key=lambda x: x["time"], reverse=True)
    return items[:limit] if limit else items


def fetch_one(code: str, limit: int | None = None) -> list[dict]:
    """拉单票股吧帖子(不落盘)。空数据抛错,不返回空列表伪装成功。

    先解析全部帖子,再按时间窗(today - NEWS_LOOKBACK_DAYS)过滤,最后取最新 limit 条。
    过滤在取 limit 之前,避免"先截断再过滤"漏掉窗口内的帖子。
    """
    items = _filter_recent(_parse(_http_get(code)), _cutoff())
    if limit:
        items = items[:limit]
    if not items:
        raise ValueError(f"{code} 股吧无帖子(接口异常/被反爬/代码错/全部超时间窗)")
    return items


def fetch_ugc(codes: list[str], limit: int | None = None) -> dict[str, list[dict]]:
    """抓取每票近期股吧帖子并经 store 落盘。

    输出:{code: [{time, author, is_v, text, likes, replies}, ...]}(时间倒序)。
    仅保留时间窗(today - NEWS_LOOKBACK_DAYS)内的帖子,使跨票热度可比。
    is_v 标记是否大V/加V用户,供热度加权。
    单票失败记 logger 跳过,不中断整批;拉到空视作失败(不静默)。
    """
    limit = limit or settings.UGC_LIMIT

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 股吧 %s 采集...", i, n, code)
        try:
            items = fetch_one(code, limit)
            store.put_raw("ugc", code, items, meta={"source": "eastmoney_guba"})
            out[code] = items
            logger.info("股吧 %s:%d 帖", code, len(items))
        except Exception as e:
            failed.append(code)
            logger.error("股吧 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("股吧拉取失败(%d): %s", len(failed), failed)
    return out


def load_ugc(code: str, date: str | None = None) -> list[dict]:
    """从 store 读单票 UGC 帖子。缓存缺失抛 FileNotFoundError。

    date:缺省 None → "latest"(向后兼容);显式传日期即锁定读该日分区(不回退,缺则抛)。
    情绪引擎的 date-pin + 可识别回退走 store.get_raw_resolved,此处只做简单透传。
    """
    return store.get_raw("ugc", code, date=date or "latest")


def _day_span(posts: list[dict]) -> int:
    """帖子实际覆盖的天数 = 最新帖与最早帖日期跨度 + 1,至少 1。

    仅用能解析出日期的帖子(见 _post_date);少于 2 个可解析日期时无法算跨度,
    返回 1(退化为不归一,heat_per_day == heat_score)。
    """
    dates = [d for p in posts if (d := _post_date(p.get("time", ""))) is not None]
    if len(dates) < 2:
        return 1
    span = (date.fromisoformat(max(dates)) - date.fromisoformat(min(dates))).days + 1
    return max(span, 1)


def _heat(posts: list[dict]) -> dict:
    """纯函数:由帖子列表算量化热度指标(不依赖情感打分,完全可测)。

    - post_count:帖子数
    - v_ratio:大V帖占比(v帖数 / 帖数),无帖为 0
    - reply_total:回复总数
    - heat_score:热度分 = (帖数 + 0.5*回复总数 + 0.2*点赞总数) * (1 + v_ratio),保留2位
      直觉:讨论量为主、互动(回复>点赞)加成,有大V参与再整体放大。
      **绝对量**,跨票不可比(各票取到的帖子时间跨度不一)。
    - heat_per_day:日均热度 = heat_score / 帖子覆盖天数(见 _day_span),保留2位。
      **归一口径**,除掉时间跨度差异后跨票可比。跨度无法计算(<2 个可解析日期)时
      退化为 heat_per_day == heat_score。原 heat_score 保留不删(向后兼容)。
    """
    n = len(posts)
    if n == 0:
        return {"post_count": 0, "v_ratio": 0.0, "reply_total": 0,
                "heat_score": 0.0, "heat_per_day": 0.0}
    v_cnt = sum(1 for p in posts if p.get("is_v"))
    reply_total = sum(int(p.get("replies") or 0) for p in posts)
    like_total = sum(int(p.get("likes") or 0) for p in posts)
    v_ratio = round(v_cnt / n, 4)
    heat = round((n + 0.5 * reply_total + 0.2 * like_total) * (1 + v_ratio), 2)
    span = _day_span(posts)
    heat_per_day = round(heat / span, 2)
    return {
        "post_count": n,
        "v_ratio": v_ratio,
        "reply_total": reply_total,
        "heat_score": heat,
        "heat_per_day": heat_per_day,
    }


def compute_heat(code: str) -> dict:
    """基于已抓 UGC 算量化热度指标(读本地缓存;不依赖情感打分)。

    输出:{post_count, v_ratio, reply_total, heat_score, heat_per_day}。
    缓存缺失抛错(经 load_ugc)。纯计算逻辑见 _heat。
    """
    return _heat(load_ugc(code))
