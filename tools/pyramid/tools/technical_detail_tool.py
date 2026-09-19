"""technical_detail_tool（①塔基·技术指标明细）· Wave2 技术面填充。

补 price_volume 未上全的**技术指标明细**：MA 阶梯/排列 · MACD · KDJ · RSI · 筹码(chip) ·
结构支撑压力(prediction)。**不重复量价**——现价/涨幅/量比/pos60/vsMA 归 price_volume，
入场价/止损止盈/持有期/情景 归 entry_price，本工具一律不碰。

口径贯通（设计 2026-09-19 §2d）：
- 状态类（MA排列 / MACD状态 / KDJ状态）序列化器 `tools/analysis/technical.py` 已算好，**原样用不重算**。
- RSI 无预置状态 → 本工具供档位表，阈值对齐 `tools/config/strategy.py` THRESHOLDS.超买超卖.RSI12
  的 canonical 单一真源（测试 test_technical_detail 锁死防漂移）。
- chip 档（获利比例 / 集中度90）无现成口径 → 按全 A 分位标定（口径段注明）。
- BOLL：snapshot 现未落盘（技术序列化器算了但未拷进 record）→ 恒标"待补落盘"，本工具不动序列化器。

数据源：data/analysis/<as_of>/<code>.json 的 snapshot / chip / prediction（慢变字段·防未来天然按日目录）。
产出走 Wave1 契约：字段解读=[字段(名,值,口径,意味)...]，浓缩块自动派生（≤8 行·G3）。
"""
from __future__ import annotations

from typing import Optional, Any
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 格档, 字段

# ── RSI12 档位（阈值=strategy.THRESHOLDS.超买超卖.RSI12 canonical，测试锁死；升序 value≤上界命中）──
# 超卖极端≤20 / 超卖(20,30] / 中性(30,70] / 超买(70,80] / 超买极端>80
RSI12档 = [
    (20.0, "超卖极端", "极端超卖·反弹动能积聚"),
    (30.0, "超卖", "超卖·具反弹条件"),
    (70.0, "中性", "强弱均衡"),
    (80.0, "超买", "超买·警惕回调"),
    (1e9, "超买极端", "极端超买·回调压力大"),
]
# ── 筹码·获利盘比例档（全 A 09-17 分位标定 p25=.18/p50=.52/p75=.82；升序）──
获利盘档 = [
    (0.2, "普遍套牢", "底部·上方抛压轻"),
    (0.5, "套牢为主", "多数账户套牢"),
    (0.8, "获利为主", "多数账户获利"),
    (1.01, "普遍获利", "高位·获利了结压力大"),
]
# ── 筹码·集中度90 档（越小越集中；全 A 分位标定 p25=.10/p50=.16/p75=.21；升序）──
集中度档 = [
    (0.10, "高度集中", "筹码高度集中·控盘/惜售"),
    (0.18, "较集中", "筹码较集中"),
    (0.28, "一般", "筹码分布一般"),
    (1e9, "分散", "筹码分散·涣散"),
]

# 状态类意味表（v2：只讲影响，共性定义"排列/KDJ是什么"进统一词表）——
_MA排列意味 = {
    "多头排列": "影响：多头趋势、技术面加分",
    "空头排列": "影响：空头趋势、技术面减分",
    "纠缠": "影响：方向未明、中性",
    "数据不足": "影响：K线不足、排列不可判",
}
_KDJ意味 = {
    "超买": "影响：高位派发风险、择时谨慎",
    "超卖": "影响：超跌反弹条件、择时偏机会",
    "-": "影响：中性区、无超买超卖信号",
    "数据不足": "影响：K线不足、KDJ不可判",
}
# ── v2 影响模板（共性定义进词表，此处只讲本股本值影响）──
_RSI影响 = {
    "超卖极端": "极端超卖、反弹动能积聚、偏机会",
    "超卖": "超卖、具反弹条件",
    "中性": "强弱均衡、择时中性",
    "超买": "超买、警惕回调",
    "超买极端": "极端超买、回调压力大、减分",
}
_获利盘影响 = {
    "普遍套牢": "底部、上方抛压轻、偏机会",
    "套牢为主": "多数账户套牢、反弹有解套压力",
    "获利为主": "多数账户获利、需防获利了结",
    "普遍获利": "高位、获利了结压力大、减分",
}
_集中度影响 = {
    "高度集中": "筹码高度集中、主力控盘/惜售",
    "较集中": "筹码较集中",
    "一般": "筹码分布一般",
    "分散": "筹码分散、涣散",
}


def _pstock_path(root: Optional[str], as_of: str, code: str) -> str:
    return os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")


def _fnum(x: Any, nd: int = 2) -> Optional[float]:
    """安全转 float 并round；非数值→None（→字段值段渲染 NA）。"""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    return round(float(x), nd)


def _macd意味(状态: Optional[str], dif: Optional[float]) -> str:
    """MACD 意味：状态原样，金叉/死叉按 dif 零轴位置增强（水上更强/水下需确认）。"""
    if not 状态:
        return "数据缺失"
    水上 = isinstance(dif, (int, float)) and dif > 0
    if 状态 == "金叉":
        return "影响：零轴上金叉、多头动能强" if 水上 else "影响：零轴下金叉、反弹初期需确认"
    if 状态 == "死叉":
        return "影响：零轴下死叉、空头加速" if not 水上 else "影响：零轴上死叉、高位转弱"
    if 状态 == "多头":
        return "影响：柱>0、多头延续"
    if 状态 == "空头":
        return "影响：柱<0、空头延续"
    return "影响：状态未明"


class TechnicalDetailTool:
    name = "technical_detail"
    塔层 = "①塔基"
    面 = "技术面"  # 技术指标明细（MACD/KDJ/RSI/MA/筹码/支撑压力）——量价归 price_volume
    source = "per-stock json snapshot(ma/macd/kdj/rsi)/chip/prediction.结构位"

    def run(self, as_of: str, code: Optional[str] = None,
            root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("technical_detail 需 --code")
        path = _pstock_path(root, as_of, code)
        rec = None
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    rec = json.load(f) or {}
            except Exception:
                rec = None
        snap = (rec or {}).get("snapshot") if isinstance(rec, dict) else None
        if not isinstance(snap, dict) or not snap:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="技术指标明细: 数据缺失（无 snapshot 字段·不编造）",
                fields={"数据缺": True}, freshness="missing",
                防未来=True, source=self.source, 面=self.面,
            )
        chip = (rec or {}).get("chip") if isinstance(rec.get("chip"), dict) else {}
        pred = (rec or {}).get("prediction") if isinstance(rec.get("prediction"), dict) else {}

        items: list[dict] = []

        # ── 1. 均线排列（ma5/10/20/60 + 排列，原样用序列化器状态）──
        ma = snap.get("ma") or {}
        ma5, ma10, ma20, ma60 = (_fnum(ma.get(k)) for k in ("ma5", "ma10", "ma20", "ma60"))
        排列 = ma.get("排列")
        ma值 = "/".join("NA" if v is None else f"{v:g}" for v in (ma5, ma10, ma20, ma60))
        items.append(字段(
            "均线排列", 排列 if 排列 else None,
            f"MA5/10/20/60={ma值}",
            _MA排列意味.get(排列, "影响：数据缺失"),
        ))

        # ── 2. MACD（状态原样 + dif 零轴增强意味）──
        macd = snap.get("macd") or {}
        dif, dea, bar = (_fnum(macd.get(k), 3) for k in ("dif", "dea", "macd"))
        macd状态 = macd.get("状态")
        items.append(字段(
            "MACD", macd状态 if macd状态 else None,
            f"dif={dif if dif is not None else 'NA'}/dea={dea if dea is not None else 'NA'}"
            f"/柱={bar if bar is not None else 'NA'}",
            _macd意味(macd状态, dif),
        ))

        # ── 3. KDJ（状态原样：超买 K>80 / 超卖 K<20 / - 中性）──
        kdj = snap.get("kdj") or {}
        k, d, j = (_fnum(kdj.get(x)) for x in ("k", "d", "j"))
        kdj状态 = kdj.get("状态")
        items.append(字段(
            "KDJ", kdj状态 if kdj状态 else None,
            f"K={k if k is not None else 'NA'}/D={d if d is not None else 'NA'}"
            f"/J={j if j is not None else 'NA'}",
            _KDJ意味.get(kdj状态, "影响：数据缺失"),
        ))

        # ── 4. RSI（本工具供档，阈值对齐 strategy canonical；主档 RSI12，RSI6/24 附注）──
        rsi = snap.get("rsi") or {}
        rsi6, rsi12, rsi24 = (_fnum(rsi.get(x)) for x in ("rsi6", "rsi12", "rsi24"))
        rsi12档, _ = 格档(rsi12, RSI12档)
        rsi6提示 = ""
        if rsi6 is not None:
            if rsi6 > 80:
                rsi6提示 = "、RSI6短线过热"
            elif rsi6 < 20:
                rsi6提示 = "、RSI6短线超跌"
        items.append(字段(
            "RSI", rsi12,
            (f"{rsi12档}·RSI6={rsi6 if rsi6 is not None else 'NA'}"
             f"/RSI24={rsi24 if rsi24 is not None else 'NA'}") if rsi12 is not None
            else "RSI12缺失",
            (f"影响：{_RSI影响.get(rsi12档, '参考强弱')}{rsi6提示}" if rsi12 is not None
             else "影响：数据缺失"),
        ))

        # ── 5. 筹码（获利盘 + 集中度90 合一·全A分位口径；降级不隐瞒）──
        获利比例 = _fnum(chip.get("获利比例"), 4) if chip else None
        集中度 = _fnum(chip.get("集中度90"), 4) if chip else None
        均成 = _fnum(chip.get("平均成本"), 3) if chip else None
        现价 = _fnum(chip.get("现价"), 3) if chip else None
        降级 = bool(chip.get("降级")) if chip else False
        if 获利比例 is not None or 集中度 is not None:
            获档, _ = 格档(获利比例, 获利盘档)
            集档, _ = 格档(集中度, 集中度档)
            套牢 = None if 获利比例 is None else round((1 - 获利比例) * 100, 1)
            获pct = "NA" if 获利比例 is None else f"{获利比例 * 100:.0f}%"
            成本注 = ""
            if 均成 is not None and 现价:
                盈 = (现价 / 均成 - 1) * 100
                成本注 = f"、均成{均成}{'<' if 盈 >= 0 else '>'}现价{现价}整体{'获利' if 盈 >= 0 else '套牢'}{abs(盈):.1f}%"
            套牢注 = "" if 套牢 is None else f"·套牢盘{套牢}%"
            意味 = (f"影响：{_获利盘影响.get(获档, '')}{成本注}；{_集中度影响.get(集档, '')}"
                    + ("（筹码估算降级·换手缺失、可信度打折）" if 降级 else ""))
            items.append(字段(
                "筹码", f"获利{获pct}/集中{集中度 if 集中度 is not None else 'NA'}",
                f"获利盘·{获档}{套牢注}；集中度90·{集档}",
                意味,
            ))
        else:
            items.append(字段("筹码", None, "获利盘/集中度90缺失", "影响：筹码数据缺失、该维缺席"))

        # ── 6. 结构支撑压力（prediction.结构位 原样贯通 + 逼近派生档）──
        结构 = (pred.get("结构位") or {}) if pred else {}
        支撑 = pred.get("支撑位") if pred else None
        压力 = pred.get("压力位") if pred else None
        距支 = _fnum(结构.get("距支撑%"), 2)
        距压 = _fnum(结构.get("距压力%"), 2)
        区间位 = _fnum(结构.get("区间位置%"), 1)
        趋势 = 结构.get("趋势")
        if 支撑 or 压力 or 距支 is not None or 距压 is not None:
            逼近 = []
            if 距压 is not None and 距压 <= 2:
                逼近.append("逼近压力·上行受阻")
            if 距支 is not None and 距支 <= 2:
                逼近.append("逼近支撑·支撑待验")
            支文 = (支撑[0] if isinstance(支撑, list) and 支撑 else "NA")
            压文 = (压力[0] if isinstance(压力, list) and 压力 else "NA")
            意味 = (f"影响：{趋势 or '趋势未明'}"
                    + ("·" + "/".join(逼近) if 逼近 else "·区间运行"))
            items.append(字段(
                "支撑压力", f"支撑{支文}/压力{压文}",
                f"结构位·距支撑{距支 if 距支 is not None else 'NA'}%"
                f"/距压力{距压 if 距压 is not None else 'NA'}%/区间位{区间位 if 区间位 is not None else 'NA'}%",
                意味,
            ))
        else:
            items.append(字段("支撑压力", None, "prediction.结构位缺失", "影响：结构支撑压力数据缺失、该维缺席"))

        # ── 7. BOLL（snapshot 未落盘·恒待补，本工具不动序列化器）──
        boll = snap.get("boll")
        if isinstance(boll, dict) and boll:
            上 = _fnum(boll.get("上轨") or boll.get("upper"))
            中 = _fnum(boll.get("中轨") or boll.get("middle"))
            下 = _fnum(boll.get("下轨") or boll.get("lower"))
            items.append(字段(
                "BOLL", f"上{上}/中{中}/下{下}",
                f"±2σ通达信口径·{boll.get('状态') or '已落盘'}",
                f"影响：{boll.get('状态') or '轨道位置参考'}",
            ))
        else:
            items.append(字段(
                "BOLL", None, "snapshot未落盘",
                "影响：待补落盘(另路chip补·本工具不动技术序列化器)",
            ))

        fresh = "fresh"
        if snap.get("新鲜度") and snap.get("新鲜度") != "新鲜":
            fresh = "stale"
        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
            浓缩块="",  # 由 字段解读 自动派生（展示=传输同源）
            字段解读=items,
            fields={
                "排列": 排列, "MACD状态": macd状态, "KDJ状态": kdj状态,
                "RSI12": rsi12, "RSI12档": rsi12档,
                "获利比例": 获利比例, "集中度90": 集中度, "筹码降级": 降级,
                "距支撑pct": 距支, "距压力pct": 距压, "趋势": 趋势,
                "BOLL落盘": bool(isinstance(boll, dict) and boll),
            },
            freshness=fresh, 防未来=True, source=self.source, 面=self.面,
        )


register(TechnicalDetailTool())
