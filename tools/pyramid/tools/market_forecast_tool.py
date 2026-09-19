"""market_forecast（④宏观）· 查当日**大盘定调**（只读查询接口）。

背景：合成 Agent 辩证时没有独立查大盘定调的接口，只能被动接受个股信号。本工具把当日
market_forecast.json（schema market_forecast/v1）原样 surface，让 Agent 能主动核大盘 β、
分歧、上行概率分位、广度/情绪/资金面快照——**且效力 caveat 原样透传，绝不洗白成"高概率能赚钱"**。

数据源：data/analysis/<as_of>/market_forecast.json。缺当日 → 回退最近 ≤as_of 一日并标 stale；
全无 → missing。防未来：断言所用文件的内部 as_of ≤ 查询 as_of。

分层输出：
  · 浓缩块（≤8 行，全部 surface 自 json、不新算档）：β基准(proxy 默认,1日+5日)/hs300背景/
    分歧标记/广度快照/情绪快照/两融(标 kill-switch 权重0)/维度贡献(1日+5日两档标清 horizon)/
    ⚠️ notes 效力 caveat **原样**（json.notes 无换行=单行，整段原样进浓缩块）。
  · fields = 全量结构（targets/breadth/sentiment/fundflow/分歧标记/notes 原样），供下钻。

选股口径：个股 β 基准默认 proxy(全A等权≈中小盘)，hs300 仅权重股背景；两者分歧看"分歧标记"。
code 参数忽略（市场级，不挂任何个股卡；面=None）。
"""
from __future__ import annotations

from typing import Optional, Any
import datetime as _dt
import glob
import json
import os
import re

from tools.pyramid.registry import ToolResult, register
from tools.pyramid._common import data_root, 浓缩块

_HORIZONS = ("1", "5")  # 选股用两档 horizon（1 日 / 5 日）


def _load_json(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def _resolve(as_of: str, root: Optional[str]) -> tuple[Optional[dict], str, Optional[str]]:
    """定位 market_forecast.json：先当日，缺则回退最近 ≤as_of 一日（stale）。

    返回 (data, freshness, used_date)。全无 → (None, "missing", None)。
    """
    base = os.path.join(data_root(root), "data", "analysis")
    exact = os.path.join(base, as_of, "market_forecast.json")
    d = _load_json(exact)
    if d is not None:
        return d, "fresh", as_of
    # 回退：扫所有日期目录，取 ≤as_of 的最近一日
    cand = []
    for p in glob.glob(os.path.join(base, "*", "market_forecast.json")):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(os.path.dirname(p)))
        if m and m.group(1) <= as_of:
            cand.append((m.group(1), p))
    if not cand:
        return None, "missing", None
    cand.sort(reverse=True)
    used_date, path = cand[0]
    return _load_json(path), "stale", used_date


def _fmt_pu(p: Any) -> str:
    return f"{float(p):.2f}" if isinstance(p, (int, float)) else "NA"


def _horizon_line(tgt: dict, h: str) -> str:
    """一个 target 的某 horizon：方向·分位·p_up。缺→NA，绝不编。"""
    hz = (tgt.get("horizons") or {}).get(h) or {}
    d = hz.get("direction") or "NA"
    b = hz.get("prob_bucket") or hz.get("bucket") or "NA"
    return f"{h}日 {d}·{b} p_up={_fmt_pu(hz.get('p_up'))}"


def _contrib_line(tgt: dict, h: str) -> str:
    """某 horizon 的维度贡献 factor_contrib（技术/广度/消息面/资金流 原样）。"""
    fc = ((tgt.get("horizons") or {}).get(h) or {}).get("factor_contrib") or {}
    def g(k):
        v = fc.get(k)
        return f"{float(v):+.2f}" if isinstance(v, (int, float)) else "NA"
    return f"{h}日 技{g('技术')}广{g('广度')}消{g('消息面')}资{g('资金流')}"


class MarketForecastTool:
    name = "market_forecast"
    塔层 = "④宏观"
    面 = None  # 市场级背景，不挂个股四面卡（统筹裁定 2026-09-20）
    source = "data/analysis/<date>/market_forecast.json（schema market_forecast/v1）"

    def run(self, as_of: str, code: Optional[str] = None, root: Optional[str] = None, **kw) -> ToolResult:
        d, freshness, used_date = _resolve(as_of, root)
        if d is None:
            return ToolResult(
                name=self.name, 塔层=self.塔层, as_of=as_of, code=code,
                浓缩块="大盘定调: 当日及之前均无 market_forecast 落盘·待补·人工确认",
                fields={"present": False}, freshness="missing", 防未来=True, source=self.source,
            )

        # 防未来：文件内部 as_of 不得晚于查询 as_of
        file_as_of = str(d.get("as_of") or used_date or "")
        assert not file_as_of or file_as_of <= as_of, \
            f"防未来违规: market_forecast.as_of={file_as_of} > 查询 as_of={as_of}"

        targets = d.get("targets") or {}
        proxy = targets.get("proxy") or {}
        hs300 = targets.get("hs300") or {}
        β默认 = ((d.get("选股用β基准") or {}).get("默认")) or "proxy"
        分歧 = d.get("分歧标记") or {}
        breadth = d.get("breadth_snapshot") or {}
        senti = d.get("sentiment_snapshot") or {}
        ff = d.get("fundflow_snapshot") or {}
        notes = str(d.get("notes") or "")

        lines = []
        # ① β基准（proxy 默认，1日+5日）
        stale_tag = f"（用 {used_date} 数据·stale）" if freshness == "stale" else ""
        lines.append(f"大盘定调·β基准={β默认}(选股用){stale_tag}: "
                     + " ｜ ".join(_horizon_line(proxy, h) for h in _HORIZONS))
        # ② hs300 背景
        lines.append("hs300权重股背景(仅背景勿当个股): "
                     + " ｜ ".join(_horizon_line(hs300, h) for h in _HORIZONS))
        # ③ 分歧标记
        if 分歧.get("触发"):
            维度 = 分歧.get("维度") or {}
            typ = "/".join(f"{h}日「{(维度.get(h) or {}).get('类型','?')}」" for h in _HORIZONS if h in 维度)
            lines.append(f"分歧标记: 触发({分歧.get('口径','')}) {typ}——β以proxy为准勿被hs300偏多带偏")
        else:
            lines.append("分歧标记: 未触发（权重股与中小盘方向一致）")
        # ④ 广度快照
        lines.append(
            f"广度: 净涨跌{_fmt_pu(breadth.get('net_adv'))} MA20上方{_fmt_pu(breadth.get('above_ma20_ratio'))} "
            f"涨停{_num(breadth.get('limit_up'))}/跌停{_num(breadth.get('limit_down'))} 中位{_num(breadth.get('median_pct'))}%")
        # ⑤ 情绪快照
        lines.append(
            f"情绪(se): 多空比{_fmt_pu(senti.get('se_ratio'))} 多{_num(senti.get('se_bull'))}/空{_num(senti.get('se_bear'))}"
            f"(样本n={_num(senti.get('se_n'))})")
        # ⑥ 两融（kill-switch·权重0）
        融资余额 = ff.get("融资余额")
        融资亿 = f"{float(融资余额)/1e8:.0f}亿" if isinstance(融资余额, (int, float)) else "NA"
        lines.append(f"两融({ff.get('margin_date','NA')}): 融资余额{融资亿}·⚠️资金流维权重=0(kill-switch·不参与判别)")
        # ⑦ 维度贡献（proxy·1日+5日两档标清 horizon）
        lines.append("维度贡献(proxy·factor_contrib): "
                     + " ｜ ".join(_contrib_line(proxy, h) for h in _HORIZONS))
        # ⑧ 效力 caveat 原样（不洗白）
        if notes:
            lines.append(f"⚠️效力诚实(原样): {notes}")

        return ToolResult(
            name=self.name, 塔层=self.塔层, as_of=as_of, code=code, 浓缩块=浓缩块(lines),
            fields={
                "present": True, "schema": d.get("schema"), "file_as_of": file_as_of,
                "选股用β基准": d.get("选股用β基准"), "分歧标记": 分歧,
                "targets": targets, "breadth_snapshot": breadth,
                "sentiment_snapshot": senti, "fundflow_snapshot": ff,
                "notes": notes,  # 效力 caveat 原样留全量，绝不删改
            },
            freshness=freshness, 防未来=True, source=self.source,
        )


def _num(v: Any) -> str:
    """整数化展示（None→NA，float 去尾零）。"""
    if v is None:
        return "NA"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


register(MarketForecastTool())
