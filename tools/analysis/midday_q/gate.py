"""午盘 Q · 大盘闸门(14:30 首判 + 14:50 复核)。

方案见 docs/每日分析_午盘Q/00_大盘闸门.md。契约见 M1_契约稿.md §四。

依赖(只读,不重算):
    · data/intraday/<date>/T<slot>.json   ← intraday_snapshot 落盘的指数分时价
    · data/breadth/<date>_T<slot>.json    ← market_breadth 落盘后由 pipeline 拷副本
    · (可选)T-1 收盘价用于算涨跌幅基准(intraday_snapshot 里已含,不再另读)

产出:
    · data/analysis/midday_q/gate_<date>.json    ← 首判/复核/final 三段合并

设计约束:
    · 纯计算,不触网、不重算已有横截面口径
        全 A 广度必须走 tools.analysis.market_forecast.breadth.cross_section_stats(单一真源)
        本模块只做"读 → 判定 → 输出",不重实现涨跌家数/涨停统计
    · 阈值 hardcode 在 THRESHOLDS(M1 保守选项,M3.a 回测后再入 config/strategy.json)
    · G6 北向 M1 阶段跳过(已停披露,见 _数据源实测.md);判定按 5 项算
    · 任一必需指标缺失 → state="未知",allowed_strategies=[](不静默用旧值)

⚠️ 骨架期:函数体尚未实现(2026-09-07),仅锁 I/O 契约。M1 审阅后填实现。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, TypedDict

from tools.config import settings

logger = logging.getLogger("analysis.midday_q.gate")

# ────────────────────────────── 阈值(hardcode) ──────────────────────────────

# 6 项指标的强/弱阈值(方案 00_大盘闸门.md §闸门指标)
# G6 M1 跳过,保留阈值不用
THRESHOLDS: dict = {
    "G1_hs300_intraday_pct":     {"strong": 0.005,  "weak": -0.005},    # 沪深300 分时涨幅
    "G2_csi1000_intraday_pct":   {"strong": 0.005,  "weak": -0.005},    # 中证1000 分时涨幅
    "G3_up_down_ratio":          {"strong": 1.5,    "weak": 0.67},       # 涨跌家数比
    "G4_limit_up_down_ratio":    {"strong": 5.0,    "weak": 1.0},        # 涨停/跌停比
    "G5_money_effect":           {"strong": 200,    "weak": -200},        # ≥3% 家数 - ≤-3% 家数
    "G6_northbound_net_yi":      {"strong": 30.0,   "weak": -30.0},       # 亿(M1 跳过)
    # 崩盘特殊阈值(G4/G5 联合下限)
    "CRASH_g4_below":            0.5,
    "CRASH_g5_below":            -500,
    # 状态判定:命中 strong/weak 项数 ≥ 阈值即定档
    "STATE_STRONG_HITS":         4,
    "STATE_WEAK_HITS":           4,
}

# 状态 → 允许策略(方案 README.md §派单器·候选设计)
STATE_ALLOW: dict[str, tuple[list[str], float]] = {
    "强势":  (["Q1", "Q3"], 1.0),
    "弱势":  (["Q2", "Q3"], 0.7),
    "震荡":  (["Q1", "Q2", "Q3"], 0.5),
    "崩盘":  ([], 0.0),
    "未知":  ([], 0.0),
}

State = Literal["强势", "弱势", "震荡", "崩盘", "未知"]
Stage = Literal["1430", "1450", "final"]

OUT_ROOT = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q"


class GateIndicators(TypedDict, total=False):
    hs300_intraday_pct: float | None
    csi1000_intraday_pct: float | None
    up_down_ratio: float | None
    limit_up_down_ratio: float | None
    money_effect: int | None
    northbound_net_yi: float | None       # M1 恒 None(跳过)


class GateResult(TypedDict):
    date: str
    as_of: str
    state: State
    indicators: GateIndicators
    allowed_strategies: list[str]
    position_pct: float
    reasons: list[str]                     # 每项指标判定过程,便于人读


# ────────────────────────────── 路径 ──────────────────────────────

def gate_path(date: str) -> Path:
    """产出路径 data/analysis/midday_q/gate_<date>.json(一天一份,三段合并)。"""
    return OUT_ROOT / f"gate_{date}.json"


def snapshot_path_for(date: str, slot: str) -> Path:
    """依赖:intraday_snapshot 落盘的 T<slot>.json 路径。委托单一真源。"""
    from tools.pipeline.intraday_snapshot import snapshot_path
    return snapshot_path(date, slot)


def breadth_path_for(date: str, slot: str) -> Path:
    """依赖:market_breadth 一天多次跑的副本(由 midday_q_gate pipeline 落副本时保证)。

    默认副本命名:data/breadth/<date>_T<slot>.json(与 breadth 原路径 <date>.json 区分)。
    """
    return settings.PROJECT_ROOT / "data" / "breadth" / f"{date}_T{slot}.json"


# ────────────────────────────── 指标计算 ──────────────────────────────

def _read_indices(snapshot: dict) -> tuple[float | None, float | None]:
    """从 intraday_snapshot 的 payload 提取 (沪深300 分时涨幅, 中证1000 分时涨幅)。

    分时涨幅口径:snapshot.indices[code].pct_chg / 100(源方给百分比,归一到小数)。
    · 沪深300 = 000300
    · 中证1000 = 000852(注:intraday_snapshot.INDEX_CODES 目前含 000905=中证500 而非 1000,
      需 M1 扩:见 M1_契约稿.md §六.4)
    """
    raise NotImplementedError("M1 骨架")


def _read_breadth(breadth: dict) -> tuple[float | None, float | None, int | None]:
    """从 breadth 副本 payload 提取 (涨跌家数比 G3, 涨停/跌停比 G4, 赚钱效应 G5)。

    - G3 = up / max(down, 1)
    - G4 = limit_up / max(limit_down, 1)
    - G5 = 涨幅≥3% 家数 - 跌幅≥3% 家数(需 breadth payload 支持;若字段缺失 → None)
    """
    raise NotImplementedError("M1 骨架")


def _classify(indicators: GateIndicators) -> tuple[State, list[str]]:
    """按 THRESHOLDS 分档,返回 (state, reasons)。

    - G6 强制视为 None(M1 跳过)
    - 必需指标 G1..G5 任一为 None → state="未知"
    - 崩盘判定优先:G4 ≤ CRASH_g4_below 且 G5 ≤ CRASH_g5_below → "崩盘"
    - 命中 strong ≥ STATE_STRONG_HITS(4) → "强势"
    - 命中 weak   ≥ STATE_WEAK_HITS  (4) → "弱势"
    - 其余 → "震荡"
    - reasons 逐项列出:"G1=+0.31%(震荡)" / "G4=1.8(震荡)"
    """
    raise NotImplementedError("M1 骨架")


# ────────────────────────────── 单点闸门 ──────────────────────────────

def compute_gate(
    date: str,
    as_of: str,                       # "14:30" 或 "14:50"
    snapshot_path: Path | None = None,
    breadth_path: Path | None = None,
) -> GateResult:
    """单个时点的闸门判定。

    参数:
        date:           "YYYY-MM-DD"
        as_of:          "14:30" 或 "14:50"(其他值:M1 暂只支持这两个)
        snapshot_path:  显式指定(测试用);None → snapshot_path_for(date, slot 由 as_of 推)
        breadth_path:   同上;None → breadth_path_for(date, slot)

    行为:
        · 读快照 + 广度 → 提指标 → 分档 → 输出 GateResult
        · 任一文件缺失:state="未知",reasons=["快照缺失: <path>"]
        · 文件在但字段缺失:该指标 None,继续判(≥1 必需缺 → state="未知")

    非交易日 / 时点非 14:30/14:50 由 pipeline 层拦截,本函数只做纯计算。
    """
    raise NotImplementedError("M1 骨架")


# ────────────────────────────── 首判+复核合并 ──────────────────────────────

def merge_two_stage(stage1: GateResult, stage2: GateResult) -> dict:
    """合并首判+复核,应用翻转规则(方案 00_大盘闸门.md §翻转规则)。

    翻转规则(保守方向):
        · stage1.state ∈ {弱势, 崩盘} → final.allowed=[](本策略族只保守,弱盘不出手)
        · stage1.state ∈ {强势, 震荡} 且 stage2.state ∈ {弱势, 崩盘} → final.allowed=[],flipped=True
        · 其余 → final 取 stage2(复核为准),flipped=False

    未知态处理:
        · stage1 或 stage2 任一为 "未知" → final.allowed=[]

    返回:
        {
            "date": "2026-09-07",
            "stage1_1430": {...GateResult 原样...},
            "stage2_1450": {...GateResult 原样...},
            "final": {
                "allowed_strategies": [...],
                "position_pct": 0.0..1.0,
                "state": State,                # 最终参考态(=stage2.state 或 "撤单")
                "note": "复核确认" | "翻转-撤单" | "首判弱势-不出手" | "未知-不出手",
            },
            "flipped": bool,
        }
    """
    raise NotImplementedError("M1 骨架")
