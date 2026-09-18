"""shared_pool_tool（①塔基·统一召回池 + 来源标签）· P1 窗1。

构建统一召回池并给**来源标签**。五来源**平权并集**（K线复核不给特权）：
  ① ≥2 策略命中并集（多策略命中闸门.json；缺则由 每日选股.json picks 的 ≥2 strategies 派生）
  ② council top（council/投票 json）
  ③ 板块 roster（sector_focus.json 重点板块池 → data/sector_roster/<板块>.json 成分）
  ④ Agent 主线（<date>_agent收盘_*.json 的 selections 推荐票）
  ⑤ 全A K线过闸（复用 tools/experimental/pyramid_select_v1 池扫描·in_pool 口径）

数据缺来源时标 missing 不编。给 code 时输出该票命中的来源标签。
"""
from __future__ import annotations

import glob
import json
import os
from collections import Counter
from typing import Optional

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

# 来源固定顺序（决定 5 来源分母与展示序）
_SOURCE_ORDER = ["多策略并集", "council", "板块roster", "agent主线", "K线过闸"]


# ── 各来源加载器：返回 set[code]（present）或 None（missing）──
def _analysis_dir(root: Optional[str], as_of: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of)


def _load_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _first_glob(adir: str, patterns) -> Optional[str]:
    for pat in patterns:
        hits = sorted(glob.glob(os.path.join(adir, pat)))
        if hits:
            return hits[0]
    return None


def _load_multi_strategy(adir: str) -> Optional[set]:
    """① ≥2 策略命中并集。优先专用闸门文件；缺则由 每日选股.json picks 的 ≥2 strategies 派生。"""
    p = _first_glob(adir, ["*多策略命中闸门*.json", "*多策略命中*.json"])
    if p:
        d = _load_json(p)
        codes = _extract_codes(d)
        return codes if codes else None
    # 派生兜底：每日选股 picks 中被 ≥2 策略命中的票
    p2 = os.path.join(adir, "每日选股.json")
    d2 = _load_json(p2)
    if isinstance(d2, dict) and isinstance(d2.get("picks"), list):
        out = {
            str(x["code"])
            for x in d2["picks"]
            if isinstance(x, dict) and x.get("code") and len(x.get("strategies") or []) >= 2
        }
        return out or None
    return None


def _load_council(adir: str) -> Optional[set]:
    """② council top。缺文件即 missing。"""
    p = _first_glob(adir, ["*council*.json", "*投票*.json", "*议会*.json"])
    if not p:
        return None
    codes = _extract_codes(_load_json(p))
    return codes or None


def _load_roster(root: Optional[str], adir: str) -> Optional[set]:
    """③ 板块 roster：sector_focus 重点板块池 → 各板块角色表成分并集。"""
    focus = _load_json(os.path.join(adir, "sector_focus.json"))
    if not isinstance(focus, dict):
        return None
    pool = focus.get("重点板块池")
    if not isinstance(pool, list):
        return None
    codes: set = set()
    base = data_root(root)
    for item in pool:
        rel = item.get("角色表") if isinstance(item, dict) else None
        if not rel:
            continue
        d = _load_json(os.path.join(base, rel))
        roles = d.get("roles") if isinstance(d, dict) else None
        if isinstance(roles, dict):
            for lst in roles.values():
                if isinstance(lst, list):
                    codes |= {str(x["code"]) for x in lst if isinstance(x, dict) and x.get("code")}
    return codes or None


def _load_agent(adir: str, as_of: str) -> Optional[set]:
    """④ Agent 主线：<date>_agent收盘_*.json 的 selections 推荐票。"""
    files = sorted(glob.glob(os.path.join(adir, "*agent收盘*.json")))
    if not files:
        return None
    codes: set = set()
    for f in files:
        d = _load_json(f)
        sels = d.get("selections") if isinstance(d, dict) else None
        if isinstance(sels, list):
            codes |= {str(x["code"]) for x in sels if isinstance(x, dict) and x.get("code")}
    return codes or None


def _scan_kline(root: Optional[str], as_of: str) -> Optional[set]:
    """⑤ 全A K线过闸：复用 pyramid_select_v1 的 metrics/in_pool 口径（in_pool=召回池）。"""
    try:
        import pandas as pd
        from tools.experimental.pyramid_select_v1 import metrics, in_pool, THRESH
    except Exception:
        return None
    kdir = os.path.join(data_root(root), "data", "master", "kline")
    if not os.path.isdir(kdir):
        return None
    asof = pd.Timestamp(as_of)
    th = dict(THRESH)
    pool: set = set()
    for f in glob.glob(os.path.join(kdir, "*.parquet")):
        code = os.path.basename(f)[:6]
        if not code.startswith(("00", "30", "60", "68")):
            continue
        try:
            df = pd.read_parquet(
                f, columns=["date", "open", "high", "low", "close", "volume", "amount"]
            )
        except Exception:
            continue
        m = metrics(df, code, asof, th)
        if "skip" in m:
            continue
        if in_pool(m, th):
            pool.add(code)
    return pool


def _extract_codes(obj) -> set:
    """从任意召回 json 里稳健抽取 6 位股票代码（找 'code'/'代码' 键，或列表元素）。"""
    out: set = set()

    def walk(x):
        if isinstance(x, dict):
            for k in ("code", "代码", "股票代码"):
                v = x.get(k)
                if isinstance(v, (str, int)) and len(str(v)) >= 4:
                    out.add(str(v))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                if isinstance(v, (str, int)) and str(v).isdigit() and len(str(v)) == 6:
                    out.add(str(v))
                else:
                    walk(v)

    walk(obj)
    return out


def build_result(name: str, 塔层: str, source: str, as_of: str, code: Optional[str], raw: dict) -> ToolResult:
    """纯函数：把各来源结果（set 或 None）合成 ToolResult。语义锁测试直接锁这里（不跑重扫描）。"""
    present = {k: raw[k] for k in _SOURCE_ORDER if raw.get(k) is not None}
    missing = [k for k in _SOURCE_ORDER if raw.get(k) is None]
    union: set = set().union(*present.values()) if present else set()
    cnt: Counter = Counter()
    for s in present.values():
        cnt.update(s)
    共识 = [c for c, n in cnt.items() if n >= 2]  # ≥2 来源命中 = 交叉共识
    命中数 = {k: len(v) for k, v in present.items()}

    freshness = "fresh" if present else "missing"
    hit_line = " ".join(f"{k}={len(v)}" for k, v in present.items()) or "无present来源"
    lines = [
        f"池规模: {len(union)}票（{len(present)}/5来源平权并集·K线复核不给特权）",
        f"来源命中: {hit_line}【各来源去重后计数】",
        f"缺来源: {'/'.join(missing) if missing else '无'}【标missing不编】",
        f"≥2来源共识: {len(共识)}票【多来源交叉·可信度更高】",
    ]
    fields = {
        "池规模": len(union),
        "各来源命中数": 命中数,
        "缺来源": missing,
        "共识数": len(共识),
    }
    if code:
        code = str(code)
        labels = [k for k, v in present.items() if code in v]
        fields["查询票"] = code
        fields["命中来源"] = labels
        lines.append(
            f"{code}: 命中来源={'/'.join(labels) if labels else '无'}（{len(labels)}/{len(present)}来源）"
        )
    lines.append("口径: 平权并集·防未来as_of·缺来源标missing")

    return ToolResult(
        name=name,
        塔层=塔层,
        as_of=as_of,
        code=code,
        浓缩块=浓缩块(lines),
        fields=fields,
        freshness=freshness,
        防未来=True,
        source=source,
    )


def build_raw(as_of: str, root: Optional[str] = None, scan_kline: bool = True) -> dict:
    """构建 5 来源原始 set（present=set[code] / missing=None）。SSOT：run 与 D2 骨架共用。"""
    adir = _analysis_dir(root, as_of)
    return {
        "多策略并集": _load_multi_strategy(adir),
        "council": _load_council(adir),
        "板块roster": _load_roster(root, adir),
        "agent主线": _load_agent(adir, as_of),
        "K线过闸": _scan_kline(root, as_of) if scan_kline else None,
    }


def pool_with_labels(
    as_of: str, root: Optional[str] = None, scan_kline: bool = True
) -> dict[str, list]:
    """召回池成员 → {code: [命中来源标签...]}。供 D2 骨架遍历池；缺来源不计入分母。"""
    raw = build_raw(as_of, root=root, scan_kline=scan_kline)
    present = {k: raw[k] for k in _SOURCE_ORDER if raw.get(k) is not None}
    union: set = set().union(*present.values()) if present else set()
    return {
        str(c): [k for k, v in present.items() if c in v] for c in union
    }


class SharedPoolTool:
    name = "shared_pool"
    塔层 = "①塔基"
    source = "5来源平权并集: 多策略∪council∪板块roster∪Agent主线∪全A K线过闸"

    def run(
        self,
        as_of: str,
        code: Optional[str] = None,
        root: Optional[str] = None,
        scan_kline: bool = True,
        **kw,
    ) -> ToolResult:
        raw = build_raw(as_of, root=root, scan_kline=scan_kline)
        return build_result(self.name, self.塔层, self.source, as_of, code, raw)


register(SharedPoolTool())
