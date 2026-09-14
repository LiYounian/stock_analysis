"""Shadow 候选池 v2(强度排序 + 质量闸门 + 交叉确认)—— headless 自选质量化。

承接计划:docs/计划/2026-09-14_候选池改造计划_headless自选质量化.md
定位:与 v1(shadow_pool.py)**并存**、**不改 v1、不碰生产**。`build_pool_v2` 与
`shadow_pool.build_pool` **同形状**(返回 (pool: list[str], meta: dict)),故现有
shadow_run.py / shadow_score.py **零改动**即可 v1/v2 双跑对拍。

三段式流水线(每段输入输出明确,见计划 §4/§5):
  段一 strength_base  · 用 council `排序分` 做强度骨架(已聚合、已把风控 veto 计入排序)
  段二 quality_gates  · 硬剔结构性噪声(龙虎否决 / 极低流动性)+ 软标签(融资摊薄/筹码/资金流)
  段三 cross_confirm  · 叠加 council 口径多样性/专家数 + 选择性策略命中做加分,行业限流,截断 N

═══ S1 质量字段落表(读真实 record/council 确认,非臆测)═══
council `策略0合议.json` 顶层:`命中高危红旗`/`命中龙虎榜否决` 是**计数(int)不是名单** →
  逐票否决信息须从 record 取。`top[]` 每行:
    code, 行业, 综合方向(看多/看空/中性), 综合分, 综合分_收缩, 排序分(主强度,已含 veto 惩罚),
    口径多样性(int), 参与专家数(int), 覆盖口径(list), 财报风险(null|str)
  注:council `top` 长度按日不稳(多数日 20,个别历史日 30/1997)→ 只采信"有 record 的、看多的"行,
      按 排序分 降序取前 K;record 只对当日 ~500 只可分析票落盘,天然把 1997 行收敛到可分析域。
record `<code>.json` 各块确切键:
    valuation.mktcap_yi(亿, 流动性/规模代理; valuation 可能整块为 None → 防御)
    lhb_veto.triggered(bool, 龙虎榜否决硬闸门)
    snapshot.vol_ratio / snapshot.vol_state(量比/量态, 辅助)
    fundflow.今日主力净流入 / 近5日主力合计 / 主力连续净流入天数(资金流软标签)
    financing.固定一问.有临近解禁_90日(bool) / financing.解禁.未来90日占流通_pct(摊薄软标签)
    financing.可转债.潜在摊薄_pct(可转债摊薄软标签)
    chip.获利比例(0~1, 高位追涨风险软标签) / chip.集中度90
  注:record 无绝对成交额/换手率字段,亦无融资融券余额;"融资拥挤"按计划本意用**摊薄/解禁**口径落地
      (可转债/定增/临近解禁),"流动性"用 mktcap_yi 当日截面分位口径落地。

═══ 防未来 / 防偷看纪律(硬红线,计划 §6.2)═══
- 闸门/权重全部**第一性原理先验设定**,绝不为最大化这 16 日 α 调参。
- 流动性分位用**当日截面**(同日全体可分析票的 mktcap 分布)——同期截面非未来数据,walk-forward 合法。
- 逐日一致、参数不按日调;同输入同输出(确定性)。
"""
from __future__ import annotations
import json
from pathlib import Path

from tools.analysis import shadow_pool  # 复用 _screens(选择性策略命中,交叉确认用)

# ── 先验参数(第一性原理设定,非在 16 日上调优;改动须在文档说明理由)──────────────
CAP = 15            # 目标候选数上限 N(计划 §9 推荐 8~15)
MIN = 8             # 目标候选数下限(不足则由 edge/策略命中补入)
K_BASE = 60         # 强度骨架从 council top 采信的最大条数(record 会进一步收敛)
IND_CAP = 3         # 单行业最多入池只数 M(对齐 SOP 多样化)
LIQ_PCTL = 0.10     # 流动性硬闸门:当日 mktcap 截面分位下限(剔底部 decile)
LIQ_FLOOR_YI = 20.0 # 流动性绝对地板(亿):无论分位如何,市值低于此一律视为结构性偏薄
CHIP_PROFIT_HI = 0.90   # 筹码软标签:获利比例高于此=高位普遍获利、追涨风险
UNLOCK_PCT_HI = 3.0     # 融资软标签:未来90日解禁占流通 > 此(%)=临近摊薄压力
CONVBOND_DILUTE_HI = 5.0  # 融资软标签:可转债潜在摊薄 > 此(%)
# 交叉确认加分权重(先验小权重,仅作强度之上的微调/tie-break,不喧宾夺主)
W_DIVERSITY = 0.02   # 每单位"口径多样性"加分
W_EXPERTS = 0.01     # 每单位"参与专家数"加分
W_SCREEN_HIT = 0.03  # 每个选择性策略命中加分


# ── 段一 · 强度打底 ──────────────────────────────────────────────────────────
def load_council(data_root, date: str):
    """读 council top + 顶层旗标计数。返回 (rows: list[dict], flags: dict)。"""
    p = Path(data_root) / date / "策略0合议.json"
    if not p.exists():
        return [], {}
    obj = json.loads(p.read_text(encoding="utf-8"))
    rows = obj.get("top") or []
    flags = {
        "命中高危红旗": obj.get("命中高危红旗"),
        "命中龙虎榜否决": obj.get("命中龙虎榜否决"),
        "扫描数": obj.get("扫描数"),
        "top_n": obj.get("top_n"),
    }
    return rows, flags


def _record(data_root, date: str, code: str) -> dict:
    p = Path(data_root) / date / f"{code}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def strength_base(council_rows, data_root, date: str, k: int = K_BASE):
    """段一:council 看多 ∩ 有 record,按 排序分(退回综合分_收缩)降序取前 k。

    返回 list[dict]:{code, strength, 行业, 综合方向, 口径多样性, 参与专家数, 综合分_收缩}。
    """
    base = []
    for r in council_rows:
        if not isinstance(r, dict):
            continue
        if r.get("综合方向") != "看多":   # 只做多头侧(headless 无人复核,宁缺毋滥)
            continue
        code = str(r.get("code") or "").zfill(6)
        if len(code) != 6 or not code.isdigit():
            continue
        if not (Path(data_root) / date / f"{code}.json").exists():
            continue  # 无 record 不可分析,天然收敛 council 巨表
        strength = r.get("排序分")
        if strength is None:
            strength = r.get("综合分_收缩")
        if strength is None:
            strength = r.get("综合分")
        if strength is None:
            continue
        base.append({
            "code": code,
            "strength": float(strength),
            "行业": r.get("行业"),
            "综合方向": r.get("综合方向"),
            "口径多样性": r.get("口径多样性") or 0,
            "参与专家数": r.get("参与专家数") or 0,
            "综合分_收缩": r.get("综合分_收缩"),
        })
    # 排序分降序;同分 code 升序保证确定性
    base.sort(key=lambda x: (-x["strength"], x["code"]))
    # 去重(council top 理论无重复,防御)
    seen, dedup = set(), []
    for x in base:
        if x["code"] in seen:
            continue
        seen.add(x["code"])
        dedup.append(x)
    return dedup[:k]


# ── 段二 · 质量闸门(硬剔 + 软标签)────────────────────────────────────────────
def _daily_mktcap_quantile(data_root, date: str, pctl: float) -> float | None:
    """当日全体可分析票 mktcap_yi 的截面分位(walk-forward 合法:仅同期截面)。"""
    d = Path(data_root) / date
    caps = []
    for f in d.glob("*.json"):
        name = f.stem
        if not (len(name) == 6 and name.isdigit()):
            continue
        try:
            v = json.loads(f.read_text(encoding="utf-8")).get("valuation") or {}
        except (json.JSONDecodeError, OSError):
            continue
        m = v.get("mktcap_yi")
        if isinstance(m, (int, float)):
            caps.append(float(m))
    if not caps:
        return None
    caps.sort()
    idx = int(len(caps) * pctl)
    idx = min(idx, len(caps) - 1)
    return caps[idx]


def quality_gates(base, data_root, date: str):
    """段二:逐票过硬闸门 + 打软标签。

    返回 (kept: list[dict], dropped: list[dict{code,reason}], soft_tags: dict{code:[tag,...]})。
    硬闸门(任一命中即剔):龙虎榜否决 triggered / 流动性(mktcap 低于当日分位或绝对地板)。
    软标签(不剔,供 DeepSeek 研判读):融资摊薄、筹码高位、资金流转弱。
    """
    liq_q = _daily_mktcap_quantile(data_root, date, LIQ_PCTL)
    liq_cut = LIQ_FLOOR_YI if liq_q is None else max(liq_q, LIQ_FLOOR_YI)
    kept, dropped, soft = [], [], {}
    for x in base:
        code = x["code"]
        rec = _record(data_root, date, code)
        # 硬闸门 1:龙虎榜否决
        lhb = rec.get("lhb_veto") or {}
        if lhb.get("triggered") is True:
            dropped.append({"code": code, "reason": f"龙虎否决({lhb.get('reason')})"})
            continue
        # 硬闸门 2:流动性下限(当日截面分位 ∨ 绝对地板)
        val = rec.get("valuation") or {}
        m = val.get("mktcap_yi")
        if isinstance(m, (int, float)) and m < liq_cut:
            dropped.append({"code": code, "reason": f"流动性偏薄(mktcap {m:.1f}亿<{liq_cut:.1f})"})
            continue
        # ── 软标签(不剔)──
        tags = []
        fin = rec.get("financing") or {}
        unlock = ((fin.get("解禁") or {}).get("未来90日占流通_pct"))
        if isinstance(unlock, (int, float)) and unlock > UNLOCK_PCT_HI:
            tags.append(f"临近解禁{unlock:.1f}%")
        cb = ((fin.get("可转债") or {}).get("潜在摊薄_pct"))
        if isinstance(cb, (int, float)) and cb > CONVBOND_DILUTE_HI:
            tags.append(f"可转债摊薄{cb:.1f}%")
        chip = rec.get("chip") or {}
        prof = chip.get("获利比例")
        if isinstance(prof, (int, float)) and prof > CHIP_PROFIT_HI:
            tags.append(f"高位获利盘{prof*100:.0f}%")
        ff = rec.get("fundflow") or {}
        f5 = ff.get("近5日主力合计")
        if isinstance(f5, (int, float)) and f5 < 0:
            tags.append("近5日主力净流出")
        if tags:
            soft[code] = tags
        y = dict(x)
        y["soft_tags"] = tags
        kept.append(y)
    return kept, dropped, soft


# ── 段三 · 交叉确认 + 行业限流 + 截断 ────────────────────────────────────────
def cross_confirm_rank(kept, data_root, date, cap=CAP, min_n=MIN, ind_cap=IND_CAP):
    """段三:强度 + 交叉确认加分 → 行业限流 → 截断 N。返回排序后的 list[dict]。"""
    screens = shadow_pool._screens(data_root, date)  # {策略名:[code,...]} 选择性策略
    hit_count = {}
    for _name, codes in screens.items():
        for c in codes:
            hit_count[c] = hit_count.get(c, 0) + 1
    for x in kept:
        c = x["code"]
        x["screen_hits"] = hit_count.get(c, 0)
        x["final_score"] = (
            x["strength"]
            + W_DIVERSITY * float(x.get("口径多样性") or 0)
            + W_EXPERTS * float(x.get("参与专家数") or 0)
            + W_SCREEN_HIT * x["screen_hits"]
        )
    # final_score 降序,code 升序保证确定性
    ranked = sorted(kept, key=lambda x: (-x["final_score"], x["code"]))
    # 行业限流(单行业最多 ind_cap 只);行业缺失(None)时**不限流**(无从判断集中度,
    # 早期历史日 council 行业整列为 None,若并入单桶会误杀,把池子错误压到 min_n)。
    out, ind_used, overflow = [], {}, []
    for x in ranked:
        ind = x.get("行业")
        if not ind or ind_used.get(ind, 0) < ind_cap:
            out.append(x)
            if ind:
                ind_used[ind] = ind_used.get(ind, 0) + 1
        else:
            overflow.append(x)
        if len(out) >= cap:
            break
    if len(out) < min_n:  # 行业限流误伤 → 用溢出票按序补到 min_n(仍 ≤cap)
        for x in overflow:
            if len(out) >= min(min_n, cap):
                break
            out.append(x)
        out.sort(key=lambda x: (-x["final_score"], x["code"]))
    return out[:cap]


# ── 编排:v1 兼容接口 ────────────────────────────────────────────────────────
def build_pool_v2(data_root, date: str, cap: int = CAP, min_n: int = MIN):
    """编排三段,返回 (pool: list[str], meta: dict) —— 与 shadow_pool.build_pool 同形状。"""
    council_rows, flags = load_council(data_root, date)
    base = strength_base(council_rows, data_root, date, k=K_BASE)
    kept, dropped, soft = quality_gates(base, data_root, date)
    final = cross_confirm_rank(kept, data_root, date, cap=cap, min_n=min_n)
    pool = [x["code"] for x in final]
    meta = {
        "date": date,
        "version": "v2",
        "council_flags": flags,
        "base_size": len(base),
        "kept_after_gates": len(kept),
        "dropped": dropped,
        "pool_size": len(pool),
        "soft_tags": {x["code"]: x.get("soft_tags", []) for x in final if x.get("soft_tags")},
        "detail": [
            {"code": x["code"], "行业": x.get("行业"), "strength": round(x["strength"], 4),
             "final_score": round(x["final_score"], 4), "screen_hits": x["screen_hits"],
             "soft_tags": x.get("soft_tags", [])}
            for x in final
        ],
    }
    return pool, meta


if __name__ == "__main__":
    DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
    days = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
            "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
            "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]
    for d in days:
        pool, meta = build_pool_v2(DR, d)
        print(f"{d}: base={meta['base_size']} kept={meta['kept_after_gates']} "
              f"pool={meta['pool_size']} dropped={len(meta['dropped'])}")
        print(f"    pool={','.join(pool)}")
