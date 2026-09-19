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
_RS档_加 = {"强": 30, "偏强": 20, "中": 10, "偏弱": 5, "弱": 0}  # A9 起废弃：骨架不再按 RS 打分（与 pos60 重复衡量位置且方向相反），保留常量以防别处引用
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
    """③ 角色(龙头/中军/跟涨)为主 + 冷热拥挤降分。无角色用 focus_score 兜底。

    A9：不再计入 RS（相对强弱分位）。RS 本质衡量"个股涨得强不强/高不高"，与
    子分_量价自证 里的 pos60 档重复衡量"位置"维度且方向相反（pos60 低位加分=偏好
    没涨的；RS 强加分=偏好已涨强的），造成骨架自相矛盾、净偏高位。回测证据：含 RS 的
    板块角色 rank IC 持续为负、含 pos60 的量价自证持续为正。位置维度今后只由 pos60
    衡量一次。sector_context 工具仍照常产出 RS 字段，只是骨架不再用它打分。
    """
    角色 = sec.get("角色")
    base = _角色分.get(角色, 15)
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


# 财报排雷嫌疑档 → 扣分（高危在 veto 里踢，此处给中/低降分）
# missing（工具显式返回·未深采/无数据）取"半个低档"中性扣分，落在 无嫌疑(0)<missing<低 之间：
# 未深采的票不该与"深采查证干净(无嫌疑=0)"同拿排雷满分（#6 缺数据→排雷虚高）。
# ⚠ 量级 8/5/6 是 forward 数据不足(rank IC 暂不可验)时的保守取值，待 forward 够了用 rank IC 复验重调。
_财报档_扣 = {"高危": 100, "中": 30, "低": 15, "无嫌疑": 0, "missing": 8}
# 解禁嫌疑档 → 扣分（高危走 veto 不在此重复扣；此处对中/低降分）
_解禁档_扣 = {"高危": 100, "中": 25, "低": 10, "无嫌疑": 0, "missing": 5}
# 减持嫌疑档 → 扣分（高走 veto「清仓式减持」不在此重复扣；此处对中/低降分）
_减持档_扣 = {"高": 100, "中": 25, "低": 12, "无嫌疑": 0, "missing": 6}


def 子分_排雷(fake: dict, exp: dict, fin: Optional[dict] = None,
            unlock: Optional[dict] = None, reduce: Optional[dict] = None) -> float:
    """④ 负向：clean=满分，假利好+经验硬排雷+财报嫌疑+解禁嫌疑+减持嫌疑降分。硬否决在 veto_reason 里处理。

    #6 缺数据中性化：财报/解禁/减持工具在数据缺失时显式返回档="missing"，此时给"半个低档"
    中性扣分（8/5/6），而非默认满分——"未深采的票"不该与"深采查证干净(无嫌疑=0扣)"同分。
    区分"缺参 ≠ 缺数据"：调用方省略某维(fin/unlock/reduce=None)时按不评估处理(不扣，if 守卫跳过)，
    只有工具显式返回档="missing"才触发中性惩罚（真实管线 build_skeleton 三维恒传 dict）。
    """
    s = _假利好档_分.get(fake.get("嫌疑档"), 100)
    # 经验硬排雷命中（现行·排雷环节）每条再减 15
    hits = exp.get("命中规则") or []
    hard = [r for r in hits if isinstance(r, dict) and r.get("状态") == "现行"
            and "排雷" in str(r.get("环节", ""))]
    s -= 15 * len(hard)
    # 财报嫌疑降分（高危会被 veto，这里对中/低也降）
    if fin:
        s -= _财报档_扣.get(fin.get("嫌疑档"), 0)
    # 解禁嫌疑降分（高危会被 veto，这里对中/低也降）
    if unlock:
        s -= _解禁档_扣.get(unlock.get("解禁嫌疑档"), 0)
    # 减持嫌疑降分（高会被 veto，这里对中/低也降）
    if reduce:
        s -= _减持档_扣.get(reduce.get("减持嫌疑档"), 0)
    return _clip100(s)


def 子分_宏观催化(sec: dict) -> float:
    """② 板块净催化档（global_macro 属 P3 未建，v1 用板块净催化代理）。"""
    return _clip100(_净催化档_分.get(sec.get("净催化档"), 40))


def veto_reason(fake: dict, gate: dict, fin: Optional[dict] = None,
                unlock: Optional[dict] = None, reduce: Optional[dict] = None) -> Optional[str]:
    """一票否决（拍板：涨停不可买）：假利好高 / 财报高危 / 大额解禁临近 / 清仓式减持 / 涨停 / 极高位 → 踢出池，不进排序。"""
    if fake.get("嫌疑档") == "高":
        return "假利好高嫌疑"
    if fin and fin.get("嫌疑档") == "高危":
        return "财报高危红旗"
    if unlock and unlock.get("解禁嫌疑档") == "高危":
        return "大额解禁临近"
    if reduce and reduce.get("减持嫌疑档") == "高":
        return "清仓式减持"
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
                fake: dict, exp: dict, fin: Optional[dict] = None,
                unlock: Optional[dict] = None, reduce: Optional[dict] = None) -> 骨架票:
    """对一票算五子分 + 加权骨架分 + 否决判定。"""
    subs = {
        "量价自证": 子分_量价自证(pv, gate),
        "板块角色": 子分_板块角色(sec),
        "策略共识": 子分_策略共识(sp_labels),
        "排雷": 子分_排雷(fake, exp, fin, unlock, reduce),
        "宏观催化": 子分_宏观催化(sec),
    }
    total = sum(subs[k] * WEIGHTS[k] for k in WEIGHTS)
    v = veto_reason(fake, gate, fin, unlock, reduce)
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
            "财报": (fin or {}).get("嫌疑档"), "财报评级": (fin or {}).get("评级"),
            "解禁": (unlock or {}).get("解禁嫌疑档"),
            "解禁剩余天数": (unlock or {}).get("剩余天数"),
            "减持": (reduce or {}).get("减持嫌疑档"),
            "减持命中条数": (reduce or {}).get("命中条数"),
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
    fin_t = registry.get("financial_redflag")
    unlock_t = registry.get("unlock_risk")
    reduce_t = registry.get("insider_reduction")

    ranked: list[骨架票] = []
    vetoed: list[骨架票] = []
    数据缺失 = 0
    for c in codes:
        try:
            pv = pv_t.run(as_of, c, root=root).fields
            gate = gate_t.run(as_of, c, root=root).fields
            sec = sec_t.run(as_of, c, root=root).fields
            fake = fake_t.run(as_of, c, root=root).fields
            exp = exp_t.run(as_of, c, root=root).fields
            fin = fin_t.run(as_of, c, root=root).fields
            unlock = unlock_t.run(as_of, c, root=root).fields
            reduce = reduce_t.run(as_of, c, root=root).fields
        except Exception:
            数据缺失 += 1  # 工具取数失败/字段缺 → 未计分（A11：与池规模自洽）
            continue
        row = compose_one(c, members[c], pv, gate, sec, fake, exp, fin, unlock, reduce)
        (vetoed if row.否决 else ranked).append(row)

    # A10 稳定 tie-break：骨架分↓ → 量价自证子分↓（最决定性基石）→ 代码↑（终极可复现）。
    # 消除"同分靠原始 code 序偶然进出"——排序完全确定、可复现。
    ranked.sort(key=_排序键)

    计分票数 = len(ranked) + len(vetoed)  # A11：实际算出骨架分的票（含被否决）
    return {
        "as_of": as_of,
        "权重": WEIGHTS,
        # A11 计分票数口径三分拆·自洽：
        #   参与票数 = 计分票数 + 数据缺失票数 ；计分票数 = 排序票数 + 否决票数
        "池规模": len(members),
        "参与票数": len(codes),        # 本次实际遍历的票（limit 时 < 池规模）
        "计分票数": 计分票数,          # 算出骨架分的票（排序 + 否决）
        "排序票数": len(ranked),       # 进入排序表的票
        "否决票数": len(vetoed),       # 一票否决踢出的票
        "数据缺失票数": 数据缺失,      # 取数失败未计分的票
        "排序键": "骨架分↓, 量价自证子分↓, 代码↑",
        "排序": ranked,
        "排雷否决": vetoed,
    }


def _排序键(r: "骨架票"):
    """A10 排序键：骨架分↓、量价自证子分↓、代码↑（稳定可复现，无同分偶然进出）。"""
    return (-r.骨架分, -(r.子分.get("量价自证") or 0.0), r.code)


def select_top(ranked: list, top_n: int) -> list:
    """A10 topN 分桶：取 top_n，但若边界正好切在同骨架分处，整桶纳入（避免同分硬切）。

    ranked 须为已按 _排序键 降序排好的排序表。返回长度 ≥ top_n，多出的部分
    与第 top_n 名同骨架分（同分票要么全进要么全不进，不跨界劈开）。
    """
    if top_n is None or top_n <= 0 or top_n >= len(ranked):
        return list(ranked)
    cutoff = ranked[top_n - 1].骨架分
    # ranked 降序：所有 骨架分 >= cutoff 的票连续位于表首，含边界同分整桶
    return [r for r in ranked if r.骨架分 >= cutoff]
