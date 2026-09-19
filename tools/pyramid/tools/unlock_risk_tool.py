"""unlock_risk（②消息·限售解禁）· 四因子 #1。

金字塔缺"解禁抛压"维度：临近大额限售解禁 = 潜在抛压 / 套现窗口，是明确的负向消息面。
本工具把 per-stock json 的 `financing.解禁`（未来次数/未来90日次数/未来90日占流通_pct/
下一次/明细/不可采信次数）结构化为"解禁嫌疑档 + 剩余天数 + 占流通比例"，供 D2 骨架排雷层
与 LLM 消费。设计依据：docs/计划/2026-09-18_四因子接入设计…md 的「因子二·解禁」整节。

数据源：data/analysis/<as_of>/<code>.json 的 financing.解禁（低频·月级·30天缓存·有解禁公告
时定点刷）。缺 financing.解禁 → missing 不编。档位写死 + 语义锁测试。

防未来（公理·硬闸）：解禁未来日期本身合法（"已披露的未来安排"，非未来价格），但只用
as_of 当天可知的信息——
  · 剩余天数 = 下一次.解禁日 − as_of 自算（不信采集时相对的 `距今日`，as_of≠采集日会偏）；
  · 下一次.披露日 > as_of（as_of 时尚未披露）→ 视为不可见，不计入；
  · 下一次.解禁日 ≤ as_of（相对 as_of 已解禁）→ 无前视临近；
  · 绝不读解禁前后价格（em 的解禁后20日涨跌等前视列本工具不碰）。

不确定点（诚实标注·可复盘调）：
  · 占比口径用 `未来90日占流通_pct`（采集时的 90 日窗聚合，已按「占比口径」剔除不可采信条），
    与 `下一次` 的剩余天数并用属轻度错配（聚合可能由窗内较晚的大额批次主导）；缺该聚合则
    退回 下一次.占流通市值_pct → 下一次.占总股本_pct → 全缺则保守判"低"。理由：设计文档把
    该聚合定为占比主口径，且它已做不可采信护栏，工程最省且防未来。
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

# ── 档位阈值（写死·语义锁测试锁死）──────────────────────
_占比_低线 = 3.0    # 占流通 < 3% → 低
_占比_高线 = 10.0   # 占流通 ≥ 10% → 高危候选（大额解禁业界粗线）
_临近_天 = 30       # 剩余 ≤ 30 日算临近（套现窗口临近）
_窗口_天 = 90       # 90 日窗

# ── v2 影响模板（解禁嫌疑档→对选股影响；共性"占比线/临近天数"已进统一词表）──
_解禁影响 = {
    "高危": "大额+临近解禁、机械抛压大、减分(veto候选)",
    "中": "解禁抛压、关注",
    "低": "解禁抛压小",
    "无嫌疑": "无临近解禁抛压、筹码供给面友好",
}


def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _parse_date(s):
    if not s:
        return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _unlock_verdict(占比: Optional[float], 剩余天数: Optional[int],
                    ratio_missing: bool) -> str:
    """解禁嫌疑档（写死）：无嫌疑 / 低 / 中 / 高危。仅在"有可见临近解禁"时调用。

    占比 = 90日窗占流通%（percent 口径）；剩余天数 = 下一次解禁距 as_of 自然日。
      占比不可知(ratio_missing)                → 低（保守·有临近但占比缺）
      占比 < 3%                                → 低
      3% ≤ 占比 < 10%  且 剩余 ≤ 30 日          → 中（占比中·临近）
      3% ≤ 占比 < 10%  且 剩余 > 30 日          → 低（占比中·尚远）
      占比 ≥ 10%       且 剩余 ≤ 30 日          → 高危（大额·临近·veto 候选）
      占比 ≥ 10%       且 剩余 > 30 日          → 中（大额·尚远）
    """
    if ratio_missing or 占比 is None:
        return "低"
    d = 999 if 剩余天数 is None else 剩余天数
    if 占比 < _占比_低线:
        return "低"
    if 占比 < _占比_高线:
        return "中" if d <= _临近_天 else "低"
    return "高危" if d <= _临近_天 else "中"


class UnlockRiskTool:
    name = "unlock_risk"
    塔层 = "②消息"
    面 = "资金面"  # 限售解禁=机械筹码供给→资金面·筹码供给（统筹裁决 2026-09-19）
    source = "per-stock json financing.解禁（未来90日次数/占流通pct/下一次{解禁日,披露日,占比}/不可采信次数）"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("unlock_risk 需 --code")
        path = _pstock_path(root, as_of, code)
        解禁 = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    dd = json.load(f)
                fin = dd.get("financing") if isinstance(dd, dict) else None
                解禁 = fin.get("解禁") if isinstance(fin, dict) else None
            except Exception:
                解禁 = None
        if not isinstance(解禁, dict):
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="解禁风险: 数据缺失（无 financing.解禁 字段·不编造）",
                fields={"解禁嫌疑档": "missing", "剩余天数": None, "占流通pct": None,
                        "下一次日期": None, "不可采信": None, "未来90日次数": None},
                freshness="missing", 防未来=True, source=self.source,
            )

        as_of_d = _parse_date(as_of)
        未来次数 = int(解禁.get("未来次数") or 0)
        未来90日次数 = int(解禁.get("未来90日次数") or 0)
        不可采信 = int(解禁.get("不可采信次数") or 0)
        下一次 = 解禁.get("下一次")

        # ── 判"有无可见临近解禁"（防未来）────────────────
        无临近 = (未来90日次数 <= 0) or (not isinstance(下一次, dict))
        剩余天数: Optional[int] = None
        下一次日期: Optional[str] = None
        if isinstance(下一次, dict):
            下一次日期 = (str(下一次.get("解禁日") or "")[:10]) or None
            披露日 = _parse_date(下一次.get("披露日"))
            解禁日 = _parse_date(下一次日期)
            if 解禁日 is None:
                无临近 = True  # 解禁日缺/格式异常 → 无法确认时点 → 当无临近(容错·不抛错)
            if 披露日 and as_of_d and 披露日 > as_of_d:
                无临近 = True  # as_of 时尚未披露 → 不可见
            if 解禁日 and as_of_d:
                剩余天数 = (解禁日 - as_of_d).days
                if 剩余天数 < 0:
                    无临近 = True  # 相对 as_of 已解禁 → 无前视临近

        采信注 = f"　含不可采信批次{不可采信}条(口径存疑·已剔出占比)" if 不可采信 > 0 else ""

        if 无临近:
            lines = [
                f"解禁嫌疑: 无嫌疑（90日内无可见临近解禁·不扣分）影响：{_解禁影响['无嫌疑']}",
                f"未来次数={未来次数}　未来90日次数={未来90日次数}　"
                + (f"下一次={下一次日期}(超窗/已过/未披露)" if 下一次日期 else "下一次=无") + 采信注,
            ]
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块=浓缩块(lines),
                fields={"解禁嫌疑档": "无嫌疑", "剩余天数": 剩余天数, "占流通pct": None,
                        "下一次日期": 下一次日期, "不可采信": 不可采信,
                        "未来90日次数": 未来90日次数},
                freshness="fresh", 防未来=True, source=self.source,
            )

        # ── 有可见临近解禁：定占比 + 出档 ────────────────
        占比 = 解禁.get("未来90日占流通_pct")
        占比口径 = "未来90日占流通%"
        if 占比 is None and isinstance(下一次, dict):
            占比 = 下一次.get("占流通市值_pct")
            占比口径 = "下一次占流通市值%"
            if 占比 is None:
                占比 = 下一次.get("占总股本_pct")
                占比口径 = "下一次占总股本%(折算)"
        ratio_missing = 占比 is None
        占比f = float(占比) if isinstance(占比, (int, float)) else None
        level = _unlock_verdict(占比f, 剩余天数, ratio_missing)

        占比str = f"{占比f:.2f}%" if 占比f is not None else "缺(保守判低)"
        剩余str = f"{剩余天数}日" if 剩余天数 is not None else "未知"
        lines = [
            f"解禁嫌疑: {level}（下一次解禁 {下一次日期}·剩余 {剩余str}·占流通 {占比str}）影响：{_解禁影响.get(level, '')}",
            f"占比口径: {占比口径}　未来90日次数={未来90日次数}　未来次数={未来次数}" + 采信注,
        ]
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块=浓缩块(lines),
            fields={"解禁嫌疑档": level, "剩余天数": 剩余天数, "占流通pct": 占比f,
                    "下一次日期": 下一次日期, "不可采信": 不可采信,
                    "未来90日次数": 未来90日次数, "占比口径": 占比口径},
            freshness="fresh", 防未来=True, source=self.source,
        )


register(UnlockRiskTool())
