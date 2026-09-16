"""角色主表 build + 落库(产出 B)。data/sector_roster/<板块>.json,一板块一份。

更新频率(需求文档 §4 分层):日更价量字段 / 周复核名单 / 事件触发即时重估。P1 先做**全量重算**
落盘 + 变更留痕;周复核的"名单变更建议"由 diff 上一版产生(P1 记录版本,diff 逻辑留 P1.2 收尾)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("sector_forecast.roster")


def _members_by_sector() -> dict[str, list[str]]:
    """{申万一级: [code,...]}(reverse of code_industry + industry_map)。"""
    from tools.collectors import code_industry
    from tools.analysis import industry_map
    snap = code_industry.load()
    out: dict[str, list[str]] = {}
    for c, raw in snap.items():
        sw = industry_map.to_sw(raw) if raw else None
        if sw:
            out.setdefault(sw, []).append(c)
    return out


from functools import lru_cache


@lru_cache(maxsize=1)
def _name_map() -> dict:
    """code→name 全表(akshare 一次性拉,lru 缓存)。无网络 → 空 map,降级为代码。"""
    try:
        from tools.collectors import universe
        return {d["code"]: d["name"] for d in universe.fetch_universe(exclude_bj=False)}
    except Exception as e:
        logger.warning("股票名全表拉取失败(降级为代码显示):%s", e)
        return {}


def _stock_name(code: str) -> Optional[str]:
    return _name_map().get(code)


def build_roster(sw: str, date: str, members: list[str]) -> dict:
    """单板块角色主表。"""
    from tools.analysis.sector_forecast import roles as R
    feat = R.member_features(sw, members, date)
    roles = R.identify_roles(sw, feat) if not feat.empty else {
        r: [] for r in ("龙头", "中军", "补涨先锋", "弹性股")}
    # 补股票名
    for lst in roles.values():
        for it in lst:
            nm = _stock_name(it["code"])
            if nm:
                it["name"] = nm
    return {
        "板块": sw, "口径": "申万一级", "细分说明": R.SEED_NOTE.get(sw, ""),
        "更新日": date, "roles_version": R.ROLES_VERSION,
        "成分数": int(len(feat)), "参与打分数": int(len(feat)),
        "roles": roles,
        "诚实边界": ["概念级细分成分缺(仅申万一级)", "基本面quality/龙虎榜/北向未纳入P1角色打分(缺失不惩罚)"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def write_roster(payload: dict, *, out_root: Optional[str] = None) -> Path:
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_roster"
    root.mkdir(parents=True, exist_ok=True)
    out = root / f"{payload['板块']}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    return out


def build_all_rosters(date: str, *, frame=None, sectors: Optional[list[str]] = None,
                      out_root: Optional[str] = None) -> list[Path]:
    """对目标板块批量 build 角色主表并落盘。sectors 缺省 = 种子清单。"""
    from tools.analysis.sector_forecast import roles as R
    mem = _members_by_sector()
    targets = sectors or R.SEED_SW
    paths = []
    for sw in targets:
        members = mem.get(sw, [])
        if not members:
            logger.warning("板块 %s 无成分(申万映射为空),跳过", sw)
            continue
        payload = build_roster(sw, date, members)
        p = write_roster(payload, out_root=out_root)
        paths.append(p)
        nlead = len(payload["roles"]["龙头"])
        logger.info("角色表 %s → %s(成分%d,龙头候选%d)", sw, p, payload["成分数"], nlead)
    return paths
