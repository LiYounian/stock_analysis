"""stock_sentiment_tool（②消息塔层 · 消息情绪面）· 个股级消息情绪/公告/预期/社媒。

体检卡四面重构 Wave2 首个"字段解读"工具（口径三段规范载体，见 registry.ToolResult.字段解读）。
读 per-stock json（`data/analysis/<as_of>/<code>.json`）产出**个股级**消息情绪面条目：

- 个股情绪/舆情：sentiment.{净情绪分,利好数,利空数,样本数,覆盖率,质量,三层}（三层原样解读）。
  净情绪≈中性时**区分三态**（v2 §4）：样本太少（非缺数据）/ 真中性 / 打分未覆盖（缺数据待补）。
- 消息覆盖与可信度：sentiment.{覆盖率,质量,样本数}——抓 N 条、约 X% 成功打分，决定净情绪分可信度。
- 公司公告/事件：events[] 按 type 归类 tally。**剔除 {减持,权益变动,解禁}**——它们已被
  insider_reduction（基本面·治理）/ unlock_risk（资金面·筹码）专用工具占用，此处再统计会双计。
- 一致预期/研报覆盖：consensus.{覆盖机构数,预期EPS,预期增速}（部分为 None → "无覆盖·待补"）。
- 社媒热度：ugc（读到就用，无则 NA）。

**不重复**消息面已有的 sector_context（板块催化/角色）与 fake_good_news（假利好 price 行为）——
本工具只补个股级情绪/公告/预期/社媒。

口径贯通：三档位表常量（净情绪档/覆盖率档/研报覆盖档）语义锁；四段（名/值/口径/意味）禁空编，
None 诚实标"待补/NA"不臆造。events 防未来（drop date>as_of）+ 近 60 日窗（源实测事件龄≤65 日）。
"""
from __future__ import annotations

from typing import Optional, Any
import datetime as _dt
import json
import os

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 字段, 格档

# ── 写死常量（语义锁）──
RECENT_EVENT_DAYS = 60          # 公告事件近窗（自然日）；源实测事件龄≤65 日、无一>90 日
专用事件类型 = ("减持", "权益变动", "解禁")  # 已被 insider_reduction/unlock_risk 占用，本工具 tally 剔除避免双计
样本太少阈值 = 5                 # 净情绪≈中性且样本≤此值 → 判"消息太少非真中性"(v2 §4·仅呈现分支,不影响档位/打分)

# ── v2 净情绪影响模板（非中性档）──
_净情绪影响 = {
    "强正": "消息面显著偏多、加分",
    "正": "消息面偏多、小幅加分",
    "负": "消息面偏空、小幅减分",
    "强负": "消息面显著偏空、减分",
}
# ── v2 消息覆盖可信度影响模板 ──
_覆盖可信 = {
    "低覆盖": "读数可信度低、别依赖",
    "部分覆盖": "读数可信度一般、别过度依赖",
    "全覆盖": "读数可信度高",
}

# ── 净情绪档（on sentiment.净情绪分，实测域约[-0.69,+0.62]，格档 value<=上界 命中）──
净情绪档 = [
    (-0.30, "强负", "显著偏空·利空主导"),
    (-0.05, "负", "偏空"),
    (0.05, "中性", "多空均衡"),
    (0.30, "正", "偏多"),
    (float("inf"), "强正", "显著偏多·利好主导"),
]

# ── 覆盖率档（on sentiment.覆盖率 0~1，打分成功率）──
覆盖率档 = [
    (0.5, "低覆盖", "打分成功率≤50%·情绪分置信低"),
    (0.9, "部分覆盖", "打分成功率50~90%"),
    (float("inf"), "全覆盖", "打分成功率>90%·情绪分可信"),
]

# ── 研报覆盖档（on consensus.覆盖机构数，实测 1~35）──
研报覆盖档 = [
    (0, "无覆盖", "无机构覆盖·待补"),
    (2, "冷门", "≤2家覆盖·关注度低"),
    (9, "一般", "3~9家覆盖"),
    (float("inf"), "高关注", "≥10家覆盖·机构高关注"),
]

_SENT_口径缺省 = "三层加权净情绪(缺层重归一)"


def _load_json(code: str, as_of: str, root: Optional[str]) -> Optional[dict]:
    """读 per-stock json；文件缺或非 dict → None（missing，不编）。"""
    p = os.path.join(data_root(root), "data", "analysis", as_of, f"{code}.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _num(x: Any) -> Optional[float]:
    """转 float，失败/None → None。"""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _fmt(x: Any) -> str:
    """EPS 等浮点：None→NA，否则 2 位。"""
    v = _num(x)
    return "NA" if v is None else f"{v:.2f}"


def _tally_events(events: Any, as_of: str) -> tuple[Optional[int], str]:
    """近窗公告事件 tally（防未来 + 剔除专用类型）。

    返回 (保留条数, roster 文本)。events 非列表 → (None, "无events数据·待补")。
    """
    if not isinstance(events, list):
        return None, "无events数据·待补"
    try:
        cut = _dt.date.fromisoformat(as_of)
    except ValueError:
        cut = None
    counts: dict[str, int] = {}
    for e in events:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        if t in 专用事件类型:  # 已被基本面·治理 / 资金面·筹码 占用，剔除避免双计
            continue
        ds = e.get("date")
        if cut is not None:
            try:
                d = _dt.date.fromisoformat(str(ds)[:10])
            except (ValueError, TypeError):
                continue
            if d > cut or (cut - d).days > RECENT_EVENT_DAYS:  # 防未来 + 近窗
                continue
        key = str(t) if t else "其他"
        counts[key] = counts.get(key, 0) + 1
    n = sum(counts.values())
    if n == 0:
        return 0, "无新公告事件"
    roster = "·".join(f"{k}×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    return n, roster


class StockSentimentTool:
    name = "stock_sentiment"
    塔层 = "②消息"
    面 = "消息情绪面"  # 个股级情绪/公告/预期/社媒（不重复 sector_context/fake_good_news）
    source = "per-stock json: sentiment / events / consensus / ugc"

    def run(self, as_of: str, code: Optional[str] = None, root: Optional[str] = None, **kw) -> ToolResult:
        if not code:
            raise ValueError("stock_sentiment 需 --code")
        d = _load_json(code, as_of, root)
        if d is None:
            return ToolResult(
                name=self.name, 塔层=self.塔层, 面=self.面, as_of=as_of, code=code,
                浓缩块="个股消息面: 无 per-stock json·无法核情绪/公告/预期·人工确认",
                fields={"数据不足": True},
                freshness="missing", 防未来=True, source=self.source,
            )

        sent = d.get("sentiment") if isinstance(d.get("sentiment"), dict) else {}
        cons = d.get("consensus") if isinstance(d.get("consensus"), dict) else {}
        tri = sent.get("三层") if isinstance(sent.get("三层"), dict) else {}

        fields_out: dict[str, Any] = {}
        字段解读 = []

        # ── ① 个股净情绪 ──
        净 = _num(sent.get("净情绪分"))
        质量 = str(sent.get("质量")) if sent.get("质量") is not None else "NA"
        样本数 = sent.get("样本数")
        利好数, 利空数 = sent.get("利好数"), sent.get("利空数")
        sent口径 = str(sent.get("口径") or _SENT_口径缺省).strip() or _SENT_口径缺省
        样本文 = 样本数 if 样本数 is not None else "NA"
        好坏文 = (f"利好{利好数 if 利好数 is not None else 'NA'}"
                f"/利空{利空数 if 利空数 is not None else 'NA'}/样本{样本文}")
        if 净 is None:
            fields_out["净情绪档"] = "无有效情绪"
            字段解读.append(字段(
                "个股净情绪", None,
                f"无有效情绪·待补·质量{质量}",
                f"影响：情绪未成功打分、缺数据待补(非中性、非无倾向)·{好坏文}",
            ))
        else:
            档名, _ = 格档(净, 净情绪档)
            fields_out["净情绪分"] = 净
            fields_out["净情绪档"] = 档名
            # v2 §4：净情绪≈中性 → 三态区分（样本太少 / 真中性 / 样本数未知）
            if 档名 == "中性":
                if isinstance(样本数, int) and 样本数 <= 样本太少阈值:
                    影响 = (f"不是缺数据，是近期消息太少(仅{样本数}条)、不足以形成多空倾向，"
                          "既不助攻也不压制")
                elif isinstance(样本数, int):
                    影响 = "样本充足但多空均衡、真中性，消息面不偏不倚"
                else:
                    影响 = "多空均衡、中性(样本数未知、置信见下方覆盖)"
            else:
                影响 = _净情绪影响.get(档名, "参考消息倾向")
            字段解读.append(字段(
                "个股净情绪", 净,
                f"{档名}·{sent口径}",
                f"影响：{影响}·{好坏文}",
            ))

        # ── ② 情绪三层（原样解读）──
        舆情 = tri.get("舆情") if isinstance(tri.get("舆情"), dict) else {}
        新闻 = tri.get("新闻") if isinstance(tri.get("新闻"), dict) else {}
        政策 = tri.get("政策") if isinstance(tri.get("政策"), dict) else {}
        舆情净 = _num(舆情.get("净情绪"))
        多空 = str(舆情.get("多空") or "NA")
        if not tri:
            字段解读.append(字段(
                "情绪三层", None,
                "三层缺失·待补",
                "影响：无新闻/政策/舆情三层拆解、看总净情绪",
            ))
        else:
            字段解读.append(字段(
                "情绪三层", 舆情净,
                "舆情层净情绪·三层原样(缺层重归一)",
                f"影响：驱动看三层——舆情{多空}·新闻{_fmt(新闻.get('净情绪'))}·政策{_fmt(政策.get('净情绪'))}",
            ))
            fields_out["舆情多空"] = 多空

        # ── ③ 消息覆盖与可信度（原"情绪样本质量"·v2 改名讲人话）──
        cov = _num(sent.get("覆盖率"))
        if cov is None or 质量 in ("missing", "None", "NA"):
            字段解读.append(字段(
                "消息覆盖与可信度", cov,
                f"打分未覆盖·待补·质量{质量}",
                f"影响：无有效情绪样本(样本{样本文})、读数不可信",
            ))
            fields_out["覆盖率档"] = "打分未覆盖"
        else:
            档名, _ = 格档(cov, 覆盖率档)
            百 = f"{cov * 100:.0f}%"
            字段解读.append(字段(
                "消息覆盖与可信度", cov,
                f"{档名}·抓{样本文}条·约{百}成功打分·质量{质量}",
                f"影响：样本{样本文}+{档名}→{_覆盖可信.get(档名, '')}",
            ))
            fields_out["覆盖率档"] = 档名

        # ── ④ 公司公告事件（近窗 + 剔除专用类型）──
        n事件, roster = _tally_events(d.get("events"), as_of)
        事件口径 = (
            f"近{RECENT_EVENT_DAYS}日events按type归类·已剔除减持/权益变动/解禁"
            "(见基本面治理/资金面筹码)·impact多待判不臆断方向"
        )
        公告影响 = (f"影响：近窗有{n事件}条公告、需辨方向（{roster}）"
                  if isinstance(n事件, int) and n事件 > 0
                  else f"影响：近窗无公告事件、消息面无事件驱动（{roster}）")
        字段解读.append(字段("公司公告事件", n事件, 事件口径, 公告影响))
        fields_out["公告事件数"] = n事件
        fields_out["公告roster"] = roster

        # ── ⑤ 一致预期/研报覆盖 ──
        insts = _num(cons.get("覆盖机构数"))
        g = _num(cons.get("预期增速"))
        增速文 = "无·待补" if g is None else f"{g * 100:+.1f}%"
        eps文 = f"预期EPS当年{_fmt(cons.get('预期EPS当年'))}/次年{_fmt(cons.get('预期EPS次年'))}·增速{增速文}"
        if insts is None:
            字段解读.append(字段("一致预期", None, "无覆盖·待补",
                             f"影响：无机构覆盖、冷门股缺专业锚、估值更靠自身财报·{eps文}"))
            fields_out["研报覆盖档"] = "无覆盖"
        else:
            档名, _ = 格档(insts, 研报覆盖档)
            字段解读.append(字段("一致预期", int(insts), f"{档名}·{int(insts)}家覆盖",
                             f"影响：{档名}机构关注·{eps文}"))
            fields_out["研报覆盖档"] = 档名
            fields_out["覆盖机构数"] = int(insts)

        # ── ⑥ 社媒热度 ──
        ugc = d.get("ugc")
        if ugc in (None, {}, [], ""):
            字段解读.append(字段("社媒热度", None, "社媒热度源未接入",
                             "影响：暂无社媒热度数据、该维后置"))
            fields_out["社媒热度"] = None
        else:
            ugc文 = str(ugc)[:24]
            字段解读.append(字段("社媒热度", ugc文, "社媒热度原样", "影响：见 ugc 字段"))
            fields_out["社媒热度"] = ugc文

        # 新鲜度：sentiment 陈旧或净情绪分缺 → stale；否则 fresh
        新鲜度 = str(sent.get("新鲜度") or "")
        freshness = "stale" if (新鲜度 == "陈旧" or 净 is None) else "fresh"

        return ToolResult(
            name=self.name, 塔层=self.塔层, 面=self.面, as_of=as_of, code=code,
            浓缩块="",  # 由 字段解读 自动派生（展示=传输同源）
            字段解读=字段解读, fields=fields_out,
            freshness=freshness, 防未来=True, source=self.source,
        )


register(StockSentimentTool())
