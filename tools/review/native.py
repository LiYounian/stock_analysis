"""各线原生口径列（双列报告 a 列）：**只 join 现有产物、绝不重算**。

用途：与统一 Model-A 列（b 列）并排，便于交叉核对/各线自身归因。当前源：
  · 板块消息线：`data/shadow_forward/sector_news/<date>.json`（sector_news_forward 已跑的回踩限价
    r_d1/r_d2/excess，逐票）→ native_sector。
  · 次日实盘线：`nextday_scorecard.csv` 是**逐日聚合**（非逐票），无法逐票 join → native_nextday=None
    有声缺失（其口径即 Model-A r_d1，跨版收益榜以 Model-A 列为准）。
缺产物 → None（不重算、不臆造）。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from tools.review.types import Pick

logger = logging.getLogger("review.native")


def _sector_news_index(date: str, data_root: str | None, cache: dict) -> dict[str, dict]:
    """读 shadow_forward/sector_news/<date>.json → {code: {r_d1,r_d2,excess_d1,stream}}。缺→{}。"""
    key = ("sector_news", date)
    if key in cache:
        return cache[key]
    root = Path(data_root) if data_root else None
    if root is None:
        try:
            from tools.analysis.market_forecast.dataroot import resolve_data_root
            root = resolve_data_root(None)
        except Exception:  # noqa: BLE001
            from tools.config import settings
            root = settings.PROJECT_ROOT / "data"
    path = root / "shadow_forward" / "sector_news" / f"{date}.json"
    idx: dict[str, dict] = {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        cache[key] = idx
        return idx
    for stream_key in ("龙头", "跟涨", "leaders", "followers"):
        for rec in (doc.get(stream_key) or []):
            code = rec.get("code")
            if not code:
                continue
            lb = rec.get("labels") or {}
            idx[str(code)] = {"r_d1": lb.get("r_d1"), "r_d2": lb.get("r_d2"),
                              "excess_d1": lb.get("excess_d1"), "stream": stream_key}
    cache[key] = idx
    return idx


def join_native(pick: Pick, data_root: str | None = None, cache: dict | None = None) -> dict:
    """返回 {native_nextday, native_sector}。best-effort join，缺→None。"""
    cache = cache if cache is not None else {}
    sec = None
    if pick.来源 == "板块催化":
        sec = _sector_news_index(pick.date, data_root, cache).get(pick.code)
    return {"native_nextday": None, "native_sector": sec}
