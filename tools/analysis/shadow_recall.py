"""Shadow 候选池召回通路(recall)—— 补召 v2 强度打底漏掉的催化赢家。

承接设计:docs/计划/2026-09-14_候选池召回通路设计_recall补召.md
定位:**构建于 v2 之上、不改 shadow_pool_v2.py 主逻辑、不碰生产**。`build_pool_v2r` 与
`shadow_pool.build_pool` / `shadow_pool_v2.build_pool_v2` **同形状**(返回 (pool, meta)),
故现有 shadow_run.py / shadow_score.py **零改动**即可 v1/v2/v2r 三臂对拍。

═══ 为什么要召回 ═══
v2 的强度打底宇宙 = council top「看多」∩ 有 record(每日 ~20 行的**窄漏斗**)。选择性策略
(量价放量/动量组合/最强选股/消息面确认/放量后缩量回踩…)在 v2 里只作段三 tie-break 微加分,
**不作宇宙来源** → 一只票即使被多个催化策略同时命中,只要 council 没排进 top 看多就不进候选池。

═══ 召回口径:screen≥2 多策略交叉确认(证据唯一带增益的通道)═══
16 日样本对 7 条通道实测(池外基线 mean_r=−0.42、次日≥5%赢家率 7.7%):朴素催化(资金流/情绪/
量价 单通道)**全部跑输基线**(追催化→均值回归,催化已 price-in);**唯一**同时改善 mean_r(+0.10)
与赢家率(2.1×)、体量可用(~4.6/日)的是 **screen≥2**(≥2 个选择性策略交叉确认)。这正是 v1 的
核心智慧(hit≥2),v2 把它当宇宙来源丢弃了,本通路补回。详见设计文档 §3。

═══ 防偷看(硬红线)═══
- 阈值 ≥2 是**先验**(沿用 v1 `hit≥2` 约定),非在这 16 日调优。
- **拒绝为住 002811/603270 降到 screen≥1**:它们在 09-02 只命中 1 策略,本通路 correctly 不召回;
  降阈值对着已知例子拟合 = 数据偷看。它们是"漏召回现象存在"的例子,不是拟合目标。
- 防未来:screens/闸门只用当日及以前数据;确定性(同输入同输出),单测锁死。

═══ 排序/cap 契约 ═══
- 最终池 = [强度候选(v2 build_pool_v2 产出,受保护不被挤出)] + [召回候选(按 hit_count 降序)];
  召回**恒排在强度之后**,tie-break 按最小命中策略榜长(越选择性越强)→ code。
- 召回独立上限 RECALL_CAP,总池 ≤ CAP_R(略宽于 v2 CAP 以护住强度主池);行业限流对合并后总池统一施加。
"""
from __future__ import annotations
import json
from pathlib import Path

from tools.analysis import shadow_pool
from tools.analysis import shadow_pool_v2 as v2

# ── 先验参数(第一性原理设定,非在 16 日上调优;改动须在文档说明理由)──────────────
MIN_HITS = 2      # 召回门槛:命中的选择性策略数下限(沿用 v1 hit≥2 交叉确认约定)
RECALL_CAP = 5    # 单日召回补入上限(召回增量 ~4.6/日,cap 5 略高于均值、不淹没主池)
CAP_R = 18        # 总池上限(v2 CAP=15 + 召回余量,保证强度主池不被召回挤出)
IND_CAP = v2.IND_CAP  # 单行业上限,复用 v2(对合并后总池统一施加)


def _industry_of(council_ind: dict, rec: dict, code: str):
    """行业口径:council top 行业 → record meta.industry → meta.industry_asof → None。

    None 时不参与行业限流(与 v2 一致:行业缺失无从判断集中度,不误杀)。
    """
    ind = council_ind.get(code)
    if ind:
        return ind
    meta = rec.get("meta") or {}
    return meta.get("industry") or meta.get("industry_asof")


def _soft_tags(rec: dict) -> list[str]:
    """复用 v2 段二的软标签口径(摊薄/解禁/筹码高位/资金转弱),供 DeepSeek 研判读。

    与 v2.quality_gates 内联口径一致(常量同源);召回票也打软标签,信息对齐。
    """
    tags: list[str] = []
    fin = rec.get("financing") or {}
    unlock = ((fin.get("解禁") or {}).get("未来90日占流通_pct"))
    if isinstance(unlock, (int, float)) and unlock > v2.UNLOCK_PCT_HI:
        tags.append(f"临近解禁{unlock:.1f}%")
    cb = ((fin.get("可转债") or {}).get("潜在摊薄_pct"))
    if isinstance(cb, (int, float)) and cb > v2.CONVBOND_DILUTE_HI:
        tags.append(f"可转债摊薄{cb:.1f}%")
    chip = rec.get("chip") or {}
    prof = chip.get("获利比例")
    if isinstance(prof, (int, float)) and prof > v2.CHIP_PROFIT_HI:
        tags.append(f"高位获利盘{prof*100:.0f}%")
    ff = rec.get("fundflow") or {}
    f5 = ff.get("近5日主力合计")
    if isinstance(f5, (int, float)) and f5 < 0:
        tags.append("近5日主力净流出")
    return tags


def screen_hit_counts(data_root, date: str):
    """复用 shadow_pool._screens,统计每票命中的选择性策略数 + 最小命中榜长。

    返回 (hit: {code:int}, min_screen_len: {code:int})。
    min_screen_len 越小=命中的策略越选择性(榜越短)→ 越强,作 tie-break。
    """
    screens = shadow_pool._screens(data_root, date)
    hit: dict[str, int] = {}
    min_len: dict[str, int] = {}
    for _name, codes in screens.items():
        L = len(codes)
        for c in set(codes):  # 同策略内去重,防同榜重复计数
            hit[c] = hit.get(c, 0) + 1
            if c not in min_len or L < min_len[c]:
                min_len[c] = L
    return hit, min_len


def recall_candidates(data_root, date: str, base_codes, min_hits: int = MIN_HITS):
    """召回候选:screen≥min_hits ∧ 池外 ∧ 过硬闸门 ∧ 非 council 看空。

    输入:base_codes = 强度主池 code 集合(这些已入池,不再召回)。
    输出:排序后 list[dict]{code, hit_count, min_screen_len, 行业, soft_tags, origin, dropped?}。
    硬闸门复用 v2(龙虎否决 / 流动性截面分位+地板);额外守卫:council 明确看空的票不召回。
    """
    base_set = set(base_codes)
    hit, min_len = screen_hit_counts(data_root, date)

    council_rows, _ = v2.load_council(data_root, date)
    bearish: set[str] = set()
    council_ind: dict[str, str] = {}
    for r in council_rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("code") or "").zfill(6)
        if r.get("综合方向") == "看空":
            bearish.add(code)
        if r.get("行业"):
            council_ind[code] = r.get("行业")

    liq_q = v2._daily_mktcap_quantile(data_root, date, v2.LIQ_PCTL)
    liq_cut = v2.LIQ_FLOOR_YI if liq_q is None else max(liq_q, v2.LIQ_FLOOR_YI)

    cand = []
    for code, h in hit.items():
        if h < min_hits:
            continue
        if code in base_set:          # 已在强度主池,不重复召回
            continue
        if len(code) != 6 or not code.isdigit():
            continue
        if code in bearish:           # council 明确看空:不召回(第一性原理守卫)
            continue
        rec = v2._record(data_root, date, code)
        if not rec:                   # 无 record 不可分析,天然收敛
            continue
        # 硬闸门 1:龙虎榜否决
        if (rec.get("lhb_veto") or {}).get("triggered") is True:
            continue
        # 硬闸门 2:流动性下限(当日截面分位 ∨ 绝对地板)
        m = (rec.get("valuation") or {}).get("mktcap_yi")
        if isinstance(m, (int, float)) and m < liq_cut:
            continue
        cand.append({
            "code": code,
            "hit_count": h,
            "min_screen_len": min_len.get(code, 10**9),
            "行业": _industry_of(council_ind, rec, code),
            "soft_tags": _soft_tags(rec),
            "origin": f"recall/screen_x{h}",
        })
    # hit_count 降序 → 最小命中榜长升序(越选择性越强)→ code 升序(确定性)
    cand.sort(key=lambda x: (-x["hit_count"], x["min_screen_len"], x["code"]))
    return cand


def build_pool_v2r(data_root, date: str, cap_r: int = CAP_R,
                   recall_cap: int = RECALL_CAP, min_hits: int = MIN_HITS,
                   ind_cap: int = IND_CAP):
    """编排:v2 强度主池(受保护)+ screen≥min_hits 召回补入。

    返回 (pool: list[str], meta: dict) —— 与 build_pool / build_pool_v2 同形状。
    """
    base_pool, base_meta = v2.build_pool_v2(data_root, date)
    base_codes = list(base_pool)                 # 强度主池,顺序即 v2 排序
    base_set = set(base_codes)

    # 行业占用:从 v2 meta.detail 取强度主池各票行业
    detail = {d["code"]: d for d in base_meta.get("detail", [])}
    ind_used: dict[str, int] = {}
    for c in base_codes:
        ind = detail.get(c, {}).get("行业")
        if ind:
            ind_used[ind] = ind_used.get(ind, 0) + 1

    cands = recall_candidates(data_root, date, base_set, min_hits=min_hits)

    out = list(base_codes)                       # 强度主池恒在前,受保护
    recalled: list[dict] = []
    skipped_industry: list[dict] = []
    for x in cands:
        if len(recalled) >= recall_cap or len(out) >= cap_r:
            break
        ind = x.get("行业")
        if ind and ind_used.get(ind, 0) >= ind_cap:  # 行业已满,跳过(记录)
            skipped_industry.append(x)
            continue
        out.append(x["code"])
        recalled.append(x)
        if ind:
            ind_used[ind] = ind_used.get(ind, 0) + 1

    meta = {
        "date": date,
        "version": "v2r",
        "base_pool_size": len(base_codes),
        "recall_added": len(recalled),
        "pool_size": len(out),
        "recall_cap": recall_cap,
        "cap_r": cap_r,
        "min_hits": min_hits,
        "recalled": [
            {"code": x["code"], "hit_count": x["hit_count"], "行业": x.get("行业"),
             "origin": x["origin"], "soft_tags": x.get("soft_tags", [])}
            for x in recalled
        ],
        "recall_pool_candidates": len(cands),
        "recall_skipped_industry": [x["code"] for x in skipped_industry],
        "soft_tags": {
            **base_meta.get("soft_tags", {}),
            **{x["code"]: x["soft_tags"] for x in recalled if x.get("soft_tags")},
        },
        "base_meta": base_meta,
    }
    return out, meta


if __name__ == "__main__":
    DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
    days = ["2026-08-11", "2026-08-13", "2026-08-14", "2026-08-18", "2026-08-20",
            "2026-08-24", "2026-08-26", "2026-08-28", "2026-08-31", "2026-09-01",
            "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-08", "2026-09-09", "2026-09-10"]
    for d in days:
        pool, meta = build_pool_v2r(DR, d)
        rc = ",".join(x["code"] for x in meta["recalled"])
        print(f"{d}: base={meta['base_pool_size']} +recall={meta['recall_added']} "
              f"pool={meta['pool_size']}  recalled=[{rc}]")
