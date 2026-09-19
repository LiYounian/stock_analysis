"""valuation_tool（③塔身·基本面·估值）· Wave2。

读 per-stock json 的 `valuation`（pe_ttm/pb/mktcap_yi/pe_valid/mode/basis/口径提示）
+ `fundamental`/`financial`（PEG 现算取净利增速），产出四面契约的**基本面·估值**条目：
PE(TTM) / PB / PEG(现算) / PE历史分位 / 市值口径。

口径贯通：所有档位来自本文件档位表常量，拼装层零加工（设计 2026-09-19 §1b/§2c/§4.2）。
- PE 是**跨市场粗档·非行业调整**——真估值看 PEG + 行业分位(B期)；`pe_valid=False` 或
  `mode≠PE适用`(如金融股/亏损) 不套档，原样传 valuation.basis。
- PEG=pe_ttm÷净利增速(%)；增速优先 financial.利润表摘要.归母净利增速(报告期更新)，
  缺→fallback fundamental.净利增速，值段注明报告期；**增速≤0 → PEG 失效不套档**。
- PE 历史分位读 valuation.pe_percentile（0~1，现值在自身历史 PE 序列中的 ≤x 占比，
  窗口=pe_percentile_window，None=全历史）；缺字段 → 标"缺失"不编。

缺 valuation → missing 不编。档位写死 + 语义锁测试。研究模拟，非投资建议。
"""
from __future__ import annotations

from typing import Optional
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 格档, 字段

# ── PE(TTM) 参考档：跨市场粗档，非行业调整（全A横截面 n≈194 实测标定）──
_PE参考档 = [
    (0.0, "亏损", "PE≤0·盈利为负,PE失去估值意义"),
    (15.0, "低", "PE∈(0,15]·跨市场低估区(粗档)"),
    (30.0, "中", "PE∈(15,30]·跨市场合理区(粗档)"),
    (60.0, "偏高", "PE∈(30,60]·跨市场偏贵(粗档)"),
    (float("inf"), "高", "PE>60·跨市场高估(粗档,或高成长/主题溢价)"),
]
# ── PB 档 ──
_PB档 = [
    (1.0, "破净", "PB≤1·破净,市值低于净资产"),
    (2.0, "低", "PB∈(1,2]·净资产溢价温和"),
    (4.0, "中", "PB∈(2,4]·净资产溢价中等"),
    (8.0, "偏高", "PB∈(4,8]·溢价偏高"),
    (float("inf"), "高", "PB>8·高溢价"),
]
# ── PEG 现算档（Lynch 基准 PEG=1）──
_PEG档 = [
    (0.8, "低估", "PEG≤0.8·成长消化估值,便宜"),
    (1.2, "合理", "PEG∈(0.8,1.2]·估值与成长匹配(Lynch基准1)"),
    (2.0, "偏高", "PEG∈(1.2,2]·估值略超成长"),
    (float("inf"), "高估", "PEG>2·估值未被成长消化"),
]
# ── PE 历史分位档（现值在自身历史 PE 序列中的 ≤x 占比，0~1）──
_PE分位档 = [
    (0.20, "极低", "分位≤20%·自身历史估值底部区"),
    (0.40, "偏低", "分位∈(20%,40%]·低于自身多数时期"),
    (0.60, "中位", "分位∈(40%,60%]·自身估值中枢"),
    (0.80, "偏高", "分位∈(60%,80%]·高于自身多数时期"),
    (float("inf"), "极高", "分位>80%·逼近自身历史估值顶部区"),
]


def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _pick_growth(fund: dict, fin: dict) -> tuple[Optional[float], str]:
    """PEG 增速源：优先 financial.利润表摘要.归母净利增速(报告期更新)，缺→fundamental.净利增速。

    返回 (增速值%, 源标签含报告期)。两者皆缺 → (None, "增速缺失")。
    """
    摘要 = (fin or {}).get("利润表摘要") or {}
    g = 摘要.get("归母净利增速")
    if isinstance(g, (int, float)):
        return float(g), f"归母,{(fin or {}).get('报告期', '?')}"
    g2 = (fund or {}).get("净利增速")
    if isinstance(g2, (int, float)):
        # fundamental 与 valuation 同口径日期；报告期取 valuation.报告期由调用方补，这里只标来源
        return float(g2), "净利,fundamental口径"
    return None, "增速缺失"


class ValuationTool:
    name = "valuation"
    塔层 = "③塔身"
    面 = "基本面"  # 估值：PE/PB/PEG/市值
    source = "per-stock json valuation(pe_ttm/pb/mktcap_yi/pe_valid/mode/basis/口径提示) + fundamental/financial(PEG增速)"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("valuation 需 --code")
        path = _pstock_path(root, as_of, code)
        doc = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f) or {}
            except Exception:
                doc = None
        val = (doc or {}).get("valuation")
        if not val:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="估值: 数据缺失（无 valuation 字段·不编造）",
                fields={"数据缺": True}, freshness="missing", 防未来=True,
                source=self.source, 面=self.面,
            )
        fund = (doc or {}).get("fundamental") or {}
        fin = (doc or {}).get("financial") or {}

        pe = val.get("pe_ttm")
        pb = val.get("pb")
        mktcap = val.get("mktcap_yi")
        pe_valid = val.get("pe_valid")
        mode = val.get("mode")
        basis = val.get("basis") or "无 basis"
        口径提示 = val.get("口径提示") or "无口径提示"
        口径日期 = val.get("口径日期") or "?"
        报告期滞后 = bool(val.get("报告期滞后"))

        items = []

        # ── PE(TTM)：pe_valid & mode==PE适用 才套档，否则原样传 basis ──
        pe适用 = bool(pe_valid) and (mode == "PE适用")
        if pe适用 and isinstance(pe, (int, float)):
            档, 解 = 格档(pe, _PE参考档)
            items.append(字段(
                "PE(TTM)", pe, f"{档}·{解}",
                "跨市场粗档,真估值结合 PEG+行业分位(B期);" + basis,
            ))
        else:
            items.append(字段(
                "PE(TTM)", pe if isinstance(pe, (int, float)) else None,
                f"不适用(mode={mode},pe_valid={pe_valid})",
                "PE 口径不适用,看 PB/PEG;" + basis,
            ))

        # ── PB ──
        if isinstance(pb, (int, float)):
            档, 解 = 格档(pb, _PB档)
            items.append(字段("PB", pb, f"{档}·{解}", "市净率反映净资产溢价程度"))
        else:
            items.append(字段("PB", None, "PB 缺失", "无 PB 数据"))

        # ── PEG(现算) = pe_ttm ÷ 净利增速(%) ──
        增速, 增速源 = _pick_growth(fund, fin)
        if not (pe适用 and isinstance(pe, (int, float))):
            items.append(字段(
                "PEG(现算)", None, "PEG 不适用(PE 口径不适用)",
                "PE 不适用,PEG 无意义,看 PB",
            ))
        elif 增速 is None:
            items.append(字段(
                "PEG(现算)", None, "PEG 缺失(净利增速缺)", "无净利增速,无法现算 PEG",
            ))
        elif 增速 <= 0:
            items.append(字段(
                "PEG(现算)", "不适用",
                f"增速≤0·PEG失效(增速{增速:.1f}%,{增速源})",
                "净利下滑/亏损,PEG 无意义,估值需另判",
            ))
        else:
            peg = pe / 增速
            档, 解 = 格档(peg, _PEG档)
            items.append(字段(
                "PEG(现算)", round(peg, 2),
                f"{档}·pe{pe:.1f}÷增速{增速:.1f}%({增速源})",
                解,
            ))

        # ── PE 历史分位：从 valuation.pe_percentile 读（0~1=现值在自身历史 PE 序列中的 ≤x 占比）──
        pe分位 = val.get("pe_percentile")
        pe分位窗口 = val.get("pe_percentile_window")
        if isinstance(pe分位, (int, float)):
            档, 解 = 格档(pe分位, _PE分位档)
            窗口注 = f"窗口{pe分位窗口}" if pe分位窗口 else "窗口全历史"
            items.append(字段(
                "PE历史分位", round(pe分位 * 100, 1), f"{档}·{解}·{窗口注}",
                "自身历史估值区间对比,越低越接近自身估值底部",
            ))
        else:
            items.append(字段(
                "PE历史分位", None, "缺失(pe_percentile 未落盘或无历史 PE 序列)",
                "暂无自身历史估值区间对比",
            ))

        # ── 市值 / 估值口径(滞后新鲜度)──
        items.append(字段(
            "市值/估值口径", mktcap if isinstance(mktcap, (int, float)) else None,
            f"市值(亿)·口径日期{口径日期}·{'报告期滞后' if 报告期滞后 else '报告期最新'}",
            口径提示,
        ))

        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块="", 字段解读=items,
            fields={
                "pe_ttm": pe, "pb": pb, "mktcap_yi": mktcap,
                "pe_valid": pe_valid, "mode": mode, "basis": basis,
                "peg_增速": 增速, "peg_增速源": 增速源, "报告期滞后": 报告期滞后,
            },
            freshness="fresh", 防未来=True, source=self.source, 面=self.面,
        )


register(ValuationTool())
