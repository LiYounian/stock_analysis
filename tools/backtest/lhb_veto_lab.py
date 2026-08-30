"""龙虎榜「否决 / 反转」信号的**严格评测**(WI-6 Phase 2 —— veto 专线)。

命题:龙虎榜作多头 alpha 已证为负(见 lhb_block_lab)。本 lab 把它**反过来**当风控否决/
反转信号评测其价值,回答三问:

  ① **否决价值有多大**:净买上榜票 T+1 开盘入、持有 H 日的**净超额**(去沪深300、扣往返成本),
     取负号即"否决/离场所**避免的**跑输幅度"。按 H∈{1,5,10}、按年、按 net_buy_ratio 分档看稳健性。
  ② **反转价值有没有**:净卖上榜票 H1 短反弹的净超额,扣成本后是否还剩正。
  ③ **是否稳定 + 显著**:按交易日聚类 bootstrap(H0 平均超额=0)、net_buy_ratio 前向 rank-IC。

===== 防未来函数(红线)=====
复用 lhb_block_lab:入场=上榜日 T 的 **T+1 开盘**,退出=**T+H 收盘**,沪深300按日去市场超额;
方向/信号仅用披露日 ≤ T−1 信息,前向收益仅作标签。绝不用上榜日当日盘中/收盘信息。

用法:
  python -m tools.backtest.lhb_veto_lab run [--start 20240101] [--end 20251231] [--out PATH]
非投资建议;历史回测≠未来保证。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest import lhb_block_lab as LBL
from tools.backtest import lhb_veto as VETO
from tools.backtest.eval_v3 import stats as _st
from tools.backtest.eval_v3.prices import PriceBook

logger = logging.getLogger("backtest.lhb_veto")

HORIZONS = (1, 5, 10)             # 持有交易日:T+1 开盘 → T+H 收盘
COST_RT = LBL.COST_RT             # 往返成本 0.2%(与既有回测一致)
BENCH = LBL.BENCH                 # 沪深300
OUT_DEFAULT = "data/analysis/backtest/lhb_veto_lab.json"
_DISCLAIMER = ("龙虎榜盘后披露,入场=T+1开盘、退出=T+H收盘(防未来函数);沪深300按日去市场超额,"
               "按交易日聚类bootstrap;扣往返0.2%成本。**否决/反转风控信号**,非多头买入建议;"
               "历史回测≠未来保证,非投资建议。")


# ═════════════════════ ① 一腿统计(按日聚类超额 + 净超额 + rank-IC) ═════════════════════
def _leg(sub: pd.DataFrame, seed: int = 20260828) -> dict:
    """某分组×某视界:按交易日聚类的毛/净超额 + 双边 p + net_buy_ratio 前向 rank-IC。

    avoided_underperf_net = −净超额:否决/离场该组所**避免**的净跑输(正=否决有价值)。
    """
    sub = sub.dropna(subset=["stk_ret", "bench_ret"])
    if sub.empty:
        return {"n": 0}
    by_day = list(sub.groupby("ev_date"))
    strat_day = [g["stk_ret"].to_numpy(float) for _, g in by_day]
    mkt_day = [float(g["bench_ret"].iloc[0]) for _, g in by_day]
    gross = _st.cluster_bootstrap_excess(strat_day, mkt_day, seed=seed)
    net = _st.cluster_bootstrap_excess([a - COST_RT for a in strat_day], mkt_day, seed=seed)
    pairs = []
    for _, g in by_day:
        gg = g.dropna(subset=["sig", "excess"])
        if len(gg) >= 5:
            pairs.append((gg["sig"].to_numpy(float), gg["excess"].to_numpy(float)))
    ic = _st.rank_ic(pairs) if pairs else {"mean_ic": None, "p_value": None}
    net_ex = net.get("excess")
    return {
        "n": int(len(sub)), "n_days": int(sub["ev_date"].nunique()),
        "mean_stk_ret": round(float(sub["stk_ret"].mean()), 4),
        "gross_excess": gross.get("excess"), "gross_p": gross.get("p_value"),
        "net_excess": net_ex, "net_p": net.get("p_value"),
        "net_ci": [net.get("lo"), net.get("hi")],
        "avoided_underperf_net": (round(-net_ex, 4) if net_ex is not None else None),
        "rank_ic": ic.get("mean_ic"), "rank_ic_p": ic.get("p_value"),
    }


# ═════════════════════ ② 各视图(总体 / 按年 / 净买占比分档) ═════════════════════
def _panel_by_direction(ret: pd.DataFrame) -> dict:
    """总体:各视界 × {全部, 净买dir=+1(否决腿), 净卖dir=−1(反转腿)}。"""
    out = {}
    for h in HORIZONS:
        rh = ret[ret["h"] == h]
        out[f"H{h}"] = {
            "all": _leg(rh),
            "net_buy(dir=+1)": _leg(rh[rh["direction"] == 1]),
            "net_sell(dir=-1)": _leg(rh[rh["direction"] == -1]),
        }
    return out


def _panel_by_year(ret: pd.DataFrame) -> dict:
    """按年分层:每年 × 各视界 × {净买腿, 净卖腿} 的净超额稳健性。"""
    ret = ret.assign(year=ret["ev_date"].str[:4])
    out = {}
    for yr, ry in ret.groupby("year"):
        blk = {}
        for h in HORIZONS:
            rh = ry[ry["h"] == h]
            blk[f"H{h}"] = {
                "net_buy(dir=+1)": _leg(rh[rh["direction"] == 1]),
                "net_sell(dir=-1)": _leg(rh[rh["direction"] == -1]),
            }
        out[str(yr)] = blk
    return out


def _panel_by_ratio_bucket(ret: pd.DataFrame, nbuckets: int = 4) -> dict:
    """净买腿(dir=+1)按 net_buy_ratio 分位数分档:检验"追高越猛、见光死越狠"的单调性。"""
    out = {}
    for h in HORIZONS:
        rh = ret[(ret["h"] == h) & (ret["direction"] == 1)].dropna(subset=["sig"])
        if len(rh) < nbuckets * 10:
            out[f"H{h}"] = {"note": f"样本不足分档(n={len(rh)})"}
            continue
        try:
            qs = pd.qcut(rh["sig"], nbuckets, labels=False, duplicates="drop")
        except ValueError:
            out[f"H{h}"] = {"note": "分位切分失败"}
            continue
        rh = rh.assign(_q=qs)
        buckets = []
        for q, g in rh.groupby("_q"):
            stat = _leg(g)
            buckets.append({
                "bucket": int(q),
                "ratio_range": [round(float(g["sig"].min()), 2), round(float(g["sig"].max()), 2)],
                "n": stat.get("n"), "net_excess": stat.get("net_excess"),
                "net_p": stat.get("net_p"),
            })
        out[f"H{h}"] = buckets
    return out


# ═════════════════════ ③ 否决模块自校验(生产模块 ↔ 诊断口径对齐) ═════════════════════
def _module_self_check(events: pd.DataFrame, ret: pd.DataFrame) -> dict:
    """用生产 veto 模块对每条上榜事件在 T+1(=list_date 次日)做裁决,核验:

    entry_veto 触发集 应 == 净买腿(dir=+1)。汇总触发集在 H5 的净超额,证明"模块所否决的正是
    那批见光死票"。防未来函数已由 verdict_from_events 内部(list_date < as_of)保证。
    """
    # 事件级方向表(去 h 维,一事件一行)
    ev1 = ret[ret["h"] == 5][["code", "ev_date", "direction", "sig", "stk_ret", "bench_ret",
                              "excess"]].copy()
    if ev1.empty:
        return {"note": "无 H5 样本"}
    triggered = []
    for r in ev1.itertuples(index=False):
        as_of = (pd.Timestamp(r.ev_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        ev_dict = [{"list_date": r.ev_date, "direction": int(r.direction), "net_buy_ratio": r.sig}]
        v = VETO.verdict_from_events(ev_dict, as_of, mode=VETO.MODE_ENTRY_VETO)
        triggered.append(bool(v.triggered))
    ev1 = ev1.assign(vetoed=triggered)
    n_veto = int(ev1["vetoed"].sum())
    n_dirbuy = int((ev1["direction"] == 1).sum())
    aligned = bool((ev1["vetoed"] == (ev1["direction"] == 1)).all())
    leg = _leg(ev1[ev1["vetoed"]])
    return {
        "n_vetoed": n_veto, "n_net_buy_dir": n_dirbuy,
        "veto_equals_net_buy_leg": aligned,   # 应 True:模块否决集 == 净买腿
        "vetoed_H5_net_excess": leg.get("net_excess"), "vetoed_H5_net_p": leg.get("net_p"),
        "vetoed_H5_avoided_underperf": leg.get("avoided_underperf_net"),
    }


# ═════════════════════ ④ 结论判定 ═════════════════════
def _verdict(panel: dict, by_year: dict) -> dict:
    """自动化结论:否决价值是否稳定(方向一致 + 显著),反转是否扣成本后存活。"""
    def sig_neg(leg):   # 净超额显著为负(否决有价值)
        ne, p = leg.get("net_excess"), leg.get("net_p")
        return ne is not None and ne < 0 and p is not None and p < 0.1

    h5_buy = panel.get("H5", {}).get("net_buy(dir=+1)", {})
    h1_buy = panel.get("H1", {}).get("net_buy(dir=+1)", {})
    h1_sell = panel.get("H1", {}).get("net_sell(dir=-1)", {})
    # 按年一致性:净买腿逐年净超额是否都为负
    years_buy_neg = []
    for yr, blk in by_year.items():
        leg = blk.get("H5", {}).get("net_buy(dir=+1)", {})
        ne = leg.get("net_excess")
        years_buy_neg.append(ne is not None and ne < 0)
    rev_net = h1_sell.get("net_excess")
    return {
        "否决腿H5显著负(见光死)": sig_neg(h5_buy),
        "否决腿H1显著负(追高即跌)": sig_neg(h1_buy),
        "否决腿逐年净超额均为负": bool(years_buy_neg) and all(years_buy_neg),
        "净买H5避免跑输(净,%)": h5_buy.get("avoided_underperf_net"),
        "反转腿H1净超额(扣成本,%)": rev_net,
        "反转扣成本后仍正": rev_net is not None and rev_net > 0,
        "建议用法": _usage_reco(h5_buy, h1_buy, h1_sell),
    }


def _usage_reco(h5_buy, h1_buy, h1_sell) -> str:
    parts = []
    if (h5_buy.get("net_excess") or 0) < 0:
        parts.append("净买上榜→入选否决/持仓离场(H5见光死最稳)")
    if (h1_buy.get("net_excess") or 0) < 0:
        parts.append("H1即已跑输,可作即时否决")
    rev = h1_sell.get("net_excess")
    if rev is not None and rev > 0:
        parts.append("净卖H1弱反转扣成本后微正,仅可小仓试探")
    else:
        parts.append("净卖反转扣成本后不成立,不建议做多反弹")
    return ";".join(parts) if parts else "证据不足"


# ═════════════════════ ⑤ 编排 ═════════════════════
def run(start: str, end: str, out: str | None = OUT_DEFAULT) -> dict:
    """拉龙虎榜事件 → T+1 前向收益(H1/5/10)→ 否决/反转多视图评测 → 落 JSON。"""
    pb = PriceBook()
    bench = LBL._Bench(start, end)
    ev = LBL.fetch_events("lhb", start, end)
    logger.info("龙虎榜事件 %d 条", len(ev))
    if ev.empty:
        rep = {"window": [start, end], "error": "无龙虎榜事件"}
    else:
        ret = LBL.compute_returns(ev, pb, bench, horizons=HORIZONS)
        logger.info("可算前向收益样本 %d(事件×视界)", len(ret))
        panel = _panel_by_direction(ret)
        by_year = _panel_by_year(ret)
        rep = {
            "window": [start, end], "bench": BENCH, "cost_rt": COST_RT,
            "horizons": list(HORIZONS), "disclaimer": _DISCLAIMER,
            "n_events": int(len(ev)),
            "overall_by_direction": panel,
            "by_year": by_year,
            "net_buy_ratio_buckets": _panel_by_ratio_bucket(ret),
            "veto_module_self_check": _module_self_check(ev, ret),
            "conclusion": _verdict(panel, by_year),
        }
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("报告已写 %s", out)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description="龙虎榜否决/反转信号评测(防未来函数)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="跑评测")
    r.add_argument("--start", default="20240101")
    r.add_argument("--end", default="20251231")
    r.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "run":
        rep = run(args.start, args.end, args.out)
        print(json.dumps(rep.get("conclusion", rep), ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
