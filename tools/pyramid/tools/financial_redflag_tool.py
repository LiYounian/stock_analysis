"""financial_redflag_tool（③塔身·财报排雷）· P2。

填补 9-17 三方分歧暴露的缺口：骨架无财报维度 → DeepSeek 顺高量价分买了财报评级差的
002025，Claude 靠经验补雷。本工具把 per-stock json 的 `financial` 字段（quality_score/
评级/five_dims/flags）结构化为"财报排雷嫌疑档 + 判据"，供 D2 骨架排雷层与 LLM 消费。

数据源：data/analysis/<as_of>/<code>.json 的 financial（慢变字段，H1 午盘版不影响）。
缺 financial → missing 不编。档位写死 + 语义锁测试。
"""
from __future__ import annotations

from typing import Optional
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 格档, 浓缩块

# quality_score 档位（D1 §4：80/65/50/35 标定）
_QUALITY档 = [
    (35.0, "差", "财报质量差·排雷优先"),
    (50.0, "弱", "财报质量偏弱·警惕"),
    (65.0, "中", "财报质量中等"),
    (80.0, "良", "财报质量良好"),
    (100.0, "优", "财报质量优秀"),
]
# five_dims 单维弱项阈值（<该值算弱项，写死）
_DIM弱线 = 40.0

# ── v2 影响模板（排雷嫌疑档→对选股影响；共性"quality/五维是什么"已进统一词表）──
_排雷影响 = {
    "高危": "财报面重大减分、优先排雷", "中": "财报面减分、需警惕",
    "低": "财报面轻度关注", "无嫌疑": "财报面友好、无红旗",
}


def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _redflag_verdict(评级: Optional[str], quality: Optional[float], flags: list,
                     flags_detail: list, derived: dict) -> tuple[str, list]:
    """排雷嫌疑档 + 判据命中。档位：高危/中/低/无嫌疑（写死）。"""
    reasons = []
    高危flag = [f for f in (flags_detail or [])
               if isinstance(f, dict) and f.get("严重度") == "高"]
    中危flag = [f for f in (flags_detail or [])
               if isinstance(f, dict) and f.get("严重度") == "中"]
    扣非 = derived.get("扣非净利增速")
    扣非为负 = isinstance(扣非, (int, float)) and 扣非 < 0
    if 扣非为负:
        reasons.append(f"扣非净利增速为负({扣非:.1f}%)")
    for f in 高危flag:
        reasons.append(f"高危:{f.get('code')}")
    for f in 中危flag:
        reasons.append(f"中危:{f.get('code')}")
    q_low = isinstance(quality, (int, float)) and quality < 50
    # A4 语义锁：财报评级"风险"严重度 ≥ "差"（实测 quality 0~61），一律高危——
    #    修此前"风险"仅落"中/低"、比"差"还轻的严重度倒挂。
    if 评级 == "风险":
        reasons.append(f"评级风险(quality={quality})")
    # 分档
    if 评级 == "风险":
        level = "高危"
    elif 评级 == "差" and (q_low or 高危flag or 扣非为负):
        level = "高危"
    elif 高危flag or (评级 == "差") or 扣非为负:
        level = "中"
    elif 中危flag or q_low or 评级 == "中":
        level = "低"
    else:
        level = "无嫌疑"
    return level, reasons


class FinancialRedflagTool:
    name = "financial_redflag"
    塔层 = "③塔身"
    面 = "基本面"  # 财报盈利质量/评级
    source = "per-stock json financial（quality_score/评级/five_dims/flags/derived）"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("financial_redflag 需 --code")
        path = _pstock_path(root, as_of, code)
        fin = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    fin = (json.load(f) or {}).get("financial")
            except Exception:
                fin = None
        if not fin:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="财报排雷: 数据缺失（无 financial 字段·不编造）",
                fields={"数据缺": True, "嫌疑档": "missing"},
                freshness="missing", 防未来=True, source=self.source,
            )
        评级 = fin.get("评级")
        quality = fin.get("quality_score")
        q档, q解 = 格档(quality, _QUALITY档)
        dims = fin.get("five_dims") or {}
        弱项 = [k for k, v in dims.items() if isinstance(v, (int, float)) and v < _DIM弱线]
        flags = fin.get("flags") or []
        flags_detail = fin.get("flags_detail") or []
        derived = fin.get("derived") or {}
        level, reasons = _redflag_verdict(评级, quality, flags, flags_detail, derived)
        报告期 = fin.get("报告期")
        is_forecast = fin.get("is_forecast")

        lines = [
            f"财报排雷嫌疑: {level}【{level}】影响："
            + (f"{'; '.join(reasons)}·{_排雷影响.get(level, '')}" if reasons
               else _排雷影响.get(level, "无红旗判据")),
            f"评级/quality: {评级}·quality{quality}【{q档}】"
            + ("（预告口径）" if is_forecast else f"（{报告期}）")
            + f" 影响：财报质量{q档}",
            f"five_dims弱项(<{int(_DIM弱线)}): {'/'.join(弱项) if 弱项 else '无'}"
            + f"　[成长{dims.get('成长')}/质量{dims.get('质量')}/健康{dims.get('健康')}/运营{dims.get('运营')}/回报{dims.get('回报')}]",
            f"flags: {'/'.join(flags) if flags else '无'}",
        ]
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块=浓缩块(lines),
            fields={
                "嫌疑档": level, "判据": reasons, "评级": 评级,
                "quality_score": quality, "quality档": q档,
                "five_dims": dims, "弱项维度": 弱项, "flags": flags,
                "报告期": 报告期, "is_forecast": is_forecast,
            },
            freshness="fresh", 防未来=True, source=self.source,
        )


register(FinancialRedflagTool())
