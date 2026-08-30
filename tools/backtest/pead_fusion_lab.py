"""WI-6 Phase 1-a —— PEAD 消息面层对"技术候选池"是否加净增量(历史回测, 防未来函数)。

命题(见任务书):纯技术面扣成本≈市场,净收益唯一出路 = 消息面正交增量层叠加到技术候选池。
PEAD/业绩预告实质利空是"成色最好、且唯一可严谨历史回测"的消息代理(有真实公告日、防未来函数红线完整)。
本实验室用"可历史回测"纪律,证明(或证伪)PEAD 层对技术候选池的净增量。

===== 实验设计 =====
技术候选池:复用 fusion_lab 的技术面板 (data/analysis/backtest/fusion_panel.parquet;
  每 (票, 信号日) 一行 + 前向 5 日收益 + 三专家/结构态信号),与 eval_v3/fusion_lab 同口径同面板。
  技术评分器:① 融合分 F3(全量:os+rev+反趋势+结构态,逐日横截面 zscore 复合,fusion_lab 主线)
              ② council 删趋势 V2(存活合议基线)。两者都测,互为稳健性。

PEAD 信号(as-of, 公告日锚定, 防未来函数红线):
  对面板每行 (code, T),查该票**公告日 ≤ T 且在近 lookback 自然日内**的最近一条业绩预告(归母口径):
    方向 = +1(正超预期:预增/略增/扭亏/续盈/减亏) / −1(实质利空:预减/略减/预亏/首亏/续亏/增亏) / 0(无近期预告)。
  **只用公告日 ≤ T 的信息** → 严格 as-of,前向收益仅取 T 之后价(面板已保证)。
  lookback 默认 90 自然日(PEAD 漂移约一个季度;并测 60 做敏感性)。

两种用法(同 N、同市场基准、同交易日,只差 PEAD 一层 → 增量归因干净):
  · 断点(veto):技术 Top-N 里剔除有实质利空(pead_dir==−1)的候选,补进下一名 → 仍取 N 只。
  · 加权(tilt):tilt_score = z(技术分) + w·z(pead_dir),重排取 Top-N(正超预期上浮/利空下沉)。

对比与判据:
  · 每腿 vs 全宇宙的 gross/net(扣往返成本 0.1%/0.2%)超额 + 按日聚类 p(复用 eval_v3.stats)。
  · **增量** = (叠 PEAD 腿) − (纯技术腿),按日配对聚类 bootstrap 出增量点估计 + p。
  · 预注册达标判据:叠 PEAD 后扣成本净超额有**显著正增量**(聚类 p<0.1)才算"消息面层有价值"。

用法:
  python -m tools.backtest.pead_fusion_lab fetch   # 只拉/缓存业绩预告事件
  python -m tools.backtest.pead_fusion_lab run [--panel P] [--lookback 90] [--out O]
非投资建议。历史回测≠未来保证。事件方向仅用公告日及之前信息,前向收益仅作标签。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest import fusion_lab as fl
from tools.backtest.eval_v3 import stats as _st

logger = logging.getLogger("backtest.pead_fusion")

_DISCLAIMER = ("历史回测≠未来保证,非投资建议。PEAD 方向仅用公告日≤T 的信息(as-of),"
               "前向收益仅作标签,断点/加权两用法同 N 同基准同交易日,增量按日聚类检验。")

# —— 业绩预告分档(与 backtest_pead 同口径:锁定归母净利润)——
POS_TYPES = {"预增", "略增", "扭亏", "续盈", "减亏"}
NEG_TYPES = {"预减", "略减", "预亏", "首亏", "续亏", "增亏"}
_NET_KEY = "归属于上市公司股东的净利润"

COSTS = fl.COSTS                                        # {"net_10bp":0.10,"net_20bp":0.20}
PANEL_DEFAULT = fl.PANEL_DEFAULT
OUT_DEFAULT = "data/analysis/backtest/pead_fusion_lab.json"

_SCRATCH = Path("/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
                "c4815bd5-1307-454f-8c30-8e64377b1bf3/scratchpad")
_PEAD_CACHE = _SCRATCH / "pead_events_full.parquet"


# ═════════════════════════ ① PEAD 事件表(公告日锚定) ═════════════════════════
def default_periods() -> list[str]:
    """覆盖面板跨度(2019~2026)所需报告期:季度末 20180930 ~ 20260630。
    向前多取一个季度,保证最早信号日(~2019-01)的近 90 日窗内公告能被覆盖。"""
    ends = ("0331", "0630", "0930", "1231")
    out = []
    for y in range(2018, 2027):
        for e in ends:
            p = f"{y}{e}"
            if "20180930" <= p <= "20260630":
                out.append(p)
    return out


def fetch_pead_events(periods=None, use_cache=True) -> pd.DataFrame:
    """拉各报告期业绩预告(归母口径),规整为事件表并缓存到本会话 scratch。

    Returns df[code, 报告期, 公告日期(Timestamp), 预告类型, 方向(+1/-1/0)]。
    每 (报告期,code) 保留**最早披露日**(首次预告 = 最干净事件锚)。akshare 某期失败 → 跳过。
    与 backtest_pead.fetch_forecasts 同口径,但覆盖全跨度报告期 + 自有缓存(不污染他会话)。
    """
    periods = list(periods) if periods else default_periods()
    if use_cache and _PEAD_CACHE.exists():
        df = pd.read_parquet(_PEAD_CACHE)
        if set(periods).issubset(set(df["报告期"].unique())):
            logger.info("命中 PEAD 事件缓存 %s (%d 条)", _PEAD_CACHE, len(df))
            return df[df["报告期"].isin(periods)].reset_index(drop=True)

    import akshare as ak
    frames = []
    for p in periods:
        try:
            raw = ak.stock_yjyg_em(date=p)
        except Exception as e:                          # noqa: BLE001
            logger.warning("stock_yjyg_em(%s) 失败,跳过: %s", p, e)
            continue
        if raw is None or len(raw) == 0:
            logger.warning("stock_yjyg_em(%s) 空", p)
            continue
        net = raw[raw["预测指标"].astype(str).str.contains(_NET_KEY, na=False)].copy()
        if net.empty:
            continue
        net["code"] = net["股票代码"].astype(str).str.zfill(6)
        net["报告期"] = p
        net["公告日期"] = pd.to_datetime(net["公告日期"], errors="coerce")
        net["预告类型"] = net["预告类型"].astype(str)
        net = net.dropna(subset=["公告日期"])
        net = net.sort_values("公告日期").drop_duplicates(subset=["报告期", "code"], keep="first")
        frames.append(net[["code", "报告期", "公告日期", "预告类型"]])
        logger.info("yjyg %s: %d 事件", p, len(net))
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["方向"] = out["预告类型"].map(
        lambda t: 1 if t in POS_TYPES else (-1 if t in NEG_TYPES else 0))
    try:
        _SCRATCH.mkdir(parents=True, exist_ok=True)
        out.to_parquet(_PEAD_CACHE)
        logger.info("PEAD 事件 → %s (%d 条)", _PEAD_CACHE, len(out))
    except Exception as e:                              # noqa: BLE001
        logger.warning("PEAD 事件缓存落盘失败: %s", e)
    return out


# ═════════════════════════ ② as-of 挂载 PEAD 方向到面板 ═════════════════════════
def attach_pead(pdf: pd.DataFrame, events: pd.DataFrame,
                lookback_days: int = 90) -> pd.DataFrame:
    """给面板每行 (code, date) 挂 pead_dir(as-of):
       公告日 ≤ date 且 (date − 公告日) ≤ lookback_days 的**最近一条**预告方向;无 → 0。

    防未来函数:严格用公告日 ≤ date 的信息。返回带 pead_dir/pead_gap 列的新面板(拷贝)。
    """
    pdf = pdf.copy()
    pdf["_dt"] = pd.to_datetime(pdf["date"])
    # 每 code 一张按公告日升序的事件表
    ev_by_code: dict[str, tuple] = {}
    for c, g in events[events["方向"] != 0].groupby("code"):
        g = g.sort_values("公告日期")
        ev_by_code[c] = (g["公告日期"].to_numpy("datetime64[ns]"),
                         g["方向"].to_numpy(int))

    dirs = np.zeros(len(pdf), int)
    gaps = np.full(len(pdf), np.nan)
    lb = np.timedelta64(lookback_days, "D")
    codes = pdf["code"].to_numpy()
    tds = pdf["_dt"].to_numpy("datetime64[ns]")
    for i in range(len(pdf)):
        rec = ev_by_code.get(codes[i])
        if rec is None:
            continue
        adates, advals = rec
        T = tds[i]
        # 最近一条公告日 ≤ T
        pos = np.searchsorted(adates, T, side="right") - 1
        if pos < 0:
            continue
        gap = (T - adates[pos]) / np.timedelta64(1, "D")
        if adates[pos] <= T and (T - adates[pos]) <= lb:
            dirs[i] = int(advals[pos])
            gaps[i] = float(gap)
    pdf["pead_dir"] = dirs
    pdf["pead_gap"] = gaps
    pdf.drop(columns=["_dt"], inplace=True)
    return pdf


# ═════════════════════════ ③ 技术评分器(复用 fusion_lab) ═════════════════════════
def build_tech_score(pdf: pd.DataFrame, ranker: str) -> str:
    """在面板上算技术评分列,返回列名。
       ranker='fusion_F3' → fusion_lab 全量等权融合分;'council_V2' → 删趋势合议分。"""
    if ranker == "fusion_F3":
        fl.compute_fusion(pdf, fl.CONFIGS["F3_全量(正+反趋势+结构态)"],
                          weights=None, score_col="_tech")
    elif ranker == "council_V2":
        fl.council_composite(pdf, {"技术趋势": 0, "超买超卖": 1, "拐点": 1}, "_tech")
    else:
        raise ValueError(f"未知 ranker: {ranker}")
    return "_tech"


# ═════════════════════════ ④ veto / tilt 评测 ═════════════════════════
def _day_groups(pdf: pd.DataFrame, tech_col: str) -> dict:
    """{date: (scores[], fwd5[], pead_dir[])}(仅 tech 分有限)。"""
    day: dict[str, list] = {}
    arr = pdf[["date", tech_col, "fwd5", "pead_dir"]].to_numpy(object)
    for dt, sc, fw, pd_ in arr:
        sc = float(sc) if sc is not None else float("nan")
        if not np.isfinite(sc):
            continue
        day.setdefault(dt, []).append((sc, float(fw), int(pd_)))
    return day


def _xs_z(v: np.ndarray) -> np.ndarray:
    """横截面 zscore;std=0/全 NaN → 全 0(退化不排序)。"""
    v = v.astype(float)
    mu = np.nanmean(v)
    sd = np.nanstd(v)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(v)
    return (v - mu) / sd


def eval_pead_layer(pdf: pd.DataFrame, tech_col: str, N: int,
                    tilt_w: float = 1.0, costs: dict | None = None,
                    seed: int = 20260828) -> dict:
    """一个技术评分器 + 一个 N,给出 纯技术 / veto / tilt 三腿的 gross/net 超额(按日聚类 p)
       + veto/tilt 相对纯技术的**增量**(按日配对聚类 p)。"""
    costs = costs or COSTS
    day = _day_groups(pdf, tech_col)

    tech_day, veto_day, tilt_day = [], [], []      # 每日 Top-N 逐票收益数组
    mkt_day = []                                    # 每日全宇宙均收益
    cover = {"days": 0, "veto_touched_days": 0, "tilt_touched_days": 0,
             "neg_in_topN": 0, "pos_in_topN": 0}
    for _dt, lst in day.items():
        if len(lst) < N:
            continue
        cover["days"] += 1
        sc = np.array([x[0] for x in lst], float)
        fw = np.array([x[1] for x in lst], float)
        pdd = np.array([x[2] for x in lst], int)
        mkt_day.append(float(fw.mean()))

        order = np.argsort(-sc)                     # 技术分降序
        # 纯技术 Top-N
        tech_idx = order[:N]
        tech_day.append(fw[tech_idx])
        cover["neg_in_topN"] += int((pdd[tech_idx] == -1).sum())
        cover["pos_in_topN"] += int((pdd[tech_idx] == 1).sum())

        # veto:按技术分降序,跳过 pead_dir==-1,取前 N(不足则取满可用)
        veto_pick = [i for i in order if pdd[i] != -1][:N]
        if len(veto_pick) >= 1:
            veto_day.append(fw[np.array(veto_pick, int)])
            if any(pdd[i] == -1 for i in tech_idx):
                cover["veto_touched_days"] += 1
        else:
            veto_day.append(fw[tech_idx])          # 极端:全被 veto,退回技术(不虚构)

        # tilt:z(tech)+w·z(pead_dir) 重排取 Top-N
        tscore = _xs_z(sc) + tilt_w * _xs_z(pdd.astype(float))
        tilt_idx = np.argsort(-tscore)[:N]
        tilt_day.append(fw[tilt_idx])
        if not np.array_equal(np.sort(tilt_idx), np.sort(tech_idx)):
            cover["tilt_touched_days"] += 1

    def _legs(strat_day):
        g = _st.cluster_bootstrap_excess(strat_day, mkt_day, seed=seed)
        rec = {"gross超额%": g.get("excess"), "gross_p": g.get("p_value"),
               "gross_CI%": [g.get("lo"), g.get("hi")], "聚类交易日数": g.get("n_days"),
               "均5日%": round(float(np.mean(np.concatenate(strat_day))), 3) if strat_day else None,
               "命中%": (round(float((np.concatenate(strat_day) > 0).mean()) * 100, 1)
                         if strat_day else None)}
        for cname, cost in costs.items():
            netx = _st.cluster_bootstrap_excess([a - cost for a in strat_day], mkt_day, seed=seed)
            rec[f"{cname}超额%"] = netx.get("excess")
            rec[f"{cname}_p"] = netx.get("p_value")
        return rec

    def _increment(pead_day, base_day):
        """PEAD 腿相对纯技术腿的**每日配对**增量(pead日均 − tech日均)聚类 p。
           成本对两腿等额(同 N),增量 gross==net,故只给一个增量口径。"""
        base_means = [float(a.mean()) if len(a) else np.nan for a in base_day]
        inc = _st.cluster_bootstrap_excess(pead_day, base_means, seed=seed)
        return {"增量%": inc.get("excess"), "p": inc.get("p_value"),
                "CI%": [inc.get("lo"), inc.get("hi")], "聚类交易日数": inc.get("n_days")}

    return {
        "N": N, "tilt_w": tilt_w, "覆盖": cover,
        "纯技术": _legs(tech_day),
        "veto断点": _legs(veto_day),
        "tilt加权": _legs(tilt_day),
        "增量_veto减技术": _increment(veto_day, tech_day),
        "增量_tilt减技术": _increment(tilt_day, tech_day),
    }


# ═════════════════════════ ④b PEAD 独立预测力诊断 ═════════════════════════
def diagnose_standalone(pdf: pd.DataFrame) -> dict:
    """在面板内直接看 PEAD 方向对 fwd5 的**独立**预测力(不叠技术):
       正/负/无 三组 fwd5 均值 + pead_dir 的按日聚类 rank-IC。
       这判断"增量为零"到底是'PEAD 本身在本样本没 edge'还是'被技术层吃掉了'。"""
    pos = pdf.loc[pdf.pead_dir == 1, "fwd5"]
    neg = pdf.loc[pdf.pead_dir == -1, "fwd5"]
    zero = pdf.loc[pdf.pead_dir == 0, "fwd5"]
    active = pdf[pdf.pead_dir != 0]
    pairs = []
    for _dt, g in active.groupby("date"):
        if len(g) >= 3 and g.pead_dir.nunique() > 1:
            pairs.append((g.pead_dir.to_numpy(float), g.fwd5.to_numpy(float)))
    ic = _st.rank_ic(pairs)
    return {
        "正组_fwd5均%": round(float(pos.mean()), 3) if len(pos) else None,
        "负组_fwd5均%": round(float(neg.mean()), 3) if len(neg) else None,
        "无信号组_fwd5均%": round(float(zero.mean()), 3) if len(zero) else None,
        "正减负%": round(float(pos.mean() - neg.mean()), 3) if len(pos) and len(neg) else None,
        "pead_dir_rankIC": ic.get("mean_ic"), "IC_p": ic.get("p_value"),
        "IC聚类日数": ic.get("n_days"),
    }


# ═════════════════════════ ⑤ 主流程 ═════════════════════════
def run(panel: str = PANEL_DEFAULT, lookback_days: int = 90,
        rankers=("fusion_F3", "council_V2"), topns=(10, 20),
        tilt_ws=(0.5, 1.0), out: str = OUT_DEFAULT,
        periods=None) -> dict:
    pdf0 = fl.load_panel(panel)
    events = fetch_pead_events(periods)
    if events.empty:
        return {"错误": "无 PEAD 事件", "免责": _DISCLAIMER}

    pdf0 = attach_pead(pdf0, events, lookback_days=lookback_days)
    n_active = int((pdf0["pead_dir"] != 0).sum())
    n_pos = int((pdf0["pead_dir"] == 1).sum())
    n_neg = int((pdf0["pead_dir"] == -1).sum())
    logger.info("面板 %d 行,近%d日有活跃 PEAD 信号 %d 行 (正%d/负%d, 覆盖率%.1f%%)",
                len(pdf0), lookback_days, n_active, n_pos, n_neg, n_active / len(pdf0) * 100)

    standalone = diagnose_standalone(pdf0)

    results = {}
    for ranker in rankers:
        pdf = pdf0.copy()
        tcol = build_tech_score(pdf, ranker)
        per_r = {}
        for N in topns:
            # tilt 权重敏感性:取多个 w,主口径用第一个
            per_w = {}
            for w in tilt_ws:
                per_w[f"tilt_w={w}"] = eval_pead_layer(pdf, tcol, N, tilt_w=w)
            per_r[f"Top{N}"] = per_w
        results[ranker] = per_r

    result = {
        "生成于": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "口径": ("技术候选池(fusion_panel, 同 eval_v3)→ 叠 PEAD as-of 层(veto/tilt)→ "
                 "gross/net(扣往返成本)超额 + 按日聚类 p + 配对增量 p;防未来函数;非投资建议"),
        "面板": {"文件": panel, "行": len(pdf0),
                 "覆盖交易日": int(pdf0["date"].nunique()),
                 "区间": [pdf0["date"].min(), pdf0["date"].max()]},
        "PEAD": {"lookback自然日": lookback_days, "活跃信号行": n_active,
                 "正": n_pos, "负": n_neg,
                 "覆盖率%": round(n_active / len(pdf0) * 100, 2),
                 "事件总数": int(len(events)), "报告期数": int(events["报告期"].nunique())},
        "成本口径": {k: f"{v}%/往返" for k, v in COSTS.items()},
        "PEAD独立预测力诊断": standalone,
        "结果": results,
        "免责": _DISCLAIMER,
    }
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    _print_summary(result)
    if out:
        print("→", out)
    return result


def _print_summary(r: dict) -> None:
    print("\n===== WI-6 Phase 1-a · PEAD 消息面层增量记分卡 =====")
    print("面板:", r["面板"]["行"], "行 /", r["面板"]["覆盖交易日"], "交易日 /",
          r["面板"]["区间"])
    print("PEAD:", r["PEAD"])
    print("成本:", r["成本口径"], "  (增量判据: 净超额显著正增量 聚类 p<0.1)")
    print("PEAD 独立预测力诊断:", r["PEAD独立预测力诊断"])

    def _leg(tag, o):
        print(f"    {tag:<10} gross={str(o['gross超额%']):>7}(p{o['gross_p']}) "
              f"net10={str(o['net_10bp超额%']):>7}(p{o['net_10bp_p']}) "
              f"net20={str(o['net_20bp超额%']):>7}(p{o['net_20bp_p']}) "
              f"| 均5日={o['均5日%']} 命中={o['命中%']}%")

    for ranker, per_r in r["结果"].items():
        print(f"\n—— 技术评分器: {ranker} ——")
        for topn, per_w in per_r.items():
            for wk, o in per_w.items():
                cov = o["覆盖"]
                print(f"  [{topn} · {wk}] 交易日={cov['days']} "
                      f"veto触发日={cov['veto_touched_days']} tilt改动日={cov['tilt_touched_days']} "
                      f"(TopN内 负={cov['neg_in_topN']} 正={cov['pos_in_topN']})")
                _leg("纯技术", o["纯技术"])
                _leg("veto断点", o["veto断点"])
                _leg("tilt加权", o["tilt加权"])
                iv, it = o["增量_veto减技术"], o["增量_tilt减技术"]
                print(f"    >> 增量 veto−技术 = {iv['增量%']}% (p={iv['p']}, CI={iv['CI%']}, "
                      f"日={iv['聚类交易日数']})  | tilt−技术 = {it['增量%']}% (p={it['p']}, "
                      f"CI={it['CI%']})")


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="WI-6 Phase 1-a PEAD 消息面层增量回测")
    ap.add_argument("cmd", choices=["fetch", "run"], help="fetch只拉事件 / run全量评测")
    ap.add_argument("--panel", default=PANEL_DEFAULT)
    ap.add_argument("--lookback", type=int, default=90)
    ap.add_argument("--topns", default="10,20")
    ap.add_argument("--tilt-ws", default="0.5,1.0")
    ap.add_argument("--out", default=OUT_DEFAULT)
    a = ap.parse_args(argv)

    if a.cmd == "fetch":
        ev = fetch_pead_events()
        print(f"PEAD 事件 {len(ev)} 条,报告期 {ev['报告期'].nunique() if len(ev) else 0} 个")
        return 0
    run(panel=a.panel, lookback_days=a.lookback,
        topns=tuple(int(x) for x in a.topns.split(",")),
        tilt_ws=tuple(float(x) for x in a.tilt_ws.split(",")),
        out=a.out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
