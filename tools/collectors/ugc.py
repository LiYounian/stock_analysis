"""UGC 舆情采集:东财股吧(市场评论、散户/大V 讨论)。

情绪三层中最难的「舆情」层:强反爬、信噪比低。问题台账 R3/Q3 已定:
**本轮只抓东财股吧**(反爬低、与资金流同源),雪球需登录态,本轮不做(见文末)。

取数机制:复用 fundflow 的 curl_cffi 指纹伪装(impersonate="chrome")拉
东财股吧列表页 `guba.eastmoney.com/list,{code}.html`。该页服务端把帖子列表
以 `var article_list = {...}` 直接内联在 HTML 里(SSR),curl_cffi 实测可拿到
(200,含 80+ 帖),无需浏览器/登录态。解析该 JSON 即得帖子。

情感打分是后续 LLM 的事(见 analysis/sentiment);本模块只负责
**采集 + 量化热度指标(纯代码)**。落盘:data/raw/ugc/{code}.json。
契约:fetch_ugc(codes, limit) -> {code: [帖子...]};compute_heat(code) -> 热度指标。
"""
from __future__ import annotations

import json
import logging
import re
import time

from tools.config import settings

logger = logging.getLogger("collectors.ugc")

_UGC_DIR = settings.DATA_RAW / "ugc"
# 东财股吧列表页(SSR 内联帖子 JSON,curl_cffi 可直取,见模块 docstring)
_LIST_URL = "https://guba.eastmoney.com/list,{code}.html"
# 内联帖子列表的正则锚点:var article_list = {...};
_ARTICLE_RE = re.compile(r"var\s+article_list\s*=\s*(\{.*?\});", re.S)


def _ugc_path(code: str):
    return _UGC_DIR / f"{code}.json"


def _http_get(code: str) -> str:
    """curl_cffi 伪装 chrome 拉东财股吧列表页 HTML。抽出便于测试 mock。"""
    from curl_cffi import requests as creq

    r = creq.get(_LIST_URL.format(code=code), impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.text


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
    """拉单票股吧帖子(不落盘)。空数据抛错,不返回空列表伪装成功。"""
    items = _parse(_http_get(code), limit)
    if not items:
        raise ValueError(f"{code} 股吧无帖子(接口异常/被反爬/代码错)")
    return items


def fetch_ugc(codes: list[str], limit: int | None = None) -> dict[str, list[dict]]:
    """抓取每票近期股吧帖子并落盘。

    输出:{code: [{time, author, is_v, text, likes, replies}, ...]}(时间倒序)。
    is_v 标记是否大V/加V用户,供热度加权。
    单票失败记 logger 跳过,不中断整批;拉到空视作失败(不静默)。
    """
    settings.ensure_dirs()
    _UGC_DIR.mkdir(parents=True, exist_ok=True)
    limit = limit or settings.UGC_LIMIT

    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    for code in codes:
        try:
            items = fetch_one(code, limit)
            _ugc_path(code).write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            out[code] = items
            logger.info("股吧 %s:%d 帖", code, len(items))
        except Exception as e:
            failed.append(code)
            logger.error("股吧 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("股吧拉取失败(%d): %s", len(failed), failed)
    return out


def load_ugc(code: str) -> list[dict]:
    """从本地缓存读单票 UGC 帖子。缓存缺失抛错。"""
    p = _ugc_path(code)
    if not p.exists():
        raise FileNotFoundError(f"{code} 无 UGC 缓存,请先 fetch_ugc: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _heat(posts: list[dict]) -> dict:
    """纯函数:由帖子列表算量化热度指标(不依赖情感打分,完全可测)。

    - post_count:帖子数
    - v_ratio:大V帖占比(v帖数 / 帖数),无帖为 0
    - reply_total:回复总数
    - heat_score:热度分 = (帖数 + 0.5*回复总数 + 0.2*点赞总数) * (1 + v_ratio),保留2位
      直觉:讨论量为主、互动(回复>点赞)加成,有大V参与再整体放大。
    """
    n = len(posts)
    if n == 0:
        return {"post_count": 0, "v_ratio": 0.0, "reply_total": 0, "heat_score": 0.0}
    v_cnt = sum(1 for p in posts if p.get("is_v"))
    reply_total = sum(int(p.get("replies") or 0) for p in posts)
    like_total = sum(int(p.get("likes") or 0) for p in posts)
    v_ratio = round(v_cnt / n, 4)
    heat = (n + 0.5 * reply_total + 0.2 * like_total) * (1 + v_ratio)
    return {
        "post_count": n,
        "v_ratio": v_ratio,
        "reply_total": reply_total,
        "heat_score": round(heat, 2),
    }


def compute_heat(code: str) -> dict:
    """基于已抓 UGC 算量化热度指标(读本地缓存;不依赖情感打分)。

    输出:{post_count, v_ratio, reply_total, heat_score}。
    缓存缺失抛错(经 load_ugc)。纯计算逻辑见 _heat。
    """
    return _heat(load_ugc(code))
