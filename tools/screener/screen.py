"""选股筛选器(阶段一)。

pluggable 预设筛选,操作 serialize 记录(data/analysis/{code}.json)。
⚠️ 预设规则为**自主开发的合理默认**,待用户细化(需求 N1);阈值集中在 strategy.py['选股']。
契约见 docs/需求与目标.md 第 3 节。
"""
from __future__ import annotations

from typing import Callable

from tools.config.strategy import THRESHOLDS

Filter = Callable[[dict], bool]
_T = THRESHOLDS["选股"]


def _get(r, *path):
    cur = r
    for k in path:
        cur = (cur or {}).get(k) if isinstance(cur, dict) else None
    return cur


# ---------- 预设筛选条件(默认规则,待 N1 细化)----------
def f_reversal(r: dict) -> bool:
    """超跌反弹候选:有拐点信号且评分达标。"""
    lab = _get(r, "signals", "reversal", "拐点标签")
    score = _get(r, "signals", "reversal", "拐点评分") or 0
    return lab not in (None, "无") and score >= _T["拐点候选_最低分"]


def f_fundflow(r: dict) -> bool:
    """主力吸筹:主力连续净流入达标且今日净流入为正。"""
    days = _get(r, "fundflow", "主力连续净流入天数") or 0
    today = _get(r, "fundflow", "今日主力净流入")
    return days >= _T["主力吸筹_连续天数"] and isinstance(today, (int, float)) and today > 0


def f_trend_strong(r: dict) -> bool:
    """趋势强势:趋势评级得分达标。"""
    return (_get(r, "signals", "trend", "得分") or -999) >= _T["趋势强势_最低分"]


def f_quality(r: dict) -> bool:
    """质地优不高估:ROE 达标且 PE 有效。"""
    roe = _get(r, "fundamental", "ROE")
    pe_valid = _get(r, "valuation", "pe_valid")
    return isinstance(roe, (int, float)) and roe >= _T["质地_ROE下限"] and bool(pe_valid)


# 预设方案:名称 → 条件(单条或多条 AND)
PRESETS: dict[str, list[Filter]] = {
    "超跌反弹候选": [f_reversal],
    "主力吸筹": [f_fundflow],
    "趋势强势": [f_trend_strong],
    "质地优不高估": [f_quality],
    "反弹+资金共振": [f_reversal, f_fundflow],   # 多条件 AND 示例
}


def screen(records: dict[str, dict], filters: list[Filter]) -> list[str]:
    """对全池记录逐一过滤,返回通过所有条件的代码(按趋势得分降序)。"""
    hit = [code for code, r in records.items() if r.get("signals") and all(f(r) for f in filters)]
    hit.sort(key=lambda c: (_get(records[c], "signals", "trend", "得分") or -999), reverse=True)
    return hit


def run_presets(records: dict[str, dict]) -> dict[str, list[str]]:
    """跑全部预设方案。返回 {方案名: [代码...]}。"""
    return {name: screen(records, fs) for name, fs in PRESETS.items()}
