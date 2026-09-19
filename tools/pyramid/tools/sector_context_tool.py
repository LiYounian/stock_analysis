"""sector_context_tool（④宏观·板块环境）· P1b 窗2 工具。

给定 as_of + code，产出该票所在**申万一级板块**的环境浓缩块（每票 6 字段）：
  1. 板块      — 申万一级（现状快照标签）+ 是否重点池
  2. 冷热拥挤  — sector_regime 冷热标签 + 拥挤档（档位写死）
  3. RS分位    — 个股 pos60（近60日区间分位，主档 K 线实测）
  4. 角色      — roster 龙头/中军/补涨先锋/弹性股（缺则无角色档）
  5. 板块内排名 — 该票在其板块角色池内按 5 日涨幅的排名（未进池则不可算）
  6. 净催化    — 板块净催化方向（重点池新闻净催化 / 消息驱动），档位 强正/正/中性/负/强负

数据铁律（与 _common 同源）：
  - 只信主档 K 线（pos60 走 load_kline，688/689 自校、as_of 防未来）。
  - 板块环境读 data/analysis/<date>/sector_focus.json + sector_regime.json，
    取 ≤as_of 的最近一日（as_of 无当日文件 → stale）。
  - code→申万一级 走 config/code_industry.json（单一真源，现状快照标签）。
  - 缺某字段一律标 missing / 无档，**绝不编**。
"""
from __future__ import annotations

from typing import Optional, Any
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, load_kline, pos60, 格档, 浓缩块

# ── 档位语义锁（测试须锁死这几张表）──────────────────────────────
# focus_score（板块综合关注度，0~1）·格档取 value ≤ 上界
_FS档 = [
    (0.5, "低", "<0.5 弱关注"),
    (0.7, "中", "0.5~0.7 中等关注"),
    (float("inf"), "高", "≥0.7 高关注"),
]
# 个股 pos60（近60日 [低,高] 区间分位，0~1）
_RS档 = [
    (0.2, "弱", "近60日低位"),
    (0.4, "偏弱", "近60日低区"),
    (0.6, "中", "近60日中区"),
    (0.8, "偏强", "近60日高区"),
    (float("inf"), "强", "近60日高位"),
]
# 净催化（新闻净催化数值，利好+/利空-）
_净催化档 = [
    (-50.0, "强负", "利空强"),
    (-0.0001, "负", "偏利空"),
    (0.0, "中性", "无净催化"),
    (49.99, "正", "偏利好"),
    (float("inf"), "强正", "利好强"),
]
# 拥挤档语义（读 regime 标签、锁其含义，不重算）
_拥挤含义 = {"A": "拥挤高位(分位≥0.5)", "B": "不拥挤(分位<0.5)"}
# 冷热标签语义（读 regime 标签、锁其含义）
_冷热含义 = {
    "过冷": "冷清",
    "正常活跃": "常态",
    "拐点": "冷热切换",
    "过热": "过热拥挤",
    "回落": "退潮",
}
# 消息驱动 tag×强弱 → 净催化档（重点池无净催化数值时的旁路）
_消息驱动档 = {
    ("利好", "强"): ("强正", "消息驱动利好强"),
    ("利好", "中"): ("正", "消息驱动利好中"),
    ("利空", "强"): ("强负", "消息驱动利空强"),
    ("利空", "中"): ("负", "消息驱动利空中"),
}
# roster 角色池搜索顺序（sector_focus 角色表指向 data/sector_roster/；roster_table 为补充源）
_ROSTER_DIRS = ("sector_roster", "sector_roster_table")


# ── 数据装配 ─────────────────────────────────────────────────
def _code_industry(root: Optional[str]) -> dict:
    """读 config/code_industry.json（code→申万一级，单一真源、现状快照）。缺失返回 {}。"""
    p = os.path.join(data_root(root), "config", "code_industry.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _latest_analysis(as_of: str, fname: str, root: Optional[str]) -> tuple[Optional[dict], Optional[str]]:
    """取 data/analysis/<date>/<fname> 中 ≤as_of 的最近一日；返回 (内容, 命中日期)。"""
    base = os.path.join(data_root(root), "data", "analysis")
    if not os.path.isdir(base):
        return None, None
    dates = []
    for d in os.listdir(base):
        # 只认 YYYY-MM-DD 目录，防未来（≤as_of）
        if len(d) == 10 and d[4] == "-" and d[7] == "-" and d <= as_of:
            if os.path.exists(os.path.join(base, d, fname)):
                dates.append(d)
    if not dates:
        return None, None
    hit = max(dates)
    try:
        with open(os.path.join(base, hit, fname), "r", encoding="utf-8") as f:
            return json.load(f), hit
    except Exception:
        return None, hit


def _find_role(sector: str, code: str, root: Optional[str]) -> tuple[Optional[str], Optional[str], list]:
    """在 sector 的 roster 里找 code 的角色。

    返回 (角色名, 选级, 全角色池条目list)；roster 文件不存在 → (None, None, [])；
    存在但未含 code → (None, None, 全池)（供上层区分"无roster"与"未进池"）。
    """
    for sub in _ROSTER_DIRS:
        p = os.path.join(data_root(root), "data", sub, f"{sector}.json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                r = json.load(f)
        except Exception:
            continue
        roles = r.get("roles") or {}
        pool = []  # [(role, entry)]
        for role, lst in roles.items():
            if isinstance(lst, list):
                for e in lst:
                    pool.append((role, e))
        for role, e in pool:
            if str(e.get("code")) == str(code):
                return role, e.get("选级"), pool
        return None, None, pool  # roster 有但未含该票
    return None, None, []  # 无 roster 文件


def _rank_in_pool(code: str, pool: list) -> Optional[str]:
    """该票在角色池内按 5 日涨幅降序的排名字符串；未进池或字段缺 → None。"""
    scored = [
        (str(e.get("code")), e.get("涨幅5日"))
        for _, e in pool
        if isinstance(e.get("涨幅5日"), (int, float))
    ]
    if not any(c == str(code) for c, _ in scored):
        return None
    scored.sort(key=lambda x: x[1], reverse=True)
    for i, (c, _) in enumerate(scored, 1):
        if c == str(code):
            return f"第{i}/{len(scored)}(角色池·按5日涨幅)"
    return None


def _grade_净催化(
    net: Optional[float], in_focus: bool, in_avoid: bool, msg: Optional[tuple]
) -> tuple[str, str]:
    """净催化档：规避池优先压负；有净催化数值走数值档；否则走消息驱动旁路；都无→中性。"""
    if in_avoid:
        return "负", "在规避板块池"
    if isinstance(net, (int, float)):
        return 格档(float(net), _净催化档)
    if msg is not None:
        return _消息驱动档.get(msg, ("中性", "消息驱动信号弱"))
    return "中性", "非重点池·无消息驱动信号"


# ── 工具 ─────────────────────────────────────────────────────
class SectorContextTool:
    name = "sector_context"
    塔层 = "④宏观"
    面 = "消息情绪面"  # 板块轮动/净催化/角色/过热拥挤
    source = "code_industry(申万一级) + sector_focus/regime + roster + 主档K线pos60"

    def run(self, as_of: str, code: Optional[str] = None, root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("sector_context 需 --code")
        code = str(code)

        # 1) code → 申万一级
        sector = _code_industry(root).get(code)
        if not sector:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="板块: missing（code_industry 未含该票·无法解析申万一级）·其余字段无从判",
                fields={"板块": None, "数据缺": True},
                freshness="missing", 防未来=True, source=self.source,
            )

        # 2) 板块环境（≤as_of 最近一日）
        focus, focus_date = _latest_analysis(as_of, "sector_focus.json", root)
        regime, regime_date = _latest_analysis(as_of, "sector_regime.json", root)
        hit_date = focus_date or regime_date
        freshness = "fresh" if hit_date == as_of else ("stale" if hit_date else "missing")

        focus_entry, avoid_hit = None, False
        if isinstance(focus, dict):
            for s in focus.get("重点板块池") or []:
                if s.get("板块") == sector:
                    focus_entry = s
                    break
            avoid_hit = any((s.get("板块") == sector) for s in (focus.get("规避板块池") or []))
        regime_entry = None
        if isinstance(regime, dict):
            for s in regime.get("板块") or []:
                if s.get("板块") == sector:
                    regime_entry = s
                    break
        # 消息驱动 tag（重点池无净催化数值时旁路）
        msg = None
        if isinstance(focus, dict):
            md = focus.get("消息驱动") or {}
            for key, sign in (("利好板块", "利好"), ("利空板块", "利空")):
                for b in md.get(key) or []:
                    if b.get("board") == sector:
                        msg = (sign, b.get("强弱"))
                        break
                if msg:
                    break

        # ── 字段渲染 ──
        # 1 板块
        重点标 = "重点池" if focus_entry is not None else ("规避池" if avoid_hit else "非重点池")
        fs = focus_entry.get("focus_score") if focus_entry else None
        fs档, _ = 格档(float(fs), _FS档) if isinstance(fs, (int, float)) else ("无档", "")
        fs_txt = f" focus{fs:.2f}[{fs档}]" if isinstance(fs, (int, float)) else ""
        line_板块 = f"板块: {sector}（申万一级·现状快照·{重点标}）{fs_txt}"

        # 2 冷热拥挤
        冷热 = (regime_entry or {}).get("冷热标签") or (focus_entry or {}).get("冷热")
        拥挤 = (regime_entry or {}).get("拥挤档") or (focus_entry or {}).get("拥挤档")
        if 冷热 or 拥挤:
            冷释 = _冷热含义.get(冷热, "")
            拥释 = _拥挤含义.get(拥挤, "")
            line_冷热 = (
                f"冷热拥挤: {冷热 or 'missing'}·拥挤{拥挤 or 'missing'}"
                f"影响：{'/'.join(x for x in (冷释, 拥释) if x) or '无档说明'}"
            )
        else:
            line_冷热 = "冷热拥挤: missing（regime/focus 无该板块）"

        # 3 RS分位（个股 pos60，主档 K 线实测）——共性"pos60是什么"进词表，此处只讲板块内相对位
        df = load_kline(code, as_of, root=root, min_bars=2)
        rs = pos60(df) if df is not None else None
        if rs is not None:
            rs档, _ = 格档(rs, _RS档)
            line_rs = f"RS分位: pos60={rs:.2f}【{rs档}】影响：板块内近60日相对位置{rs档}"
        else:
            line_rs = "RS分位: missing（主档K线不足/缺失）"

        # 4 角色
        role, 选级, pool = _find_role(sector, code, root)
        if role:
            line_角色 = f"角色: {role}" + (f"·{选级}" if 选级 else "") + "（板块roster）"
        elif pool:
            line_角色 = "角色: 无角色（该票未进角色池）"
        else:
            line_角色 = "角色: 无角色档（该板块无roster）"

        # 5 板块内排名
        rank = _rank_in_pool(code, pool)
        line_排名 = f"板块内排名: {rank}" if rank else "板块内排名: 无（未进角色池·全成分排名不可算）"

        # 6 净催化
        net = focus_entry.get("新闻净催化") if focus_entry else None
        利好条 = focus_entry.get("利好条") if focus_entry else None
        利空条 = focus_entry.get("利空条") if focus_entry else None
        净档, 净释 = _grade_净催化(net, focus_entry is not None, avoid_hit, msg)
        证据 = []
        if isinstance(net, (int, float)):
            证据.append(f"net{net:.0f}")
        if isinstance(利好条, int) and isinstance(利空条, int):
            证据.append(f"利好{利好条}/利空{利空条}")
        if msg:
            证据.append(f"消息驱动{msg[0]}{msg[1] or ''}")
        line_净催化 = f"净催化: {净档}[{'·'.join(证据) or 净释}]影响：{净释}"

        lines = [line_板块, line_冷热, line_rs, line_角色, line_排名, line_净催化]
        fields: dict[str, Any] = {
            "板块": sector,
            "重点标": 重点标,
            "focus_score": round(float(fs), 4) if isinstance(fs, (int, float)) else None,
            "focus_score档": fs档 if isinstance(fs, (int, float)) else None,
            "冷热标签": 冷热,
            "拥挤档": 拥挤,
            "RS_pos60": round(rs, 4) if rs is not None else None,
            "RS档": (格档(rs, _RS档)[0] if rs is not None else None),
            "角色": role,
            "选级": 选级,
            "板块内排名": rank,
            "新闻净催化": net if isinstance(net, (int, float)) else None,
            "净催化档": 净档,
            "环境命中日": hit_date,
        }
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块=浓缩块(lines), fields=fields,
            freshness=freshness, 防未来=True, source=self.source,
        )


register(SectorContextTool())
