"""Shadow-run 候选池构造(可复现·防未来) —— headless α 证据扩样用。

复用 write_picks 的策略视图加载/榜单提取(单一真源),对 data/analysis/<date>/ 下
当日全部"策略视图"(顶层非 6 位 json,含 code 榜单)按**逐日一致、不按日调参**的固定规则
构造候选池,供 deep_analysis 逐票研判(DeepSeek 自选 shadow-run)。

规则(固定):
  1) 选择性策略集 = 当日全部含 code 榜单的策略视图,**排除两个"全域/宽网"聚合层**:
       - `策略0合议`(council,对全域打分的元聚合,非原始筛选策略;常达千级)
       - `最大范围选股`(显式"最大范围"宽网,常 100~300+ 只)
     其余(动量组合/放量后缩量回踩/量价放量/反转低换手组合/箱体形态/趋势深跌反包/
     半导体多因子/最强选股/扣非质量 等)均计为一个独立选择性策略。
  2) 命中:code 出现在某策略榜单中即"命中"该策略(只认成员资格,不认榜内名次——
     经核验多数宽网榜单按 code 升序而非强度排序,名次不可靠;见报告局限)。
  3) hit_count(code) = 命中的选择性策略数。
  4) 候选 = {hit_count>=2 的票}(多策略交叉确认,核心稳健信号)。
     若不足 MIN(=12):按策略"选择性由强到弱"(榜单由短到长)轮流补入各策略成员
     (仍在榜的、未入选的),直到 >=MIN 或耗尽。
  5) 排序键 = (hit_count desc, 最小命中策略榜长 asc[命中越选择性的策略越强], code asc);
     取前 CAP(=14) 只。
防未来:策略视图由当日流程产出(as_of<=当日),仅含 <=date 的数据。
"""
from __future__ import annotations
import json
from pathlib import Path

from tools.analysis.write_picks import _load_strategy_views, _extract_ranklist

CAP = 14
MIN = 12
# 排除的全域/宽网聚合层(按名字稳定排除,跨日一致)
EXCLUDE = {"策略0合议", "最大范围选股"}


def _screens(data_root, date: str) -> dict[str, list[str]]:
    """{策略名: [code,...]}(选择性策略;已排除全域聚合层;code 归一 6 位串)。"""
    views = _load_strategy_views(Path(data_root), date)
    out: dict[str, list[str]] = {}
    for name, obj in views.items():
        if name in EXCLUDE:
            continue
        rl = _extract_ranklist(obj)
        codes = [str(it["code"]).zfill(6) for it in rl
                 if isinstance(it, dict) and it.get("code") is not None]
        if codes:
            out[name] = codes
    return out


def build_pool(data_root, date: str, cap: int = CAP, min_n: int = MIN):
    screens = _screens(data_root, date)
    hit: dict[str, int] = {}
    min_screen_len: dict[str, int] = {}
    for name, codes in screens.items():
        for c in codes:
            hit[c] = hit.get(c, 0) + 1
            L = len(codes)
            if c not in min_screen_len or L < min_screen_len[c]:
                min_screen_len[c] = L
    cand = {c for c, n in hit.items() if n >= 2}
    # 不足则按策略选择性(榜短优先)轮流补
    if len(cand) < min_n:
        by_sel = sorted(screens.items(), key=lambda kv: len(kv[1]))
        added = True
        while len(cand) < min_n and added:
            added = False
            for _name, codes in by_sel:
                for c in codes:
                    if c not in cand:
                        cand.add(c)
                        added = True
                        break
                if len(cand) >= min_n:
                    break
    ranked = sorted(cand, key=lambda c: (-hit.get(c, 0), min_screen_len.get(c, 10**9), c))
    pool = ranked[:cap]
    meta = {
        "date": date,
        "screens": {k: len(v) for k, v in screens.items()},
        "excluded": sorted(EXCLUDE),
        "pool_size": len(pool),
        "multi_hit_in_pool": sum(1 for c in pool if hit.get(c, 0) >= 2),
        "hit_counts": {c: hit.get(c, 0) for c in pool},
    }
    return pool, meta


if __name__ == "__main__":
    DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
    days = ["2026-08-11","2026-08-13","2026-08-14","2026-08-18","2026-08-20",
            "2026-08-24","2026-08-26","2026-08-28","2026-08-31","2026-09-01",
            "2026-09-02","2026-09-03","2026-09-04","2026-09-08","2026-09-09","2026-09-10"]
    for d in days:
        pool, meta = build_pool(DR, d)
        print(f"{d}: n={meta['pool_size']} multi>=2:{meta['multi_hit_in_pool']} "
              f"screens={meta['screens']}")
        print(f"    pool={','.join(pool)}")
        print(f"    hits={meta['hit_counts']}")
