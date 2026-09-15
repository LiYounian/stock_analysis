"""真实选股候选流加载器。

从主仓 `data/analysis/<D>/<策略>.json` 提取 (date, strategy, code),口径鲁棒:
优先已知清单键,回退到"首个含 code/代码 的 dict 列表"(含向下探一层)。
另支持 `docs/每日分析/选股/*.md` 首行 `<!-- PICKS: ... -->` 锚点(blended 最终选股)。

⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import glob
import json
import os
import re
import pandas as pd

# 已知清单键(按优先级);回退到通用扫描
_LIST_KEYS = ("入选清单", "top", "rows", "重排", "评分", "picks", "票", "清单")

# 核心"选股"策略(代表我们的深度选股决策);池/闸门另计
CORE_STRATEGIES = [
    "策略0合议", "最强选股", "动量组合", "放量后缩量回踩", "反转低换手组合",
    "半导体多因子", "量价放量", "最大范围选股", "趋势深跌反包", "箱体形态",
    "指标条件化状态排序", "扣非质量",
]
MSG_STRATEGIES = ["候选池消息面确认", "消息面评分"]   # 消息面/催化维度
POOL_STRATEGIES = ["SEPA合格池", "SEPA观察池", "多策略命中闸门"]  # 大池,非最终选股

_CODE_RE = re.compile(r"^\d{6}$")


_COUNT_KEYS = ("入选数", "top_n", "top_k", "合格数", "重点数")
# 策略级硬上限:合议正常出 top20,早期 dump 模式(top_n=1997=全量排名)按排名截断复原
STRATEGY_CAP = {"策略0合议": 20}
DUMP_GUARD = 400   # 声明计数 > 此值视为"全量排名 dump",非最终选股 → 按排名取 top20


def _declared_count(obj: dict) -> int | None:
    for k in _COUNT_KEYS:
        v = obj.get(k)
        if isinstance(v, int) and v >= 0:
            return v
    return None


def _cap_for(obj: dict, strategy: str | None, n_codes: int) -> int | None:
    if strategy in STRATEGY_CAP:
        return STRATEGY_CAP[strategy]
    dc = _declared_count(obj)
    if dc is not None and dc <= DUMP_GUARD:
        return dc
    if n_codes > DUMP_GUARD:   # dump 模式:按排名复原 top20
        return 20
    return None


def _extract_codes(obj, strategy: str | None = None) -> list[str]:
    """从一个策略 JSON dict 提取入选 code 列表(鲁棒)。

    早期 schema 会把"全量排名"放进 top(如 1997 行),真正入选是 top_n 截断;
    存在声明计数且清单更长时按排名截断;合议固定 top20;dump 模式按排名复原。
    """
    if not isinstance(obj, dict):
        return []
    # 1) 已知键
    for k in _LIST_KEYS:
        v = obj.get(k)
        if isinstance(v, list):
            codes = _codes_from_list(v)
            if codes:
                cap = _cap_for(obj, strategy, len(codes))
                if cap is not None and 0 <= cap < len(codes):
                    codes = codes[:cap]
                return codes
    # 2) 通用:扫顶层所有 list-of-dict-with-code,取最像"入选"的(最短的非空,避免命中大池)
    cands = []
    for v in obj.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            codes = _codes_from_list(v)
            if codes:
                cands.append(codes)
        elif isinstance(v, dict):
            for vv in v.values():
                if isinstance(vv, list) and vv and isinstance(vv[0], dict):
                    codes = _codes_from_list(vv)
                    if codes:
                        cands.append(codes)
    if cands:
        codes = max(cands, key=len)  # 顶层清单通常是最全的入选集
        cap = _cap_for(obj, strategy, len(codes))
        if cap is not None and 0 <= cap < len(codes):
            codes = codes[:cap]
        return codes
    return []


def _codes_from_list(lst) -> list[str]:
    out = []
    for it in lst:
        if not isinstance(it, dict):
            if isinstance(it, str) and _CODE_RE.match(it):
                out.append(it)
            continue
        c = it.get("code") or it.get("代码")
        if isinstance(c, str) and _CODE_RE.match(c):
            out.append(c)
    # 去重保序
    seen = set(); uniq = []
    for c in out:
        if c not in seen:
            seen.add(c); uniq.append(c)
    return uniq


def load_picks(data_root: str, strategies: list[str] | None = None,
               start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """→ DataFrame[date(str), strategy, code]。date = 决策日 D。"""
    if strategies is None:
        strategies = CORE_STRATEGIES
    base = os.path.join(data_root, "data", "analysis")
    rows = []
    date_dirs = sorted(d for d in os.listdir(base)
                       if re.match(r"^\d{4}-\d{2}-\d{2}$", d))
    for d in date_dirs:
        if start and d < start:
            continue
        if end and d > end:
            continue
        for strat in strategies:
            fp = os.path.join(base, d, f"{strat}.json")
            if not os.path.exists(fp):
                continue
            try:
                j = json.load(open(fp))
            except Exception:  # noqa: BLE001
                continue
            for code in _extract_codes(j, strat):
                rows.append((d, strat, code))
    return pd.DataFrame(rows, columns=["date", "strategy", "code"])


_PICKS_ANCHOR = re.compile(r"<!--\s*PICKS:\s*([0-9,\s]+?)\s*-->")


def load_md_picks(repo_root: str, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
    """docs/每日分析/选股/YYYY-MM-DD.md 首行 PICKS 锚点 → blended 最终选股。
    仅取正规日期文件(不取 日内_ / 日内全A_ / _修正版 等变体,避免重复/滞后)。"""
    d = os.path.join(repo_root, "docs", "每日分析", "选股")
    rows = []
    for fp in sorted(glob.glob(os.path.join(d, "*.md"))):
        name = os.path.basename(fp)
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", name)
        if not m:
            continue
        date = m.group(1)
        if (start and date < start) or (end and date > end):
            continue
        try:
            head = open(fp, encoding="utf-8").read(500)
        except Exception:  # noqa: BLE001
            continue
        am = _PICKS_ANCHOR.search(head)
        if not am:
            continue
        for code in am.group(1).replace(" ", "").split(","):
            if _CODE_RE.match(code):
                rows.append((date, "md_blended", code))
    return pd.DataFrame(rows, columns=["date", "strategy", "code"])
