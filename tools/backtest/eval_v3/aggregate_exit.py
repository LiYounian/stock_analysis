"""带离场可实现收益的三口径聚合(eval_v3.1)。

三口径并报 + 归因(承箱体3 教训"防好离场救烂选股"):
  ① **纯选股固定持有**(现状 eval_v3 口径):r_fh = close[idx+time_stop]/入场价 − 1(毛,不扣成本)。
  ② **带离场可实现净收益**(新):离场毛收益 − 往返成本。
  ③ **离场增量 = ② − ①**:离场规则相对"死拿固定持有"多赚/少赚多少。**成本可约掉**
     (③ = (gross−cost) − (fh_gross−cost) = gross − fh_gross),故离场增量口径与成本档无关,
     纯粹衡量"离场时机"的贡献——好离场救不了烂选股就会在这项现原形。

分档:
  · **全部选中票等权**(广筛口径,每只选中票都进):所有策略都有。
  · **Top-5 / Top-10**(按 rank_score 降序,仅可排序型有连续打分者):看"分数越高的票+离场"。

显著性:按**交易日聚类** bootstrap(复用 stats),独立单元=交易日。③离场增量的 CI/p 判"离场时机
是否显著非零";②相对买入持有全市场基准的超额判"带离场后还跑不跑赢大盘"。

防未来函数:离场只用该票入场后自身价格路径 + 策略自身每日 as-of 再选(危险信号);maturity 要求
idx+time_stop<n(与固定持有 horizon=time_stop 同一到期),三口径同一样本配对。
"""
from __future__ import annotations

import logging

import numpy as np

from . import exit_sim as _ex
from . import prices as _pr
from . import stats as _st

logger = logging.getLogger("backtest.eval_v3.exit_agg")

_BOOT_B = 1000
TOPN_LEVELS = (5, 10)


def build_membership(preds):
    """从统一预测记录表建每策略的**每日选中集** + **预测日集合**(供危险信号 as-of 判定)。

    membership[sid][date] = set(该日该策略选中的 code);pred_days[sid] = set(该策略所有预测日)。
    """
    membership: dict[str, dict[str, set]] = {}
    pred_days: dict[str, set] = {}
    for sid, g in preds.groupby("strategy_id"):
        m: dict[str, set] = {}
        for d, gg in g.groupby("pred_date"):
            m[str(d)] = set(gg["code"].astype(str).tolist())
        membership[sid] = m
        pred_days[sid] = set(str(x) for x in g["pred_date"].unique())
    return membership, pred_days


def _selector(sid, code, membership, pred_days):
    """返回 is_selected_on(bar_date)->Optional[bool]:非预测日→None(持有);在选中集→True;否则→False。"""
    m = membership.get(sid, {})
    pd_set = pred_days.get(sid, set())

    def is_sel(bar_date):
        if bar_date is None or bar_date not in pd_set:
            return None                       # 当日该策略没跑/无该日 → 无法判断 → 持有
        return str(code) in m.get(bar_date, set())
    return is_sel


def iter_matured_records(preds, book: _pr.PriceBook):
    """把预测记录展开为可跑离场的记录基元(只保留能定位 idx、long-only 的)。

    产出 list[dict]:{sid, strategy, stype, pred_date, code, rank_score, direction, rec, idx, idx2date}。
    """
    recs = []
    for r in preds.itertuples(index=False):
        if int(getattr(r, "direction", 1)) != 1:
            continue                          # 仅多头(存活策略均 long-only)
        rec = book.get(r.code)
        if rec is None:
            continue
        idx = book.idx_of(r.code, r.pred_date)
        if idx is None:
            continue
        idx2date = {i: d for d, i in rec[4].items()}
        recs.append({"sid": r.strategy_id, "strategy": r.strategy, "stype": r.stype,
                     "pred_date": str(r.pred_date), "code": str(r.code),
                     "rank_score": getattr(r, "rank_score", np.nan),
                     "rec": rec, "idx": idx, "idx2date": idx2date})
    return recs


def _fixed_hold_gross(rec, idx, time_stop):
    """① 固定持有毛收益% = close[idx+time_stop]/入场价 − 1(与 eval_v3 现状口径一致)。"""
    op, high, low, close, _ = rec
    n = len(close)
    if idx + time_stop >= n:
        return None
    entry, _ = _pr.entry_price(rec, idx)
    if entry is None:
        return None
    return (float(close[idx + time_stop]) / entry - 1.0) * 100.0


def evaluate(records, membership, pred_days,
             tp_grid=_ex.TP_GRID, sl_grid=_ex.SL_GRID, ts_grid=_ex.TIME_STOP_GRID,
             danger_variants=(False, True)):
    """对每条记录 × 每参数组(tp,sl,ts,danger)跑固定持有①与离场毛收益②。

    返回 list[dict] 长表(一行=记录×参数组):
      {sid, strategy, stype, pred_date, code, rank_score, tp, sl, ts, danger,
       fh_gross(①), ex_gross(②毛,未扣成本), inc(③=ex_gross−fh_gross), reason, ambiguous, hold_days}
    仅保留两口径都成熟的行(idx+ts<n),保证配对同样本。
    """
    rows = []
    for rr in records:
        rec, idx = rr["rec"], rr["idx"]
        sel = _selector(rr["sid"], rr["code"], membership, pred_days)
        for ts in ts_grid:
            fh = _fixed_hold_gross(rec, idx, ts)
            if fh is None:
                continue
            for danger in danger_variants:
                is_sel = sel if danger else None
                i2d = rr["idx2date"] if danger else None
                for tp in tp_grid:
                    for sl in sl_grid:
                        o = _ex.simulate_long_exit(rec, idx, tp, sl, ts, cost_pct=0.0,
                                                   is_selected_on=is_sel, idx2date=i2d)
                        if not o["matured"]:
                            continue
                        rows.append({
                            "sid": rr["sid"], "strategy": rr["strategy"], "stype": rr["stype"],
                            "pred_date": rr["pred_date"], "code": rr["code"],
                            "rank_score": rr["rank_score"], "tp": tp, "sl": sl, "ts": ts,
                            "danger": danger, "fh_gross": round(fh, 4),
                            "ex_gross": o["gross_pct"], "inc": round(o["gross_pct"] - fh, 4),
                            "reason": o["exit_reason"], "ambiguous": o["path_ambiguous"],
                            "hold_days": o["hold_days"]})
    return rows


def _topn_mask(sub_rows):
    """按 (pred_date) 内 rank_score 降序标注每行属于 Top5/Top10。返回两个 set(行下标)。

    sub_rows:同一 (sid,tp,sl,ts,danger) 的行列表(dict,含 rank_score/pred_date)。
    无有效 rank_score(全 NaN)→ 两 set 皆空(该策略无连续打分,Top-N 不适用)。
    """
    idxs_by_day: dict[str, list] = {}
    for i, r in enumerate(sub_rows):
        rs = r.get("rank_score")
        if rs is None or (isinstance(rs, float) and np.isnan(rs)):
            continue
        idxs_by_day.setdefault(r["pred_date"], []).append((i, float(rs)))
    top5, top10 = set(), set()
    for _d, lst in idxs_by_day.items():
        lst.sort(key=lambda t: t[1], reverse=True)
        for rank, (i, _s) in enumerate(lst):
            if rank < 5:
                top5.add(i)
            if rank < 10:
                top10.add(i)
    return top5, top10


def _cluster_stats(day_arrays):
    """按日聚类的均值 CI + 双边 p(H0:均值=0)。day_arrays=list[每日逐记录值 np.array]。"""
    ci = _st.cluster_bootstrap_ci(day_arrays, "mean", B=_BOOT_B)
    ex = _st.cluster_bootstrap_excess(day_arrays, [0.0] * len(day_arrays), B=_BOOT_B)
    return ci, ex


def _cell(rows_slice, uni_ret, ts, cost):
    """一个 (策略,分档,tp,sl,ts,danger) × 成本档 的三口径单元。rows_slice=已筛该组合的行 dict 列表。"""
    if not rows_slice:
        return None
    fh = np.array([r["fh_gross"] for r in rows_slice], float)
    ex = np.array([r["ex_gross"] for r in rows_slice], float)      # 毛(未扣成本)
    inc = np.array([r["inc"] for r in rows_slice], float)
    n = len(rows_slice)
    days = sorted({r["pred_date"] for r in rows_slice})
    # 按日分组 inc / ex(用于聚类)
    by_day_inc, by_day_ex = {}, {}
    for r in rows_slice:
        by_day_inc.setdefault(r["pred_date"], []).append(r["inc"])
        by_day_ex.setdefault(r["pred_date"], []).append(r["ex_gross"])
    inc_days = [np.array(by_day_inc[d], float) for d in days]
    ex_days = [np.array(by_day_ex[d], float) for d in days]
    ci_inc, p_inc = _cluster_stats(inc_days)
    # ② 相对买入持有全市场基准的超额(同 horizon=ts;成本从策略侧扣)
    mkt_day = [float(uni_ret.get((d, ts), np.array([])).mean())
               if len(uni_ret.get((d, ts), np.array([]))) else None for d in days]
    ex_net_days = [a - cost for a in ex_days]
    exc = _st.cluster_bootstrap_excess(ex_net_days, mkt_day, B=_BOOT_B)
    # 离场原因分布
    reasons: dict[str, int] = {}
    for r in rows_slice:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    amb = float(np.mean([1.0 if r["ambiguous"] else 0.0 for r in rows_slice])) * 100
    ex_net_mean = float(ex.mean()) - cost
    return {
        "样本": n, "预测日数": len(days),
        "①固定持有均收益%": round(float(fh.mean()), 3),
        "②带离场净收益%": round(ex_net_mean, 3),
        "②胜率%": round(float((ex - cost > 0).mean()) * 100, 1),
        "③离场增量%": round(float(inc.mean()), 3),
        "③增量_聚类CI%": ([round(ci_inc["lo"], 3), round(ci_inc["hi"], 3)]
                          if ci_inc.get("lo") is not None else None),
        "③增量_聚类p值": p_inc.get("p_value"),
        "②超额vs买入持有全市场%": exc.get("excess"),
        "②超额_聚类p值": exc.get("p_value"),
        "基准_买入持有全市场%": (round(float(np.mean([m for m in mkt_day if m is not None])), 3)
                              if any(m is not None for m in mkt_day) else None),
        "平均持有天数": round(float(np.mean([r["hold_days"] for r in rows_slice])), 2),
        "path_ambiguous占比%": round(amb, 1),
        "离场原因分布": reasons,
        "成本档%": cost,
    }


def aggregate_exit(rows, uni_ret, costs=_ex.COST_GRID,
                   tp_grid=_ex.TP_GRID, sl_grid=_ex.SL_GRID, ts_grid=_ex.TIME_STOP_GRID,
                   danger_variants=(False, True)):
    """顶层聚合:每策略 × 分档(全部/Top5/Top10)× 参数组 × 成本档 → 三口径单元。

    返回 {sid: {"策略名","类型","分档": {level: {"组合key": cell}}}},组合 key = 'tp{tp}_sl{sl}_ts{ts}_danger{0/1}_cost{c}'。
    """
    # 先按 sid 分组
    by_sid: dict[str, list] = {}
    for r in rows:
        by_sid.setdefault(r["sid"], []).append(r)
    out: dict = {}
    for sid, srows in by_sid.items():
        strategy = srows[0]["strategy"]
        stype = srows[0]["stype"]
        entry = {"策略名": strategy, "类型": stype, "分档": {}}
        for tp in tp_grid:
            for sl in sl_grid:
                for ts in ts_grid:
                    for danger in danger_variants:
                        combo = [r for r in srows if r["tp"] == tp and r["sl"] == sl
                                 and r["ts"] == ts and r["danger"] == danger]
                        if not combo:
                            continue
                        top5, top10 = _topn_mask(combo)
                        levels = {"全部": list(range(len(combo)))}
                        if top5:
                            levels["Top5"] = sorted(top5)
                        if top10:
                            levels["Top10"] = sorted(top10)
                        for lvl, idxs in levels.items():
                            slice_rows = [combo[i] for i in idxs]
                            for cost in costs:
                                cell = _cell(slice_rows, uni_ret, ts, cost)
                                if cell is None:
                                    continue
                                key = f"tp{tp:g}_sl{sl:g}_ts{ts}_danger{int(danger)}_cost{cost:g}"
                                entry["分档"].setdefault(lvl, {})[key] = cell
        out[sid] = entry
    return out


def best_combos(agg, level="全部", danger=0, prefer_cost=0.1):
    """每策略在指定分档/危险信号开关/成本档下,按②带离场净收益% 降序挑最优参数组。

    返回 {sid: {"策略名","最优key","cell"}}。
    """
    out = {}
    for sid, e in agg.items():
        cells = e.get("分档", {}).get(level, {})
        cand = [(k, c) for k, c in cells.items()
                if k.endswith(f"danger{danger}_cost{prefer_cost:g}")]
        if not cand:
            continue
        k, c = max(cand, key=lambda kc: (kc[1]["②带离场净收益%"] or -1e9))
        out[sid] = {"策略名": e["策略名"], "类型": e["类型"], "最优key": k, "cell": c}
    return out
