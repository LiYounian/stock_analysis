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

# ── v2 影响模板（共性区间定义已进统一词表，此处只讲本股本值影响）──
_增速影响 = {
    "衰退": "大幅下滑、成长面重减分",
    "下滑": "负增长、成长面减分",
    "平稳": "温和增长、中性",
    "增长": "较快增长、成长面加分",
    "高增长": "爆发式增长、成长面强加分(注意低基数)",
}
_负债影响 = {
    "低": "杠杆很轻、财务稳健、加分",
    "中": "杠杆适中、中性",
    "偏高": "杠杆偏高、财务稳健性小幅减分",
    "高": "高杠杆、财务风险、减分",
}
# ── 财报五维逐维影响（按 _QUALITY档 档名分强/中/弱三档取句）──
_五维强弱句 = {
    "成长": ("营收利润增长强劲、成长动能强项", "成长温和、中性", "成长乏力、增长趋势弱"),
    "质量": ("盈利含金量高、扣非质量扎实", "盈利质量中等", "盈利含金量偏低、扣非质量存疑、短板"),
    "健康": ("资产负债/现金流安全", "财务健康中等", "资产负债/现金流承压"),
    "运营": ("周转与经营效率良好", "运营效率中等", "周转与经营效率偏弱"),
    "回报": ("资本回报高、赚钱效率强", "资本回报中等", "资本回报低、赚钱效率弱"),
}


def _维bucket(档: str) -> int:
    """_QUALITY档 档名 → 强弱句索引：优/良=0(强)，中=1，弱/差=2(弱)。"""
    return 0 if 档 in ("优", "良") else (1 if 档 == "中" else 2)


def _五维综合(dims: dict, q档: str) -> str:
    """从 five_dims 合成一句：最强维/最弱维 + 综合质量档（财报质量汇总·意味段）。"""
    有效 = {k: v for k, v in (dims or {}).items() if isinstance(v, (int, float))}
    if not 有效:
        return f"综合财报质量{q档}"
    强 = max(有效, key=有效.get)
    弱 = min(有效, key=有效.get)
    追高 = "、追高谨慎" if q档 in ("弱", "差", "中") and 有效.get("质量", 100) <= 50 else ""
    return f"{强}{有效[强]:g}最强、{弱}{有效[弱]:g}最弱，综合质量{q档}{追高}"


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
        档, _ = 格档(float(增速), _增速档)
        额 = f"{金额亿}亿" if 金额亿 is not None else "NA"
        return 字段(名, f"{额} 增速{增速:+.2f}%", 档,
                   f"影响：{_增速影响.get(档, '')}" + ("" if 金额亿 is not None else "(额缺)"))
    额 = f"{金额亿}亿" if 金额亿 is not None else None
    return 字段(名, 额, "增速缺失", f"影响：无{名}增速、成长不可判")


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
                source=self.source, 面=self.面, max_浓缩块_行=13,  # G3 例外恒声明·契约一致
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
        # 行业评分档(缺口二):analyzer 已算的"本行业 dimension_specs 评分区间"定位,纯透传不重算。
        # 口径诚实:是**本行业评分档**(优/良/中/弱),非同行业经验百分位。缺则回退跨行业粗档。
        行业档 = fin.get("行业评分档") or {}
        毛利用行业, 净利率用行业 = False, False
        vals = []
        if isinstance(roe, (int, float)):
            vals.append(f"ROE{roe:.2f}")
        if isinstance(gm, (int, float)):
            b = 行业档.get("毛利率")
            if b and isinstance(b.get("score"), (int, float)):
                档, _ = 格档(float(b["score"]), _QUALITY档)
                vals.append(f"毛利{gm:.2f}[本行业{b.get('行业')}评分档{档}]")
                毛利用行业 = True
            else:
                gm档, _ = 格档(float(gm), _毛利率档)
                vals.append(f"毛利{gm:.2f}[{gm档}·跨行业]")
        if isinstance(nm, (int, float)):
            b = 行业档.get("净利率")
            if b and isinstance(b.get("score"), (int, float)):
                档, _ = 格档(float(b["score"]), _QUALITY档)
                vals.append(f"净利率{nm:.2f}[本行业{b.get('行业')}评分档{档}]")
                净利率用行业 = True
            else:
                nm档, _ = 格档(float(nm), _净利率档)
                vals.append(f"净利率{nm:.2f}[{nm档}·跨行业]")
        回报文 = f"回报维{回报维}/百" if isinstance(回报维, (int, float)) else "回报维NA"
        _毛净口径 = ("毛利/净利率=本行业评分档(引擎已算行业区间,非经验百分位)"
                  if (毛利用行业 or 净利率用行业) else "毛利/净利率跨行业粗参考(无行业专家)")
        items.append(字段(
            "盈利能力", "/".join(vals) if vals else None,
            f"ROE={roe类型}未年化·不可比年化15%; {_毛净口径}",
            f"影响：当前口径盈利效率一般、强弱以下方'回报'维为准（{回报文}）",
        ))

        # ── 负债率（金融/地产例外）──
        负债 = fund.get("负债率")
        金融口径 = bool(fin.get("金融业口径"))
        b负债 = 行业档.get("资产负债率")
        if isinstance(负债, (int, float)) and b负债 and isinstance(b负债.get("score"), (int, float)) and not 金融口径:
            # 本行业评分档(缺口二透传):资产负债率是反向指标,dimension_specs 已按行业反向映射,
            # 高分=本行业内杠杆更轻。跨行业粗档 → 本行业评分档(优/良/中/弱,非经验百分位)。
            档, _ = 格档(float(b负债["score"]), _QUALITY档)
            items.append(字段("负债率", 负债, f"本行业{b负债.get('行业')}评分档{档}",
                             f"影响：本行业内杠杆位置见评分档({档}=越优越轻);跨行业粗档已由行业区间取代"))
        elif isinstance(负债, (int, float)):
            d档, _ = 格档(float(负债), _负债率档)
            例外 = "·金融业口径" if 金融口径 else "·跨行业粗档"
            影响 = _负债影响.get(d档, "") + ("（金融业高杠杆属常态、勿套档）" if 金融口径
                                          else "（无行业专家、跨行业粗参考）")
            items.append(字段("负债率", 负债, f"{d档}{例外}", f"影响：{影响}"))
        else:
            items.append(字段("负债率", None, "负债率缺失", "影响：无负债率数据、该维缺席"))

        # ── 每股股利（无档）──
        股利 = fund.get("每股股利")
        items.append(字段(
            "每股股利", 股利 if isinstance(股利, (int, float)) else None,
            "每股现金分红(元)",
            "影响：分红回报有贡献" if isinstance(股利, (int, float))
            else "影响：无分红/未披露、分红回报无贡献(成长股常态)",
        ))

        # ── 财报质量（复用 _QUALITY档）──
        评级 = fin.get("评级")
        quality = fin.get("quality_score")
        报告期 = fin.get("报告期")
        报告类型 = fin.get("报告类型") or _报告类型(报告期)
        披露日 = fin.get("披露日")
        # 缺口三①:行业专家=null → 通用兜底(五维/quality 为通用测算,非行业专属阈值)。
        # 明确打标,避免"通用兜底"被误读成"数据缺失/显糙"(如综合类 000504)。改展示不改模型。
        行业专家 = fin.get("行业专家")
        通用兜底 = bool((isinstance(quality, (int, float)) or 评级) and not 行业专家)
        兜底档注 = "·通用口径(无行业专属专家)" if 通用兜底 else ""
        兜底意味 = "；五维/quality 为**通用测算**,非本行业专属阈值(该行业暂无专家,非数据缺失)" if 通用兜底 else ""
        if isinstance(quality, (int, float)) or 评级:
            q档, _ = 格档(quality, _QUALITY档) if isinstance(quality, (int, float)) else ("NA", "")
            items.append(字段(
                "财报质量汇总", f"{评级}·quality{quality}",
                f"{q档}·{报告类型}{报告期}·披露{披露日}{兜底档注}",
                f"影响：{_五维综合(dims, q档)}{兜底意味}",
            ))
        else:
            items.append(字段("财报质量汇总", None, "无 financial 评级/quality",
                             "影响：该票未进深度财报采集"))

        # ── 财报五维逐维（v2：各维 值+档+一句描述；共性"五维分别是什么"进统一词表）──
        if dims:
            for 维 in ("成长", "质量", "健康", "运营", "回报"):
                s = dims.get(维)
                if isinstance(s, (int, float)):
                    d档, _ = 格档(float(s), _QUALITY档)
                    句 = _五维强弱句[维][_维bucket(d档)]
                    items.append(字段(维, round(float(s), 1), d档, f"影响：{句}"))
                else:
                    items.append(字段(维, None, "缺", f"影响：{维}维缺数据"))
        else:
            items.append(字段("财报五维", None, "无 five_dims",
                             "影响：该票未进深度财报采集、五维缺"))
        # ── 财报增速明细（利润表摘要归母/扣非/营收增速·保字段明细）──
        摘要 = fin.get("利润表摘要") or {}
        if 摘要:
            增速文 = (f"归母{摘要.get('归母净利增速')}/扣非{摘要.get('扣非净利增速')}"
                    f"/营收{摘要.get('营收增速')}")
            items.append(字段(
                "财报增速明细", 增速文, "利润表增速%(报告期口径)",
                "影响：扣非增速验成长真实性、剔非经常损益",
            ))
        else:
            items.append(字段("财报增速明细", None, "无利润表摘要", "影响：无增速明细、成长细节缺"))

        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块="", 字段解读=items,
            max_浓缩块_行=13,  # G3 例外·统筹裁定(2026-09-19)：财报五维逐维各一句需 >8 行
            fields={
                "营收": fund.get("营收"), "净利": fund.get("净利"),
                "营收增速": fund.get("营收增速"), "净利增速": fund.get("净利增速"),
                "ROE": roe, "毛利率": gm, "净利率": nm, "负债率": 负债,
                "每股股利": 股利, "金融业口径": 金融口径,
                "评级": 评级, "quality_score": quality, "报告期": 报告期,
                "报告类型": 报告类型, "披露日": 披露日,
                "five_dims": dims, "利润表摘要": 摘要, "回报维": 回报维,
                "行业专家": 行业专家, "通用兜底": 通用兜底,   # 缺口三①:通用兜底口径标记(展示层据此提示)
                "行业评分档": 行业档 or None,                # 缺口二:透传源(便于 web/下游复用)
            },
            freshness="fresh", 防未来=True, source=self.source, 面=self.面,
        )


register(GrowthQualityTool())
