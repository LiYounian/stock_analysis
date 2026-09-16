"""H1 组合模拟器:baseline(无状态重挑)vs treatment(续选/继续持有)。

两臂唯一差异 = 到期(D+2)退出规则:
  · baseline  :持仓在入场次一交易日收盘无条件卖出(D 选→D+1 入场→D+2 无条件了结)。
  · treatment :到期时若「仍在最新决策日 TopN」且「收盘未跌破入场价(未破卖出线)」→ 续持,
                否则收盘卖出;续持者逐日复用同一判据(掉出 TopN 或跌破入场价即卖)。
其余(N 槽等权、限价回踩 marketable 成交、涨停不可买、10bps 成本、全A等权基准)完全一致。

口径复用 nextday_kernel(防未来:入场只用 D 收盘 + D+1 OHLC;基准用当日全A等权 open→close)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tools.research.selection_alpha import nextday_kernel as K
from tools.backtest import metrics as M

logger = logging.getLogger("backtest.reselection.portfolio")


@dataclass
class SimResult:
    trades: pd.DataFrame            # 逐笔:code/entry_date/exit_date/entry/exit/gross/net/hold/alpha_net/exec_date
    equity: pd.DataFrame           # date, ret(日组合净收益), equity(净值)
    turnover_daily: float          # 日均换手(当日新建仓数 / N)
    n_entries: int
    n_skipped_untriggered: int
    n_skipped_unbuyable: int
    arm: str
    params: dict = field(default_factory=dict)


def _ensure_didx(feats: dict[str, dict]) -> None:
    for f in feats.values():
        if "didx" not in f:
            f["didx"] = {d: i for i, d in enumerate(f["dates"])}


def simulate(feats: dict[str, dict], ranks: dict[str, list[str]], market: dict,
             *, arm: str, N: int = 10, topn: int = 10, entry_rule: str = "limit_pc_0.01",
             model: str = "marketable", cost_bps: float = 10.0,
             cont_mode: str = "topn_line",
             start: str | None = None, end: str | None = None) -> SimResult:
    """按全A执行日历逐日模拟。arm ∈ {baseline, treatment}。

    cont_mode(treatment 续持判据):
      · topn_line(默认·预注册):仍在 TopN 且收盘≥入场价(未破卖出线)→ 续持;
      · topn_only(稳健性对照):仅要求仍在 TopN(去掉盈亏平衡止损),测「卖出线」是否是主因。
    """
    _ensure_didx(feats)
    mkt_ir = market["mkt_ir"]
    cal = sorted(mkt_ir)                                # 全A执行日历
    if start:
        cal = [d for d in cal if d >= start]
    if end:
        cal = [d for d in cal if d <= end]
    cost = cost_bps / 1e4

    book: dict[str, dict] = {}                          # code -> position
    trades: list[dict] = []
    eq_dates, eq_rets = [], []
    entries_per_day: list[int] = []
    n_entries = n_untrig = n_unbuy = 0

    def _limit_price(f, di, close_D):
        if entry_rule == "open":
            return f["o"][di]
        k = float(entry_rule.split("_")[-1])
        return close_D * (1.0 - k)

    for gi in range(1, len(cal)):
        g = cal[gi]
        D = cal[gi - 1]                                 # 决策日(其 TopN 于 g 执行)
        top_list = ranks.get(D, [])
        top_set = set(top_list[:topn])
        # 最新决策日排名(用于 treatment 续持判定):就是 D(≤g 的最近排名)
        day_ret_num = 0.0                               # N 槽等权:空槽记 0

        # ── Step A/B:对现有持仓 mark + 到期退出 ──
        for code in list(book.keys()):
            p = book[code]
            f = feats[code]
            di = f["didx"].get(g)
            if di is None:                              # 当日停牌:无 bar,记 0(持仓不动)
                continue
            c_g = f["c"][di]
            if not np.isfinite(c_g) or c_g <= 0:
                continue
            prev = p["mark"]
            pos_ret = c_g / prev - 1.0                  # 该仓当日收益(prev=昨收或入场 fill)
            p["mark"] = c_g
            p["last_gi"] = gi
            p["held_days"] += 1

            due = gi > p["entry_gi"]                    # 到期检查点:入场次日起
            exit_now = False
            if due:
                if arm == "baseline":
                    exit_now = True
                elif arm == "treatment":
                    still_top = code in top_set
                    above_line = c_g >= p["entry"]     # 未破卖出线(收盘≥入场价)
                    if cont_mode == "topn_only":
                        exit_now = not still_top
                    else:
                        exit_now = not (still_top and above_line)
                else:
                    raise ValueError(arm)
            if exit_now:
                gross = c_g / p["entry"] - 1.0
                net = (1.0 + gross) * (1.0 - cost) - 1.0
                pos_ret = (1.0 + pos_ret) * (1.0 - cost) - 1.0   # 退出日扣成本
                # α:净收益 − 持有期全A等权累计(entry→exit 各执行日 open→close 复利近似用 mkt_ir 累加)
                bench = _bench_cum(mkt_ir, cal, p["entry_gi"], gi)
                trades.append(dict(
                    code=code, entry_date=cal[p["entry_gi"]], exit_date=g,
                    entry=p["entry"], exit=c_g, gross=gross, net=net,
                    hold_days=p["held_days"], alpha_net=net - bench,
                    exec_date=cal[p["entry_gi"]], arm=arm))
                del book[code]
            day_ret_num += pos_ret

        # ── Step C:新建仓(填满空槽,从 TopN(D) 取未持有者)──
        n_new = 0
        if top_list:
            for code in top_list[:topn]:
                if len(book) >= N:
                    break
                if code in book:
                    continue
                f = feats.get(code)
                if f is None:
                    continue
                dD = f["didx"].get(D)
                dg = f["didx"].get(g)
                if dD is None or dg is None or dg != dD + 1:
                    continue                            # 需 D 与 D+1 为该票连续相邻 bar
                close_D = f["c"][dD]
                o_g, h_g, l_g, c_g = f["o"][dg], f["h"][dg], f["lo"][dg], f["c"][dg]
                if not (o_g > 0) or not np.isfinite(c_g):
                    continue
                if bool(K.limit_up_unbuyable(code, np.array([close_D]), np.array([o_g]))[0]):
                    n_unbuy += 1
                    continue
                P = _limit_price(f, dg, close_D)
                fill, ret, filled = K.fill_and_return(
                    np.array([P]), np.array([o_g]), np.array([h_g]),
                    np.array([l_g]), np.array([c_g]), model)
                if not bool(filled[0]):
                    n_untrig += 1
                    continue
                fillp = float(fill[0])
                book[code] = dict(entry=fillp, mark=c_g, entry_gi=gi, last_gi=gi, held_days=0)
                # 入场日当日收益(fill→当日收盘)并入组合
                day_ret_num += (c_g / fillp - 1.0)
                n_entries += 1
                n_new += 1
        entries_per_day.append(n_new)
        eq_dates.append(g)
        eq_rets.append(day_ret_num / N)

    # ── 期末清算:仍持有的仓按最后 mark 收盘平仓,记入逐笔(净值已逐日 mark,不重复计入日收益)──
    for code, p in list(book.items()):
        c_g = p["mark"]
        gross = c_g / p["entry"] - 1.0
        net = (1.0 + gross) * (1.0 - cost) - 1.0
        bench = _bench_cum(mkt_ir, cal, p["entry_gi"], p["last_gi"])
        trades.append(dict(
            code=code, entry_date=cal[p["entry_gi"]], exit_date=cal[p["last_gi"]],
            entry=p["entry"], exit=c_g, gross=gross, net=net,
            hold_days=p["held_days"], alpha_net=net - bench,
            exec_date=cal[p["entry_gi"]], arm=arm))

    eq = pd.DataFrame({"date": eq_dates, "ret": eq_rets})
    eq["equity"] = (1.0 + eq["ret"]).cumprod()
    tdf = pd.DataFrame(trades)
    turnover = float(np.mean(entries_per_day) / N) if entries_per_day else 0.0
    return SimResult(trades=tdf, equity=eq, turnover_daily=turnover,
                     n_entries=n_entries, n_skipped_untriggered=n_untrig,
                     n_skipped_unbuyable=n_unbuy, arm=arm,
                     params=dict(N=N, topn=topn, entry_rule=entry_rule, model=model,
                                 cost_bps=cost_bps, start=start, end=end))


def _bench_cum(mkt_ir: dict, cal: list[str], entry_gi: int, exit_gi: int) -> float:
    """持有期全A等权累计:∏(1+mkt_ir[g]) − 1,g 取 (entry_gi, exit_gi] 各执行日(入场后到卖出日)。"""
    acc = 1.0
    for gi in range(entry_gi + 1, exit_gi + 1):
        r = mkt_ir.get(cal[gi])
        if r is not None and np.isfinite(r):
            acc *= (1.0 + r)
    return acc - 1.0


def summarize(res: SimResult, periods_per_year: int = 244) -> dict:
    eq = res.equity
    tdf = res.trades
    cum = float(eq["equity"].iloc[-1] - 1.0) if len(eq) else 0.0
    ann = M.annualized(eq["ret"], periods_per_year) if len(eq) else 0.0
    mdd = M.max_drawdown(eq["equity"]) if len(eq) else 0.0
    shp = M.sharpe(eq["ret"], periods_per_year=periods_per_year) if len(eq) else 0.0
    trade_net = tdf["net"].to_numpy(float) if len(tdf) else np.array([])
    return {
        "arm": res.arm,
        "n_trades": int(len(tdf)),
        "cum_net": round(cum, 4),
        "annualized_net": round(ann, 4),
        "max_drawdown": round(mdd, 4),
        "sharpe": round(shp, 3),
        "trade_win_rate": round(M.win_rate(list(trade_net)), 4) if len(trade_net) else None,
        "trade_mean_net": round(float(trade_net.mean()), 6) if len(trade_net) else None,
        "trade_mean_alpha_net": round(float(tdf["alpha_net"].mean()), 6) if len(tdf) else None,
        "avg_hold_days": round(float(tdf["hold_days"].mean()), 2) if len(tdf) else None,
        "turnover_daily": round(res.turnover_daily, 4),
        "n_untriggered": res.n_skipped_untriggered,
        "n_unbuyable": res.n_skipped_unbuyable,
    }


def cluster_t_diff(tr_a: pd.DataFrame, tr_b: pd.DataFrame) -> dict:
    """两臂逐笔 net 收益按 exec_date 聚类的均值差 t(H0: 差=0)。近似:各臂按日聚合贡献。"""
    def _agg(tr):
        v = tr["net"].to_numpy(float)
        c = tr["exec_date"].to_numpy()
        m = np.isfinite(v)
        return v[m], c[m]
    va, ca = _agg(tr_a)
    vb, cb = _agg(tr_b)
    if len(va) < 3 or len(vb) < 3:
        return {"n_a": len(va), "n_b": len(vb), "diff": None, "cluster_t": None}
    mua, mub = va.mean(), vb.mean()
    diff = float(mua - mub)
    ga, gb = {}, {}
    for u, k in zip(va - mua, ca):
        ga[k] = ga.get(k, 0.0) + u
    for u, k in zip(vb - mub, cb):
        gb[k] = gb.get(k, 0.0) + u
    var_a = sum(s * s for s in ga.values()) / (len(va) ** 2)
    var_b = sum(s * s for s in gb.values()) / (len(vb) ** 2)
    G = len(set(list(ga) + list(gb)))
    corr = G / (G - 1) if G > 1 else 1.0
    var = corr * (var_a + var_b)
    t = diff / np.sqrt(var) if var > 0 else None
    return {"n_a": int(len(va)), "n_b": int(len(vb)), "n_clusters": int(G),
            "diff": round(diff, 6), "cluster_t": round(float(t), 3) if t else None}
