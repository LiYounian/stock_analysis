"""growth_quality_tool（③塔身·基本面·成长盈利+财报明细）· Wave2。

读 per-stock json 的 `fundamental`（营收/净利/增速/ROE/毛利率/净利率/负债率/每股股利，原始值）
+ `financial`（评级/quality_score/five_dims/利润表摘要/报告期），产出四面契约的
**基本面·成长盈利**条目：营收 / 净利 / 盈利能力 / 负债率 / 每股股利 / 财报质量 / 财报明细。

口径贯通（设计 2026-09-19 §1b/§2c/§4.2）：
- 增速档（营收/净利共用）、负债率档、毛利率/净利率档 来自本文件档位表常量，拼装层零加工。
- 毛利率/净利率是**跨行业粗参考**——精确需行业分位(B期)，口径明标不当绝对标准。
- **ROE 是报告期 ROE(未年化)**：报告期长度(Q1/半年/三季/年报)天然不可比，故**不设年化绝对档**，
  原始值 + "未年化"口径 + 强弱挂 `financial.five_dims.回报`(该维已按口径标定)。方案(a)·统筹拍板 2026-09-19。
- 财报 quality 复用 `financial_redflag._QUALITY档`(单一口径源,不另立)；five_dims/利润表摘要/增速
  保字段明细，**不重出排雷嫌疑**(那是 financial_redflag 的活,避免双读冲突)。

缺 fundamental → missing 不编。档位写死 + 语义锁测试。研究模拟，非投资建议。
"""
from __future__ import annotations

from typing import Optional
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 格档, 字段
from tools.pyramid.tools.financial_redflag_tool import _QUALITY档  # 单一口径源·不另立

# ── 增速档（营收/净利共用，单位 %）──
_增速档 = [
    (-20.0, "衰退", "增速≤-20%·大幅下滑"),
    (0.0, "下滑", "增速∈(-20,0]·负增长"),
    (15.0, "平稳", "增速∈(0,15]·温和增长"),
    (30.0, "增长", "增速∈(15,30]·较快增长"),
    (float("inf"), "高增长", "增速>30%·高速增长"),
]
# ── 负债率档（金融/地产天然高，另标例外）──
_负债率档 = [
    (30.0, "低", "负债率≤30%·杠杆很轻"),
    (50.0, "中", "负债率∈(30,50]·适中"),
    (70.0, "偏高", "负债率∈(50,70]·偏高"),
    (float("inf"), "高", "负债率>70%·高杠杆"),
]
# ── 毛利率档（跨行业粗参考·精确需行业分位 B期）──
_毛利率档 = [
    (20.0, "低", "毛利率≤20%(跨行业粗参考)"),
    (40.0, "中", "毛利率∈(20,40](跨行业粗参考)"),
    (60.0, "高", "毛利率∈(40,60](跨行业粗参考)"),
    (float("inf"), "很高", "毛利率>60%(跨行业粗参考)"),
]
# ── 净利率档（跨行业粗参考）──
_净利率档 = [
    (5.0, "薄", "净利率≤5%(跨行业粗参考)"),
    (15.0, "中", "净利率∈(5,15](跨行业粗参考)"),
    (30.0, "厚", "净利率∈(15,30](跨行业粗参考)"),
    (float("inf"), "很厚", "净利率>30%(跨行业粗参考)"),
]


def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _报告类型(报告期) -> str:
    """从报告期 'YYYYMMDD'/'YYYY-MM-DD' 推报告类型（未年化提示用）。识别不出返回 ''。"""
    s = str(报告期 or "").replace("-", "")
    if len(s) >= 8:
        return {"0331": "一季报", "0630": "半年报",
                "0930": "三季报", "1231": "年报"}.get(s[4:8], "")
    return ""


def _亿(v) -> Optional[float]:
    """元 → 亿元（保留原精度，None 透传）。"""
    if isinstance(v, (int, float)):
        return round(v / 1e8, 2)
    return None


def _增速文(名: str, 金额亿: Optional[float], 增速) -> dict:
    """营收/净利一条：值='X亿 增速+Y%'，档来自 _增速档，缺则 NA。"""
    if isinstance(增速, (int, float)):
        档, 解 = 格档(float(增速), _增速档)
        额 = f"{金额亿}亿" if 金额亿 is not None else "NA"
        return 字段(名, f"{额} 增速{增速:+.2f}%", f"{档}·{解}",
                   f"{名}{档}" if 金额亿 is not None else f"{名}{档}(额缺)")
    额 = f"{金额亿}亿" if 金额亿 is not None else None
    return 字段(名, 额, "增速缺失", f"无{名}增速")


class GrowthQualityTool:
    name = "growth_quality"
    塔层 = "③塔身"
    面 = "基本面"  # 成长盈利 + 财报明细
    source = "per-stock json fundamental(营收/净利/增速/ROE/毛利/净利率/负债率/股利) + financial(评级/quality/five_dims/利润表摘要)"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("growth_quality 需 --code")
        path = _pstock_path(root, as_of, code)
        doc = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f) or {}
            except Exception:
                doc = None
        fund = (doc or {}).get("fundamental")
        if not fund:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="成长盈利: 数据缺失（无 fundamental 字段·不编造）",
                fields={"数据缺": True}, freshness="missing", 防未来=True,
                source=self.source, 面=self.面,
            )
        fin = (doc or {}).get("financial") or {}
        val = (doc or {}).get("valuation") or {}

        items = []

        # ── 营收 / 净利（各带增速档）──
        items.append(_增速文("营收", _亿(fund.get("营收")), fund.get("营收增速")))
        items.append(_增速文("净利", _亿(fund.get("净利")), fund.get("净利增速")))

        # ── 盈利能力：ROE(报告期未年化) + 毛利率 + 净利率（合一，强弱挂 five_dims.回报）──
        roe = fund.get("ROE")
        gm = fund.get("毛利率")
        nm = fund.get("净利率")
        dims = fin.get("five_dims") or {}
        回报维 = dims.get("回报")
        roe报告期 = val.get("报告期") or fin.get("报告期")
        roe类型 = _报告类型(roe报告期) or "报告期"
        vals = []
        if isinstance(roe, (int, float)):
            vals.append(f"ROE{roe:.2f}")
        if isinstance(gm, (int, float)):
            gm档, _ = 格档(float(gm), _毛利率档)
            vals.append(f"毛利{gm:.2f}[{gm档}]")
        if isinstance(nm, (int, float)):
            nm档, _ = 格档(float(nm), _净利率档)
            vals.append(f"净利率{nm:.2f}[{nm档}]")
        回报文 = f"回报维{回报维}/百" if isinstance(回报维, (int, float)) else "回报维NA"
        items.append(字段(
            "盈利能力", "/".join(vals) if vals else None,
            f"ROE={roe类型}未年化·不可比年化15%; 毛利/净利率跨行业粗参考",
            f"盈利回报强弱看 {回报文}(financial.five_dims 口径)",
        ))

        # ── 负债率（金融/地产例外）──
        负债 = fund.get("负债率")
        金融口径 = bool(fin.get("金融业口径"))
        if isinstance(负债, (int, float)):
            d档, d解 = 格档(float(负债), _负债率档)
            例外 = "·金融业口径(高杠杆属常态,勿套档)" if 金融口径 else ""
            items.append(字段("负债率", 负债, f"{d档}·{d解}{例外}",
                             "杠杆水平" + ("(金融业例外)" if 金融口径 else "")))
        else:
            items.append(字段("负债率", None, "负债率缺失", "无负债率数据"))

        # ── 每股股利（无档）──
        股利 = fund.get("每股股利")
        items.append(字段(
            "每股股利", 股利 if isinstance(股利, (int, float)) else None,
            "每股现金分红(元)·None=未分红/未披露",
            "分红回报" if isinstance(股利, (int, float)) else "无分红或未披露",
        ))

        # ── 财报质量（复用 _QUALITY档）──
        评级 = fin.get("评级")
        quality = fin.get("quality_score")
        报告期 = fin.get("报告期")
        报告类型 = fin.get("报告类型") or _报告类型(报告期)
        披露日 = fin.get("披露日")
        if isinstance(quality, (int, float)) or 评级:
            q档, q解 = 格档(quality, _QUALITY档) if isinstance(quality, (int, float)) else ("NA", "")
            items.append(字段(
                "财报质量", f"{评级}·quality{quality}",
                f"{q档}(80/65/50/35 档·复用 financial_redflag 口径)",
                f"报告期{报告期}·{报告类型}·披露{披露日}" + (f"·{q解}" if q解 else ""),
            ))
        else:
            items.append(字段("财报质量", None, "无 financial 评级/quality",
                             "该票未进深度财报采集"))

        # ── 财报明细（five_dims 五维 + 利润表摘要增速，保字段明细）──
        摘要 = fin.get("利润表摘要") or {}
        if dims or 摘要:
            五维 = (f"成长{dims.get('成长')}/质量{dims.get('质量')}/健康{dims.get('健康')}"
                   f"/运营{dims.get('运营')}/回报{dims.get('回报')}") if dims else "五维NA"
            增速文 = (f"归母{摘要.get('归母净利增速')}/扣非{摘要.get('扣非净利增速')}"
                    f"/营收{摘要.get('营收增速')}") if 摘要 else "增速NA"
            items.append(字段(
                "财报明细", f"五维[{五维}]",
                "five_dims 五维(0-100)+利润表增速%(报告期口径)",
                f"利润表增速 {增速文}",
            ))
        else:
            items.append(字段("财报明细", None, "无 five_dims/利润表摘要",
                             "该票未进深度财报采集"))

        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块="", 字段解读=items,
            fields={
                "营收": fund.get("营收"), "净利": fund.get("净利"),
                "营收增速": fund.get("营收增速"), "净利增速": fund.get("净利增速"),
                "ROE": roe, "毛利率": gm, "净利率": nm, "负债率": 负债,
                "每股股利": 股利, "金融业口径": 金融口径,
                "评级": 评级, "quality_score": quality, "报告期": 报告期,
                "报告类型": 报告类型, "披露日": 披露日,
                "five_dims": dims, "利润表摘要": 摘要, "回报维": 回报维,
            },
            freshness="fresh", 防未来=True, source=self.source, 面=self.面,
        )


register(GrowthQualityTool())
