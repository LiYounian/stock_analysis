"""insider_reduction（②消息·股东减持）· 四因子 #2。

金字塔缺"大股东减持抛压"维度：临近/正在进行的减持 = 套现抛压 + 内部人看空信号，
是明确的负向消息面。本工具镜像 fake_good_news：读 per-stock json 的 `events`，
取 as_of 前 RECENT_DAYS 内 type∈{减持,权益变动} 的公告，按标题关键词粗判"减持嫌疑档"，
供 D2 骨架排雷层与 LLM 消费。设计依据：docs/计划/2026-09-18_四因子接入设计…md「因子一·股东减持」。

数据源：data/analysis/<as_of>/<code>.json 的 `events`（announcement 采集器每日跑·约30天窗·
标题级打标 type=减持/权益变动/一致行动人，impact 对"减持"判利空）。缺 events → missing 不编。
⚠️ 只走 events(公告)：`holder`=股东户数(散户拥挤度)、`fundflow`=主力资金轴，均≠大股东减持，本工具不碰。
减持是低频事件：多数票 30 天窗候选为 0 属正常（不是缺数据）。

防未来（公理·硬闸）：候选公告 date ≤ as_of（只看 as_of 当日可知的已披露公告；不读任何价格）。

v1 仅标题级粗判（写死档位 + 语义锁测试）：
  候选 = type∈{减持,权益变动} 且 as_of−RECENT_DAYS ≤ date ≤ as_of。
  逐条按标题分严重度：中性(协议转让/引入战投/业务协同/增持/未减持 或 impact=利好)不计；
    高(清仓式 或 控股股东+大额)；中单条(大额/预披露)；否则常规。
  档：无计入候选→无嫌疑；含高→高(veto候选)；≥2条 或 含中单条→中；单条常规→低。

不确定点（诚实标注·可复盘调 v2）：
  · v1 只有标题，无减持规模/占流通%/主体层级/减持方式量化——档位靠关键词，会有粗判误差；
    v2 需新采(akshare stock_hold_change_cninfo/管理层明细)才能按占流通%×主体×方式精确定档。
  · "期限届满暨未减持""尚未减持"类公告按"未减持"中性化(不误杀)——它们表意是"没减"。
  · type=权益变动 且 impact=利好(如增持)按中性化剔除，避免把增持当减持扣分。
"""
from __future__ import annotations

from typing import Optional
from datetime import date
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

# ── 写死阈值 / 关键词（语义锁测试锁死）────────────────────
RECENT_DAYS = 30                      # "近期"减持公告窗口：as_of 前 30 个自然日内
_候选类型 = ("减持", "权益变动")        # events.type 落在此集合才是候选

# 中性化关键词：协议转让给战投/业务协同/增持/未减持 → 不计入减持（避免误杀）
_中性_KW = ("协议转让", "引入战投", "战略投资", "业务协同", "增持", "未减持")
# 高危关键词：清仓式；或 控股股东/实控人 + 大额（大额清仓式减持·veto 候选）
_控股_KW = ("控股股东", "实际控制人", "实控人")
# 中档单条触发词：大额 / 预披露减持计划
_中单条_KW = ("大额", "预披露")

# 档位枚举（语义锁死）
嫌疑档枚举 = ("无嫌疑", "低", "中", "高", "missing")

# 方式标签关键词（浓缩块用·非档位判据）
_方式_映射 = (
    ("清仓式", "清仓式减持"), ("大宗", "大宗交易"), ("集中竞价", "集中竞价"),
    ("协议转让", "协议转让(中性)"), ("预披露", "预披露计划"),
)
# 主体标签关键词
_主体_映射 = (
    ("控股股东", "控股股东"), ("实际控制人", "实控人"), ("实控人", "实控人"),
    ("董事", "董监高"), ("监事", "董监高"), ("高级管理人员", "董监高"), ("高管", "董监高"),
    ("5%", ">5%股东"),
)


def _load_events(code: str, as_of: str, root: Optional[str]) -> Optional[list]:
    """读 per-stock events；文件缺 / events 非列表 → None（missing，不编造减持）。"""
    p = os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    ev = d.get("events")
    return ev if isinstance(ev, list) else None


def _parse_date(s) -> Optional[date]:
    if not s:
        return None
    s = str(s)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        for sep in ("/",):
            try:
                y, m, dd = s.split(sep)
                return date(int(y), int(m), int(dd))
            except Exception:
                continue
    return None


def _candidates(events: list, as_of: str) -> list[dict]:
    """窗内候选：type∈{减持,权益变动} 且 as_of−RECENT_DAYS ≤ date ≤ as_of（防未来）。"""
    cut = _parse_date(as_of)
    if cut is None:
        return []
    out = []
    for e in events:
        if not isinstance(e, dict):
            continue
        if str(e.get("type") or "") not in _候选类型:
            continue
        d = _parse_date(e.get("date"))
        if d is None or d > cut or (cut - d).days > RECENT_DAYS:
            continue
        out.append(e)
    return out


def _severity(e: dict) -> str:
    """单条候选严重度：neutral（不计） / high / mid / normal。写死判据·语义锁。"""
    t = str(e.get("title") or "")
    if e.get("impact") == "利好":                       # 权益变动=增持类 → 中性化
        return "neutral"
    if any(k in t for k in _中性_KW):                    # 协议转让/增持/未减持等 → 中性化
        return "neutral"
    清仓 = "清仓式" in t or "清仓" in t
    大额 = "大额" in t
    控股 = any(k in t for k in _控股_KW)
    if 清仓 or (控股 and 大额):
        return "high"
    if 大额 or "预披露" in t:
        return "mid"
    return "normal"


def _verdict(sevs: list[str]) -> tuple[str, int]:
    """计入候选的严重度列表 → (减持嫌疑档, 命中条数)。仅在有 events 时调用。

    命中条数 = 非中性候选数 n。
      n == 0                    → 无嫌疑（无候选，或候选全被中性化）
      含 high                   → 高（veto 候选）
      n ≥ 2 或 含 mid           → 中
      否则（单条常规）          → 低
    """
    counted = [s for s in sevs if s != "neutral"]
    n = len(counted)
    if n == 0:
        return "无嫌疑", 0
    if "high" in counted:
        return "高", n
    if n >= 2 or "mid" in counted:
        return "中", n
    return "低", n


def _label(title: str, 映射) -> Optional[str]:
    for kw, lab in 映射:
        if kw in title:
            return lab
    return None


class InsiderReductionTool:
    name = "insider_reduction"
    塔层 = "②消息"
    面 = "基本面"  # 大股东减持=自主治理信号→基本面·公司治理（统筹裁决 2026-09-19）
    source = "per-stock json events（type∈{减持,权益变动}·标题级·announcement 采集·约30天窗）"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("insider_reduction 需 --code")
        events = _load_events(code, as_of, root)

        # events 缺失 → missing（绝不编造减持存在）
        if events is None:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="股东减持: 数据缺失（无 events 字段·不编造）",
                fields={"减持嫌疑档": "missing", "命中条数": None,
                        "最近减持日": None, "方式标签": None, "主体标签": None,
                        "窗内候选数": None},
                freshness="missing", 防未来=True, source=self.source,
            )

        cand = _candidates(events, as_of)
        sevs = [_severity(e) for e in cand]
        档, n = _verdict(sevs)
        n候选 = len(cand)

        # 无计入候选 → 无嫌疑（区分"窗内无减持类"与"全部中性化"）
        if 档 == "无嫌疑":
            if n候选 == 0:
                line2 = f"events 总{len(events)}条·窗内减持/权益变动 0 条（低频·属正常）"
            else:
                line2 = f"窗内 {n候选} 条减持类均为协议转让/增持/未减持等·中性化不计"
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块=浓缩块([f"股东减持嫌疑: 无嫌疑（近{RECENT_DAYS}日无计入减持公告）", line2]),
                fields={"减持嫌疑档": "无嫌疑", "命中条数": 0, "最近减持日": None,
                        "方式标签": None, "主体标签": None, "窗内候选数": n候选},
                freshness="fresh", 防未来=True, source=self.source,
            )

        # 有计入候选：取严重度最高（高>中>常规）、同级取最近日的那条做主样本
        _rank = {"high": 3, "mid": 2, "normal": 1, "neutral": 0}
        counted_idx = [i for i, s in enumerate(sevs) if s != "neutral"]
        主 = max(counted_idx, key=lambda i: (_rank[sevs[i]], _parse_date(cand[i].get("date")) or date.min))
        主标题 = str(cand[主].get("title") or "")
        方式 = _label(主标题, _方式_映射) or "减持计划/常规"
        主体 = _label(主标题, _主体_映射) or "一般股东/未标注"
        最近 = max((_parse_date(cand[i].get("date")) for i in counted_idx if _parse_date(cand[i].get("date"))),
                  default=None)
        最近str = 最近.isoformat() if 最近 else "未知"

        lines = [
            f"股东减持嫌疑: {档}（窗内计入 {n} 条·最近 {最近str}）",
            f"方式: {方式}　主体: {主体}",
            f"样本: {主标题[:34]}",
        ]
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
            fields={"减持嫌疑档": 档, "命中条数": n, "最近减持日": 最近str,
                    "方式标签": 方式, "主体标签": 主体, "窗内候选数": n候选},
            freshness="fresh", 防未来=True, source=self.source,
        )


register(InsiderReductionTool())
