"""星标:仅展示,不参与 SEPA 入池过滤、不打分排序。

基本面星:近报告期营收增速或净利增速 ≥0(缺数据不打星)。
板块星:同申万一级当日合格池只数 ≥ 配置阈值。
"""
from __future__ import annotations

from tools.analysis import industry_map
from tools.collectors import board, fundamental
from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["SEPA_VCP"]


def _is_st(name: str) -> bool:
    n = (name or "").replace(" ", "").upper()
    return "ST" in n


def fundamental_star(code: str) -> bool:
    """缺缓存 / 两增速皆空 → False(不打星,不挡入表1)。"""
    try:
        fd = fundamental.load_fundamental(code)
    except FileNotFoundError:
        return False
    g1, g2 = fd.get("营收增速"), fd.get("净利增速")
    return (g1 is not None and g1 >= 0) or (g2 is not None and g2 >= 0)


def industry_of(code: str) -> str | None:
    """申万一级;映射不到 → None。"""
    raw = board.board_of(code)
    return industry_map.to_sw(raw) if raw else None


def sector_star_codes(pool: list[dict], min_n: int | None = None) -> set[str]:
    """合格池内同业 ≥ min_n 的全部代码打板块星。"""
    need = int(min_n if min_n is not None else _CFG["板块星最少同业"])
    buckets: dict[str, list[str]] = {}
    for row in pool:
        ind = row.get("industry")
        if not ind:
            continue
        buckets.setdefault(ind, []).append(row["code"])
    starred: set[str] = set()
    for codes in buckets.values():
        if len(codes) >= need:
            starred.update(codes)
    return starred


__all__ = ["fundamental_star", "industry_of", "sector_star_codes", "_is_st"]
