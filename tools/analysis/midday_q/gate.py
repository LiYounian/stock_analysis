"""午盘 Q · 大盘闸门(14:30 首判 + 14:50 复核)。

方案见 docs/每日分析_午盘Q/00_大盘闸门.md。契约见 M1_契约稿.md §四。

依赖(只读,不重算):
    · data/breadth/<date>_T<slot>.json   ← market_breadth 落盘后由 pipeline 拷副本
      本文件已含 indices(沪深300/中证1000/…)+ 全 A 广度(涨跌家数/净广度/涨跌停家数)
      → G1/G2/G3/G4/G5 全部读它,一个副本文件就够

产出:
    · data/analysis/midday_q/gate_<date>.json   ← 首判/复核/final 三段合并

设计约束:
    · 纯计算,不触网、不重算已有横截面口径(所有指标来自 breadth 副本,单一真源)
    · 阈值 hardcode 在 THRESHOLDS(M1 保守选项,M3.a 回测后再入 config/strategy.json)
    · G6 北向 M1 阶段跳过(已停披露,见 _数据源实测.md);判定按 5 项算
    · 任一必需指标缺失 → state="未知",allowed_strategies=[](不静默用旧值)

⚠️ G5 采用 breadth.net_breadth(净广度 = (涨-跌)/取到只数)而非"≥3% 差值"——
   breadth 现有字段无"≥3% 家数",用净广度作代理,阈值改为 ±0.2(净广度为比率不为家数)。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal, TypedDict

from tools.config import settings

logger = logging.getLogger("analysis.midday_q.gate")

# ────────────────────────────── 阈值(hardcode,M3.a 回测后入 config) ──────────────────────────────

THRESHOLDS: dict = {
    # G1/G2 单位:分时涨幅(小数),源方 pct_chg / 100
    "G1_hs300_intraday_pct":     {"strong": 0.005,  "weak": -0.005},
    "G2_csi1000_intraday_pct":   {"strong": 0.005,  "weak": -0.005},
    # G3 涨跌家数比(≥1 强,≤1 弱)
    "G3_up_down_ratio":          {"strong": 1.5,    "weak": 0.67},
    # G4 涨停/跌停比(赚钱效应)
    "G4_limit_up_down_ratio":    {"strong": 5.0,    "weak": 1.0},
    # G5 净广度 = (涨-跌)/取到只数,∈ [-1, +1](M1 用此代理"赚钱效应")
    "G5_net_breadth":            {"strong": 0.2,    "weak": -0.2},
    # G6 北向净买入(亿),M1 跳过,阈值先在
    "G6_northbound_net_yi":      {"strong": 30.0,   "weak": -30.0},
    # 崩盘特殊阈值(G4 极低 + G5 极负)
    "CRASH_g4_below":            0.5,
    "CRASH_g5_below":            -0.5,
    # 状态判定:命中 strong/weak 项数 ≥ 阈值即定档(6 项里必 4 项;G6 M1 不算)
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

# G6 M1 跳过标记(判定时不算入命中数,阈值只做占位)
_G6_SKIPPED = True

State = Literal["强势", "弱势", "震荡", "崩盘", "未知"]
Stage = Literal["1430", "1450", "final"]


def _out_root() -> Path:
    """产出根目录(动态求值,支持测试 monkeypatch settings.PROJECT_ROOT)。"""
    return settings.PROJECT_ROOT / "data" / "analysis" / "midday_q"


class GateIndicators(TypedDict, total=False):
    hs300_intraday_pct: float | None
    csi1000_intraday_pct: float | None
    up_down_ratio: float | None
    limit_up_down_ratio: float | None
    net_breadth: float | None
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
    return _out_root() / f"gate_{date}.json"


def snapshot_path_for(date: str, slot: str) -> Path:
    """依赖:intraday_snapshot 落盘的 T<slot>.json 路径。委托单一真源。

    (M1 阶段闸门主要读 breadth 副本;snapshot 留作 M2 个股信号用。此函数保留是为契约稳定。)
    """
    from tools.pipeline.intraday_snapshot import snapshot_path
    return snapshot_path(date, slot)


def breadth_path_for(date: str, slot: str) -> Path:
    """依赖:market_breadth 一天多次跑的副本(由 midday_q_gate pipeline 落副本时保证)。

    副本命名:data/breadth/<date>_T<slot>.json(与 breadth 原路径 <date>.json 区分)。
    """
    return settings.PROJECT_ROOT / "data" / "breadth" / f"{date}_T{slot}.json"


# ────────────────────────────── 读取工具 ──────────────────────────────

def _load_json(path: Path) -> dict | None:
    """读 JSON,不存在或损坏 → None(不抛,由上层判 state=未知)。"""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("闸门读文件失败 %s: %s", path, e)
        return None


def _pct_from_breadth_index(breadth: dict, code: str) -> float | None:
    """从 breadth.indices[code] 提取 pct_chg 并归一到小数(源方给的是百分数)。

    - breadth 结构见 tools.pipeline.market_breadth.build_payload
    - indices[code].pct_chg 单位是 %(如 0.31 表示 +0.31%)
    - 缺字段 → None(不填 0,同 collectors 口径)
    """
    if not breadth or not isinstance(breadth.get("indices"), dict):
        return None
    idx = breadth["indices"].get(code)
    if not idx:
        return None
    pct = idx.get("pct_chg")
    if pct is None:
        return None
    try:
        return float(pct) / 100.0
    except (TypeError, ValueError):
        return None


def _extract_indicators(breadth: dict | None) -> GateIndicators:
    """从 breadth 副本提取 5 项必需指标 + G6 占位。

    - G1 = 沪深300 (000300) pct_chg / 100
    - G2 = 中证1000 (000852) pct_chg / 100(breadth 已含此指数,见 market_breadth.INDEX_CODES)
    - G3 = up_count / max(down_count, 1)
    - G4 = limit_up_n / max(limit_down_n, 1)
    - G5 = net_breadth(直接读)
    - G6 = None(M1 跳过)
    """
    if not breadth:
        return GateIndicators(
            hs300_intraday_pct=None, csi1000_intraday_pct=None,
            up_down_ratio=None, limit_up_down_ratio=None,
            net_breadth=None, northbound_net_yi=None,
        )

    g1 = _pct_from_breadth_index(breadth, "000300")
    g2 = _pct_from_breadth_index(breadth, "000852")

    up = breadth.get("up_count")
    down = breadth.get("down_count")
    g3 = (float(up) / max(float(down), 1.0)) if (up is not None and down is not None) else None

    lu = breadth.get("limit_up_n")
    ld = breadth.get("limit_down_n")
    g4 = (float(lu) / max(float(ld), 1.0)) if (lu is not None and ld is not None) else None

    nb = breadth.get("net_breadth")
    g5 = float(nb) if nb is not None else None

    return GateIndicators(
        hs300_intraday_pct=g1,
        csi1000_intraday_pct=g2,
        up_down_ratio=g3,
        limit_up_down_ratio=g4,
        net_breadth=g5,
        northbound_net_yi=None,      # M1 跳过
    )


# ────────────────────────────── 判定核心 ──────────────────────────────

_INDICATOR_LABEL = {
    "hs300_intraday_pct":     ("G1 沪深300",   "%",     100.0),   # 显示时 ×100 转 %
    "csi1000_intraday_pct":   ("G2 中证1000",  "%",     100.0),
    "up_down_ratio":          ("G3 涨跌比",    "",       1.0),
    "limit_up_down_ratio":    ("G4 涨停跌停比", "",      1.0),
    "net_breadth":            ("G5 净广度",    "",       1.0),
}

_THRESHOLD_KEY = {
    "hs300_intraday_pct":     "G1_hs300_intraday_pct",
    "csi1000_intraday_pct":   "G2_csi1000_intraday_pct",
    "up_down_ratio":          "G3_up_down_ratio",
    "limit_up_down_ratio":    "G4_limit_up_down_ratio",
    "net_breadth":            "G5_net_breadth",
}


def _classify_one(value: float, strong: float, weak: float) -> str:
    """单项分档:strong / weak / 中性。"""
    if value >= strong:
        return "强"
    if value <= weak:
        return "弱"
    return "中"


def _classify(indicators: GateIndicators) -> tuple[State, list[str]]:
    """按 THRESHOLDS 分档,返回 (state, reasons)。

    行为(M1,G6 跳过):
      - G1..G5 任一为 None → state="未知"(理由列出缺哪个)
      - 崩盘优先:G4 ≤ CRASH_g4_below 且 G5 ≤ CRASH_g5_below → "崩盘"
      - 命中 strong ≥ 4 项 → "强势"
      - 命中 weak   ≥ 4 项 → "弱势"
      - 其余 → "震荡"
    """
    required_keys = list(_THRESHOLD_KEY.keys())         # 5 项必需(G6 已排除)
    reasons: list[str] = []

    # 1) 缺失检测
    missing = [k for k in required_keys if indicators.get(k) is None]
    if missing:
        for k in missing:
            label, _, _ = _INDICATOR_LABEL[k]
            reasons.append(f"{label} 缺失")
        return "未知", reasons

    # 2) 崩盘优先判定
    g4 = indicators["limit_up_down_ratio"]
    g5 = indicators["net_breadth"]
    if g4 <= THRESHOLDS["CRASH_g4_below"] and g5 <= THRESHOLDS["CRASH_g5_below"]:
        reasons.append(
            f"CRASH: G4={g4:.2f} ≤ {THRESHOLDS['CRASH_g4_below']} 且 "
            f"G5={g5:.3f} ≤ {THRESHOLDS['CRASH_g5_below']}"
        )
        return "崩盘", reasons

    # 3) 逐项分档
    strong_hits = 0
    weak_hits = 0
    for k in required_keys:
        v = indicators[k]
        tk = _THRESHOLD_KEY[k]
        strong = THRESHOLDS[tk]["strong"]
        weak = THRESHOLDS[tk]["weak"]
        cls = _classify_one(v, strong, weak)
        label, unit, scale = _INDICATOR_LABEL[k]
        disp = f"{v * scale:+.2f}{unit}" if unit == "%" else f"{v:+.3f}"
        reasons.append(f"{label}={disp} → {cls}(阈值 强≥{strong}/弱≤{weak})")
        if cls == "强":
            strong_hits += 1
        elif cls == "弱":
            weak_hits += 1

    # 4) 定档
    if strong_hits >= THRESHOLDS["STATE_STRONG_HITS"]:
        state: State = "强势"
    elif weak_hits >= THRESHOLDS["STATE_WEAK_HITS"]:
        state = "弱势"
    else:
        state = "震荡"

    reasons.insert(0, f"命中:强 {strong_hits}/{len(required_keys)}, 弱 {weak_hits}/{len(required_keys)}")
    return state, reasons


# ────────────────────────────── 单点闸门 ──────────────────────────────

def compute_gate(
    date: str,
    as_of: str,
    snapshot_path: Path | None = None,
    breadth_path: Path | None = None,
) -> GateResult:
    """单个时点的闸门判定。契约见模块 docstring / M1_契约稿.md §四。

    快照参数 snapshot_path 目前保留但不使用(M1 全部读 breadth 副本;M2 会用 snapshot 拿个股)。
    """
    if as_of not in ("14:30", "14:50"):
        raise ValueError(f"as_of 只支持 '14:30' / '14:50',收到 {as_of!r}")

    slot = as_of.replace(":", "")           # "14:30" → "1430"
    bpath = breadth_path or breadth_path_for(date, slot)
    breadth = _load_json(bpath)

    indicators = _extract_indicators(breadth)
    state, reasons = _classify(indicators)

    if breadth is None:
        reasons.insert(0, f"广度副本缺失: {bpath.name}")

    allowed, pos = STATE_ALLOW[state]
    return GateResult(
        date=date,
        as_of=as_of,
        state=state,
        indicators=indicators,
        allowed_strategies=list(allowed),
        position_pct=pos,
        reasons=reasons,
    )


# ────────────────────────────── 首判+复核合并 ──────────────────────────────

def merge_two_stage(stage1: GateResult, stage2: GateResult) -> dict:
    """合并首判+复核,应用翻转规则(方案 00_大盘闸门.md §翻转规则)。

    翻转规则(保守方向):
      - stage1 未知/弱势/崩盘 → final 空仓(首判不允许出手就不出手,不看 stage2)
      - stage2 未知/弱势/崩盘(stage1 强势/震荡) → 翻转撤单
      - 其余 → final 取 stage2(复核为准)
    """
    s1_state = stage1["state"]
    s2_state = stage2["state"]

    # stage1 不允许出手的场景
    if s1_state in ("未知", "弱势", "崩盘"):
        allowed: list[str] = []
        pos = 0.0
        note = f"首判 {s1_state} - 不出手"
        flipped = False
        final_state: State = s1_state
    # stage1 允许但 stage2 翻转
    elif s2_state in ("未知", "弱势", "崩盘"):
        allowed = []
        pos = 0.0
        note = f"翻转 - {s1_state} → {s2_state},撤单"
        flipped = True
        final_state = s2_state
    # 复核确认(强势/震荡)
    else:
        allowed = list(stage2["allowed_strategies"])
        pos = float(stage2["position_pct"])
        note = f"复核确认({s2_state})"
        flipped = False
        final_state = s2_state

    return {
        "date": stage1["date"],
        "stage1_1430": dict(stage1),
        "stage2_1450": dict(stage2),
        "final": {
            "allowed_strategies": allowed,
            "position_pct": pos,
            "state": final_state,
            "note": note,
        },
        "flipped": flipped,
    }
