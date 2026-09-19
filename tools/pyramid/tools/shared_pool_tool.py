"""shared_pool_tool（①塔基·统一召回池 + 来源标签）· P1 窗1。

构建统一召回池并给**来源标签**。四来源**平权并集**（K线复核不给特权）：
  ① ≥2 策略命中并集（多策略命中闸门.json；缺则由 每日选股.json picks 的 ≥2 strategies 派生）
  ② council top（council/投票 json）
  ③ 板块 roster（sector_focus.json 重点板块池 → data/sector_roster/<板块>.json 成分）
  ④ 全A K线过闸（复用 tools/experimental/pyramid_select_v1 池扫描·in_pool 口径）

金字塔选股是独立流程，召回层不消费他人成品选股票——原「Agent 主线」一路
（<date>_agent收盘_*.json）已移除：其产出方在 tools/experimental/、非定时任务、覆盖
极稀疏，且与「全A K线过闸」coverage 重叠。experimental/ 的 agent 脚本照常产文件，
只是金字塔不再消费。

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

# 来源固定顺序（决定四来源分母与展示序）
_SOURCE_ORDER = ["多策略并集", "council", "板块roster", "K线过闸"]


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


# 各策略 view json 文件名（SSOT：run._screener_view 的落盘名）。当日缺某策略 → glob/exists 自然略过。
_STRATEGY_VIEWS = [
    "策略0合议", "放量后缩量回踩", "动量组合", "半导体多因子",
    "最大范围选股", "量价放量", "最强选股", "反转低换手组合",
    "指标条件化状态排序", "扣非质量",
]


def _strategy_view_picks(d) -> set:
    """从单个策略 view json 抽选出票 code（兼容 入选清单/top/排行 三种落法，与 run._picks_from_view 同口径）。"""
    if not isinstance(d, dict):
        return set()
    items = d.get("入选清单") or d.get("top")
    if items:
        return {str(x["code"]) for x in items if isinstance(x, dict) and x.get("code")}
    rank = d.get("排行")
    if isinstance(rank, dict):
        out: set = set()
        for lst in rank.values():
            if isinstance(lst, list):
                for x in lst:
                    c = x.get("code") if isinstance(x, dict) else x
                    if isinstance(c, str) and c:
                        out.add(str(c))
        return out
    return set()


def _load_multi_strategy(adir: str) -> Optional[set]:
    """① ≥2 策略命中并集。

    A5 修退化：直接读各策略 view json（策略0合议/最大范围选股/量价放量/最强选股/…）计每票被
    ≥2 个策略命中的并集——这才是"多策略交叉"真口径。此前退回只读 每日选股.json picks 的
    ≥2 strategies（当日仅 4 只，漏掉 67 只），严重收缩池。
    仅当当日策略 view 文件不足 2 个（老日期/精简产物）时才回退：多策略命中闸门文件 → 每日选股 派生。
    """
    cnt: Counter = Counter()
    found = 0
    for name in _STRATEGY_VIEWS:
        picks = _strategy_view_picks(_load_json(os.path.join(adir, f"{name}.json")))
        if picks:
            found += 1
            cnt.update(picks)  # picks 已按策略内去重（单策略一票只计一次）
    if found >= 2:
        ge2 = {c for c, n in cnt.items() if n >= 2}
        return ge2 or None
    # 回退①：专用闸门文件
    p = _first_glob(adir, ["*多策略命中闸门*.json", "*多策略命中*.json"])
    if p:
        codes = _extract_codes(_load_json(p))
        return codes if codes else None
    # 回退②：每日选股 picks 中被 ≥2 策略命中的票
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


def _is_st(name: Optional[str]) -> bool:
    """名称判据：含 'ST'（含 *ST）或 '退' → ST/退市。与 pyramid_select_v1.hard_veto 同口径。"""
    return "ST" in (name or "").upper() or "退" in (name or "")


def _load_name_map(root: Optional[str]) -> dict:
    """code→name（config/code_name.json）。供召回层剔 ST/退市；缺则空 dict（宁可不剔也不误剔）。"""
    d = _load_json(os.path.join(data_root(root), "config", "code_name.json"))
    return d if isinstance(d, dict) else {}


def _scan_kline(root: Optional[str], as_of: str) -> Optional[set]:
    """④ 全A K线过闸：复用 pyramid_select_v1 的 metrics/in_pool 口径（in_pool=召回池）。

    A2 修：召回层按名称判据剔除 ST/*ST/退市（复用 config/code_name.json + _is_st），
    不让 ST 进池最干净——避免 D2 骨架/Agent 在池内买到 ST 票。
    """
    try:
        import pandas as pd
        from tools.experimental.pyramid_select_v1 import metrics, in_pool, THRESH
    except Exception:
        return None
    kdir = os.path.join(data_root(root), "data", "master", "kline")
    if not os.path.isdir(kdir):
        return None
    names = _load_name_map(root)
    asof = pd.Timestamp(as_of)
    th = dict(THRESH)
    pool: set = set()
    for f in glob.glob(os.path.join(kdir, "*.parquet")):
        code = os.path.basename(f)[:6]
        if not code.startswith(("00", "30", "60", "68")):
            continue
        if _is_st(names.get(code)):  # A2：ST/退市不入召回池
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
        f"池规模: {len(union)}票（{len(present)}/{len(_SOURCE_ORDER)}来源平权并集·K线复核不给特权）",
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
    """构建四来源原始 set（present=set[code] / missing=None）。SSOT：run 与 D2 骨架共用。"""
    adir = _analysis_dir(root, as_of)
    return {
        "多策略并集": _load_multi_strategy(adir),
        "council": _load_council(adir),
        "板块roster": _load_roster(root, adir),
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
    面 = "卡头"  # 召回来源/骨架元信息→卡头
    source = "4来源平权并集: 多策略∪council∪板块roster∪全A K线过闸"

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
