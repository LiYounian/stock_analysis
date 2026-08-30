"""WI-6 Phase 1-b —— 龙虎榜 / 大宗交易两事件源的**可回测性验证**(防未来函数)。

命题:这两个盘后披露事件源(有真实披露日 → 可历史回测)对 T+1 / 5 日前向收益
是否有**选择性**(方向可分、扣成本后仍有超额)。有则值得进一步接入,无则不接。

===== 防未来函数(红线) =====
事件盘后披露 → 最早可用 = **T+1 开盘**。本回测**入场 = 上榜日/交易日 T 的 T+1 开盘价**,
**退出 = T+H 收盘价**(H∈{1,5} 交易日),前向收益仅取披露日之后价,绝不用 T 当日信息。
东财自带的"上榜后 N 日收益"前视列已在采集器丢弃,这里也不使用(独立用前复权主档重算)。

===== 口径 =====
- 价格:tools.collectors.market 主档(前复权,离线),经 eval_v3.prices.PriceBook 取 T+1 入场。
- 市场基准:沪深300(000300)同窗口 T+1 开盘→T+H 收盘收益,做**按日超额**去市场。
- 显著性:eval_v3.stats.cluster_bootstrap_excess(按交易日聚类 bootstrap,H0: 平均超额=0)。
- 成本:往返 0.2%(20bp)从个股腿扣除,给净超额。
- 方向分组:龙虎榜按净买额符号(+1/−1);大宗按折溢率符号 + 机构买方(inst_buy)。
- 连续信号选择性:龙虎榜净买占比 / 大宗折溢率、成交额占流通市值 的按日 rank-IC。

用法:
  python -m tools.backtest.lhb_block_lab run [--start 20240101] [--end 20251231]
                                              [--sources lhb,block] [--out PATH]
非投资建议;历史回测≠未来保证。方向标签仅用披露日≤T-1 信息,前向收益仅作标签。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest.eval_v3 import stats as _st
from tools.backtest.eval_v3.prices import PriceBook
from tools.collectors import block_trade, index, lhb

logger = logging.getLogger("backtest.lhb_block")

HORIZONS = (1, 5)                 # 持有交易日数:T+1 开盘 → T+H 收盘
COST_RT = 0.002                   # 往返成本 0.2%
BENCH = "000300"                  # 市场基准:沪深300
OUT_DEFAULT = "data/analysis/backtest/lhb_block_lab.json"
_DISCLAIMER = ("盘后披露事件,入场=T+1开盘、退出=T+H收盘(防未来函数);沪深300按日去市场超额,"
               "按交易日聚类bootstrap;扣往返0.2%成本。历史回测≠未来保证,非投资建议。")


# ═════════════════════════ ① 事件表(披露日锚定,分季拉取更稳) ═════════════════════════
def _quarters(start: str, end: str) -> list[tuple[str, str]]:
    """把 [start,end](YYYYMMDD)切成按季度的 [(s,e)...],降低单次拉取体量/超时风险。"""
    s = pd.Timestamp(start)
    e = pd.Timestamp(end)
    out = []
    cur = s
    while cur <= e:
        q_end = min(cur + pd.offsets.QuarterEnd(0), e)
        out.append((cur.strftime("%Y%m%d"), q_end.strftime("%Y%m%d")))
        cur = q_end + pd.Timedelta(days=1)
    return out


def fetch_events(source: str, start: str, end: str) -> pd.DataFrame:
    """拉一个事件源在 [start,end] 的全市场事件,归一为回测用长表。

    返回列:code, ev_date(披露日=上榜日/交易日), direction, sig(连续信号), sig_name。
    lhb:  sig = net_buy_ratio(净买额占总成交比)。
    block:sig = premium_rate(折溢率);另带 inst_buy 便于机构买方分组。
    分季拉取后 concat;任一季失败降级跳过(不中断)。
    """
    frames = []
    for s, e in _quarters(start, end):
        if source == "lhb":
            df = lhb.fetch_range_df(s, e)
            if not df.empty:
                df = df.assign(ev_date=df["list_date"], sig=df["net_buy_ratio"],
                               sig_name="net_buy_ratio", inst_buy=0)
        elif source == "block":
            df = block_trade.fetch_range_df(s, e)
            if not df.empty:
                df = df.assign(ev_date=df["trade_date"], sig=df["premium_rate"],
                               sig_name="premium_rate")
        else:
            raise ValueError(f"未知事件源: {source}")
        if not df.empty:
            keep = ["code", "ev_date", "direction", "sig", "sig_name"]
            if "inst_buy" in df.columns:
                keep.append("inst_buy")
            frames.append(df[keep])
        logger.info("[%s] %s~%s: %d 条", source, s, e, 0 if df.empty else len(df))
    if not frames:
        return pd.DataFrame(columns=["code", "ev_date", "direction", "sig", "sig_name"])
    out = pd.concat(frames, ignore_index=True)
    # 去重:同票同披露日多条(多原因/多笔)→ 保留(它们是独立事件);仅去完全重复行
    return out.drop_duplicates().reset_index(drop=True)


# ═════════════════════════ ② 基准(T+1 开盘 → T+H 收盘,按交易日) ═════════════════════════
class _Bench:
    """沪深300 交易日序列:date→pos,open[]/close[]。给某披露日 D 的 T+H 基准收益。"""

    def __init__(self, start: str, end: str):
        # 多取 30 天尾巴,保证最后一批事件的 T+H 退出日也在窗内
        e_pad = (pd.Timestamp(end) + pd.Timedelta(days=40)).strftime("%Y%m%d")
        raw = index.fetch_index([BENCH], start=start, end=e_pad)
        df = raw[BENCH] if isinstance(raw, dict) else raw
        df = df.dropna(subset=["open", "close"]).reset_index(drop=True)
        self.dates = [str(x)[:10] for x in df["date"].tolist()]
        self.pos = {d: i for i, d in enumerate(self.dates)}
        self.op = df["open"].to_numpy(float)
        self.cl = df["close"].to_numpy(float)

    def ret(self, ev_date: str, h: int):
        """披露日 D 的基准 T+1 开盘 → T+H 收盘收益;不可算 → None。"""
        d = str(ev_date)[:10]
        p = self.pos.get(d)
        if p is None:                       # D 非交易日(容错):取首个 > D 的交易日前一位
            later = [i for i, x in enumerate(self.dates) if x > d]
            if not later:
                return None
            p = later[0] - 1
        entry = p + 1                       # T+1
        exit_ = p + h                       # T+H
        if entry >= len(self.cl) or exit_ >= len(self.cl):
            return None
        e_px, x_px = self.op[entry], self.cl[exit_]
        if not (e_px > 0):
            return None
        return float(x_px / e_px - 1.0)


# ═════════════════════════ ③ 前向收益(T+1 开盘入场,防未来函数) ═════════════════════════
def compute_returns(events: pd.DataFrame, pb: PriceBook, bench: _Bench,
                    horizons=HORIZONS) -> pd.DataFrame:
    """逐事件算个股 T+1 开盘→T+H 收盘收益 + 同窗沪深300基准,附去市场超额。

    输出每 (事件, 视界) 一行:code, ev_date, direction, sig, h, stk_ret, bench_ret, excess。
    入场越界/无价/退出越界 → 丢弃(不产生前视或残缺样本)。
    """
    # 预算各披露日各视界的基准收益(市场级,当天所有事件共享)
    ev_dates = sorted(events["ev_date"].dropna().unique())
    bench_ret = {(d, h): bench.ret(d, h) for d in ev_dates for h in horizons}

    rows = []
    for r in events.itertuples(index=False):
        rec = pb.get(str(r.code))
        if rec is None:
            continue
        op, _hi, _lo, cl, dmap = rec
        idx = dmap.get(str(r.ev_date)[:10])
        if idx is None:
            continue
        entry_j = idx + 1                    # T+1 开盘
        if entry_j >= len(cl):
            continue
        entry_px = float(op[entry_j])
        if not (entry_px > 0):
            entry_px = float(cl[entry_j])    # open 缺失兜底用 T+1 收盘(注明)
        if not (entry_px > 0):
            continue
        for h in horizons:
            exit_j = idx + h                 # T+H 收盘
            if exit_j >= len(cl):
                continue
            x_px = float(cl[exit_j])
            if not (x_px > 0):
                continue
            stk = x_px / entry_px - 1.0
            bch = bench_ret.get((str(r.ev_date)[:10], h))
            rows.append({
                "code": str(r.code), "ev_date": str(r.ev_date)[:10],
                "direction": int(r.direction),
                "inst_buy": int(getattr(r, "inst_buy", 0)),
                "sig": float(r.sig) if pd.notna(r.sig) else np.nan,
                "h": h, "stk_ret": stk, "bench_ret": bch,
                "excess": (stk - bch) if bch is not None else np.nan,
            })
    return pd.DataFrame(rows)


# ═════════════════════════ ④ 显著性汇总(按日聚类) ═════════════════════════
def _leg_stats(sub: pd.DataFrame, seed: int = 20260828) -> dict:
    """一腿(某分组、某视界)的按日聚类超额 + 净超额 + rank-IC。"""
    sub = sub.dropna(subset=["stk_ret", "bench_ret"])
    if sub.empty:
        return {"n": 0}
    by_day = list(sub.groupby("ev_date"))
    strat_day = [g["stk_ret"].to_numpy(float) for _, g in by_day]
    mkt_day = [float(g["bench_ret"].iloc[0]) for _, g in by_day]   # 当日基准(市场级标量)
    gross = _st.cluster_bootstrap_excess(strat_day, mkt_day, seed=seed)
    net = _st.cluster_bootstrap_excess([a - COST_RT for a in strat_day], mkt_day, seed=seed)
    # 连续信号 rank-IC(信号 vs 去市场超额),按日配对
    pairs = []
    for _, g in by_day:
        gg = g.dropna(subset=["sig", "excess"])
        if len(gg) >= 5:
            pairs.append((gg["sig"].to_numpy(float), gg["excess"].to_numpy(float)))
    ic = _st.rank_ic(pairs) if pairs else {"mean_ic": None, "p_value": None, "n_days": 0}
    return {
        "n": int(len(sub)), "n_days": int(sub["ev_date"].nunique()),
        "mean_stk_ret": round(float(sub["stk_ret"].mean()), 4),
        "mean_bench_ret": round(float(sub["bench_ret"].mean()), 4),
        "gross_excess": gross.get("excess"), "gross_p": gross.get("p_value"),
        "gross_ci": [gross.get("lo"), gross.get("hi")],
        "net_excess": net.get("excess"), "net_p": net.get("p_value"),
        "rank_ic": ic.get("mean_ic"), "rank_ic_p": ic.get("p_value"),
    }


def analyze_source(source: str, ret: pd.DataFrame) -> dict:
    """一个事件源的完整可回测性报告:总体 + 方向分组,各视界。"""
    res = {"source": source, "horizons": {}}
    for h in HORIZONS:
        rh = ret[ret["h"] == h]
        block = {"all": _leg_stats(rh)}
        block["dir=+1"] = _leg_stats(rh[rh["direction"] == 1])
        block["dir=-1"] = _leg_stats(rh[rh["direction"] == -1])
        if source == "block":
            block["inst_buy=1"] = _leg_stats(rh[rh["inst_buy"] == 1])
            block["premium(dir=+1)&inst_buy"] = _leg_stats(
                rh[(rh["direction"] == 1) & (rh["inst_buy"] == 1)])
        res["horizons"][f"H{h}"] = block
    return res


# ═════════════════════════ ⑤ 编排 ═════════════════════════
def run(start: str, end: str, sources: list[str], out: str | None = OUT_DEFAULT) -> dict:
    """拉事件 → 算 T+1 前向收益 → 按日聚类显著性 → 汇总落 JSON。"""
    pb = PriceBook()
    bench = _Bench(start, end)
    report = {"window": [start, end], "bench": BENCH, "cost_rt": COST_RT,
              "disclaimer": _DISCLAIMER, "sources": {}}
    for src in sources:
        logger.info("=== 事件源 %s ===", src)
        ev = fetch_events(src, start, end)
        logger.info("[%s] 事件总数 %d(去重后)", src, len(ev))
        if ev.empty:
            report["sources"][src] = {"error": "无事件数据"}
            continue
        ret = compute_returns(ev, pb, bench)
        logger.info("[%s] 可算前向收益样本 %d(事件×视界)", src, len(ret))
        report["sources"][src] = analyze_source(src, ret)
        report["sources"][src]["n_events"] = int(len(ev))
    if out:
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("报告已写 %s", out)
    return report


def main():
    ap = argparse.ArgumentParser(description="龙虎榜/大宗交易 可回测性验证(防未来函数)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="跑回测")
    r.add_argument("--start", default="20240101")
    r.add_argument("--end", default="20251231")
    r.add_argument("--sources", default="lhb,block")
    r.add_argument("--out", default=OUT_DEFAULT)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "run":
        rep = run(args.start, args.end, args.sources.split(","), args.out)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
