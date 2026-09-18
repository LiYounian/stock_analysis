"""D2-1 · 金字塔选股 打分骨架引擎（程序·可审计·可回测调权）。

机制见 docs/计划/2026-09-18_D2_流程设计_打分骨架加浓缩块合成.md：
  召回池(shared_pool) → 每票五基石子分(0~100) → 加权求和 = 骨架分 → 排序表。
  排雷=负向/一票否决（假利好高嫌疑/涨停/极高位 → 踢出，不进排序）。
  LLM 合成层（D2-2）在此骨架 top-N 上读浓缩块做受限调整，本文件不含 LLM。

子分函数全为纯函数（吃各工具 fields dict），语义锁测试直接锁映射，不跑重扫描。
权重与档位映射写死在顶部，forward 后（D3）按 T 层标定；改动须过语义锁测试。
"""
from __future__ import annotations

from typing import Optional
from dataclasses import dataclass, field

# ── 权重（初值靠经验·forward 后标定；改动即语义锁测试报警）──
WEIGHTS = {
    "量价自证": 0.30,   # ⑤ price_volume + gate
    "板块角色": 0.25,   # ③ sector_context
    "策略共识": 0.20,   # ① shared_pool 来源
    "排雷": 0.15,       # ④ fake_good_news + experience（负向·clean=满分）
    "宏观催化": 0.10,   # ② sector_context 净催化（板块代理·global_macro 属 P3 未建）
}

_GATE_层级分 = {"T1": 60, "T2": 40, "T3": 25, "未入层": 10}
_量比档_加 = {"放量": 25, "平量": 10, "爆量": 5, "缩量": 0}
_pos60档_加 = {"低": 15, "中": 8, "高": 0}
_角色分 = {"龙头": 50, "中军": 35, "跟涨": 20}   # 无角色 → 15（下方兜底）
_RS档_加 = {"强": 30, "偏强": 20, "中": 10, "偏弱": 5, "弱": 0}
_净催化档_分 = {"强正": 100, "正": 70, "中性": 40, "负": 15, "强负": 0}
_假利好档_分 = {"无嫌疑": 100, "低": 70, "中": 40, "高": 0}


def _clip100(x: float) -> float:
    return max(0.0, min(100.0, float(x)))


# ── 五基石子分（纯函数）──────────────────────────────
def 子分_量价自证(pv: dict, gate: dict) -> float:
    """⑤ 层级(T1>T2>T3)为主 + 温和放量 + 低位加成。爆量不加分（一日游风险）。"""
    s = _GATE_层级分.get(gate.get("层级"), 10)
    s += _量比档_加.get(pv.get("量比档"), 0)
    s += _pos60档_加.get(pv.get("pos60档"), 0)
    return _clip100(s)


def 子分_板块角色(sec: dict) -> float:
    """③ 角色(龙头/中军/跟涨)为主 + RS 分位 + 冷热拥挤降分。无角色用 focus_score 兜底。"""
    角色 = sec.get("角色")
    base = _角色分.get(角色, 15)
    base += _RS档_加.get(sec.get("RS档"), 0)
    if sec.get("拥挤档") == "A" or sec.get("冷热标签") in ("拐点", "过热"):
        base -= 10
    if 角色 is None:  # 无角色时给 focus_score 一点权（板块热度代理）
        fs = sec.get("focus_score")
        if isinstance(fs, (int, float)):
            base += 10 * float(fs)
    return _clip100(base)


def 子分_策略共识(sp_labels: list) -> float:
    """① 命中来源数 × 25（4 有效来源满分）；≥2 来源=交叉共识。"""
    n = len(sp_labels or [])
    return _clip100(n * 25)


def 子分_排雷(fake: dict, exp: dict) -> float:
    """④ 负向：clean=满分，假利好嫌疑降分。硬否决在 veto_reason 里单独处理。"""
    s = _假利好档_分.get(fake.get("嫌疑档"), 100)
    # 经验硬排雷命中（现行·排雷环节）每条再减 15
    hits = exp.get("命中规则") or []
    hard = [r for r in hits if isinstance(r, dict) and r.get("状态") == "现行"
            and "排雷" in str(r.get("环节", ""))]
    s -= 15 * len(hard)
    return _clip100(s)


def 子分_宏观催化(sec: dict) -> float:
    """② 板块净催化档（global_macro 属 P3 未建，v1 用板块净催化代理）。"""
    return _clip100(_净催化档_分.get(sec.get("净催化档"), 40))


def veto_reason(fake: dict, gate: dict) -> Optional[str]:
    """一票否决（拍板：涨停不可买）：假利好高嫌疑 / 涨停 / 极高位 → 踢出池，不进排序。"""
    if fake.get("嫌疑档") == "高":
        return "假利好高嫌疑"
    if gate.get("涨停"):
        return "涨停不可买"
    if gate.get("极高位"):
        return "极高位抛压"
    return None


@dataclass
class 骨架票:
    code: str
    骨架分: float
    子分: dict
    来源标签: list
    否决: Optional[str] = None
    fields快照: dict = field(default_factory=dict)


def compose_one(code: str, sp_labels: list, pv: dict, gate: dict, sec: dict,
                fake: dict, exp: dict) -> 骨架票:
    """对一票算五子分 + 加权骨架分 + 否决判定。"""
    subs = {
        "量价自证": 子分_量价自证(pv, gate),
        "板块角色": 子分_板块角色(sec),
        "策略共识": 子分_策略共识(sp_labels),
        "排雷": 子分_排雷(fake, exp),
        "宏观催化": 子分_宏观催化(sec),
    }
    total = sum(subs[k] * WEIGHTS[k] for k in WEIGHTS)
    v = veto_reason(fake, gate)
    return 骨架票(
        code=code,
        骨架分=round(total, 2),
        子分={k: round(v2, 1) for k, v2 in subs.items()},
        来源标签=sp_labels or [],
        否决=v,
        fields快照={
            "现价": pv.get("现价"), "涨幅pct": pv.get("涨幅pct"),
            "量比": pv.get("量比"), "pos60": pv.get("pos60"),
            "层级": gate.get("层级"), "板块": sec.get("板块"),
            "角色": sec.get("角色"), "净催化档": sec.get("净催化档"),
            "假利好": fake.get("嫌疑档"),
        },
    )


def build_skeleton(as_of: str, root: Optional[str] = None,
                   scan_kline: bool = True, limit: Optional[int] = None) -> dict:
    """遍历召回池，对每票算骨架分，返回排序表（排雷票单列）。

    limit：只算池前 limit 票（按 code 序，测试/快跑用）；None=全池。
    """
    # 延迟 import，避免无数据环境 import 即扫描
    from tools.pyramid.tools.shared_pool_tool import pool_with_labels
    from tools.pyramid import registry
    import tools.pyramid.tools  # noqa: F401  触发注册

    members = pool_with_labels(as_of, root=root, scan_kline=scan_kline)
    codes = sorted(members)
    if limit:
        codes = codes[:limit]

    pv_t = registry.get("price_volume")
    gate_t = registry.get("gate")
    sec_t = registry.get("sector_context")
    fake_t = registry.get("fake_good_news")
    exp_t = registry.get("experience_rules")

    ranked: list[骨架票] = []
    vetoed: list[骨架票] = []
    for c in codes:
        try:
            pv = pv_t.run(as_of, c, root=root).fields
            gate = gate_t.run(as_of, c, root=root).fields
            sec = sec_t.run(as_of, c, root=root).fields
            fake = fake_t.run(as_of, c, root=root).fields
            exp = exp_t.run(as_of, c, root=root).fields
        except Exception:
            continue
        row = compose_one(c, members[c], pv, gate, sec, fake, exp)
        (vetoed if row.否决 else ranked).append(row)

    ranked.sort(key=lambda r: -r.骨架分)
    return {
        "as_of": as_of,
        "权重": WEIGHTS,
        "池规模": len(members),
        "计分票数": len(codes),
        "排序": ranked,
        "排雷否决": vetoed,
    }
