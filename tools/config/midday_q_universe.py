"""午盘 Q 专用票池(只读)。

真源 = `config/midday_q_universe.json`(见 docs/每日分析_午盘Q/M2_票池草案_v2.md)。
定位:**与主票池 stock_pool 完全独立**——本模块不写、不改主票池、不 import stock_pool。

结构:
    {
      "version": "1.0",
      "focus_codes": [ ... 126 只精选池代码 ... ],
      "codes": [ {code, name, sw3, in_focus, ...}, ... 413 只主池 ],
      "sources": {...}, "sw3_stats": {...}
    }

读接口:
    get_focus_codes()      精选池 126 只(生产默认用这个)
    get_full_codes()        主池 413 只(--full-universe 时切到这个)
    get_meta(code)          单票元信息(sw3/name/manual_note)
    by_sw3()                按申万三级归组
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from tools.config import settings

logger = logging.getLogger("config.midday_q_universe")

_STORE = settings.PROJECT_ROOT / "config" / "midday_q_universe.json"


@lru_cache(maxsize=1)
def _load() -> dict:
    """读票池 JSON。文件缺失 → 抛 FileNotFoundError(不做静默默认,防止用户以为在跑但实际空跑)。"""
    if not _STORE.exists():
        raise FileNotFoundError(
            f"午盘 Q 票池不存在: {_STORE}(先生成,见 docs/每日分析_午盘Q/M2_票池草案_v2.md)"
        )
    with open(_STORE, encoding="utf-8") as f:
        return json.load(f)


def reload() -> None:
    """票池 JSON 手工修改后调用,清缓存。"""
    _load.cache_clear()


def get_focus_codes() -> list[str]:
    """精选池 126 只(生产 default)。"""
    return list(_load().get("focus_codes") or [])


def get_full_codes() -> list[str]:
    """主池 413 只(--full-universe)。"""
    return [r["code"] for r in _load().get("codes") or []]


def get_meta(code: str) -> dict | None:
    """按代码取元信息 {code, name, sw3, in_focus, manual_note?}。找不到 → None。"""
    for r in _load().get("codes") or []:
        if r.get("code") == code:
            return dict(r)
    return None


def by_sw3() -> dict[str, list[str]]:
    """按申万三级归组:{sw3: [code, ...]}。"""
    groups: dict[str, list[str]] = {}
    for r in _load().get("codes") or []:
        sw3 = r.get("sw3") or "(未分类)"
        groups.setdefault(sw3, []).append(r["code"])
    return groups


def stats() -> dict:
    """票池统计快照(供日志/审计)。"""
    data = _load()
    return {
        "version": data.get("version"),
        "updated": data.get("updated"),
        "total": len(data.get("codes") or []),
        "focus_n": len(data.get("focus_codes") or []),
        "sw3_stats": dict(data.get("sw3_stats") or {}),
    }
