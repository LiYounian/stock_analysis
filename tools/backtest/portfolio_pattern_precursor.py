"""形态选股·"动量+回踩×冷行业" 真实组合回测（严验线）。

草案的逐笔 +0.81%/单是真的、显著、OOS 稳；但草案里"年化+719%/Sharpe7.47"是
**忽略持仓重叠与资金约束的近似产物、失真**。本模块做**资金约束下的真实组合**：
  · 固定初始资金 + 最多 N 个并发持仓(每日限仓) + 每仓等权(equity/N)。
  · 信号超过空槽 → 按优先级(默认最冷行业优先)择取,其余放弃(容量约束)。
  · 逐日 MTM(持仓按当日收盘计值)→ 可信净值曲线 → 年化/Sharpe/最大回撤。
  · 行业冷热过滤消费 `industry_heat`(当前申万一级占位, regime 正式层就绪后 swap)。
  · 流动性过滤(成交额下限)+ 成本压测(往返成本档)。

复用逐笔引擎：match_entry(限价撮合)、simulate_exit(退出/涨停)。入场确定后退出
路径确定 → 每笔进场时即算好 exit_date/exit_px,持仓占槽至 exit_date。

无未来函数：信号≤D收盘;入场/退出/MTM 只用 D+1 及之后实际 K;行业冷热为因果分位。
⚠️ 测试环境研究模拟,非投资建议。不合 main。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tools.backtest import backtest_pattern_precursor as bt

logger = logging.getLogger("backtest.portfolio_precursor")


@dataclass
class PortfolioConfig:
    n_max: int = 20            # 最多并发持仓(每日限仓)
    cost_pct: float = 0.2      # 往返成本%
    dip_pct: float = 2.0
    tp_pct: float = 8.0
    sl_pct: float = 5.0
    time_stop: int = 5
    trigger: str = "mom7"      # 动量信号列
    gate: str = "gate_cold"    # 冷行业过滤列(gate_N=不过滤)
    min_amount: float = 0.0    # 流动性:入场日成交额下限(元),0=不过滤
    e0: float = 1_000_000.0


@dataclass
class _Pos:
    code: str
    cost_basis: float          # 进场投入现金(=shares×entry_px)
    shares: float
    net_pct: float             # 该笔已扣成本净收益%(进场即定,退出实现)
    exit_idx: int
    exit_date: pd.Timestamp


def _build_candidates(kl, frames, book, cfg: PortfolioConfig):
    """枚举所有候选进场：(entry_date, signal_date, code, entry_px, exit_idx, exit_px, net_pct, cold_rank)。
    cold_rank 越小=行业越冷(优先)。返回按 entry_date 分组的 dict。"""
    from collections import defaultdict
    cand = defaultdict(list)
    for code, df in kl.items():
        frame = frames[code]
        rec = book.get(code)
        if rec is None:
            continue
        n = len(df)
        dates = df["date"]
        col_t = frame[cfg.trigger].to_numpy()
        col_g = (np.ones(n, bool) if cfg.gate == "gate_N" else frame[cfg.gate].to_numpy())
        amt = df["amount"].to_numpy() if "amount" in df.columns else np.full(n, np.inf)
        heat = frame["ind_pct"].to_numpy() if "ind_pct" in frame.columns else np.full(n, 0.5)
        for t in range(60, n - cfg.time_stop - 1):
            if not (col_t[t] and col_g[t]):
                continue
            if cfg.min_amount and not (amt[t] >= cfg.min_amount):
                continue
            entry, mark = bt.match_entry(rec, t, cfg.dip_pct, code)
            if entry is None:
                continue
            ex = bt.simulate_exit(rec, t, entry, cfg.tp_pct, cfg.sl_pct, cfg.time_stop, cfg.cost_pct, code)
            if not ex["matured"]:
                continue
            entry_date = dates.iloc[t + 1]
            cand[entry_date].append(dict(signal_date=dates.iloc[t], code=code, entry_idx=t + 1,
                                         exit_idx=ex["exit_idx"], entry_px=entry,
                                         net_pct=ex["net_pct"], cold_rank=float(heat[t]),
                                         exit_date=dates.iloc[ex["exit_idx"]]))
    return cand


def run_portfolio(kl, frames, book, cfg: PortfolioConfig, calendar, date_from=None, date_to=None):
    """资金约束组合回测。calendar=升序交易日。返回 {equity(Series), stats, trades}。"""
    cand = _build_candidates(kl, frames, book, cfg)
    cal = [d for d in calendar if (not date_from or str(d.date()) >= date_from)
           and (not date_to or str(d.date()) <= date_to)]
    cash = cfg.e0
    open_pos: list[_Pos] = []
    equity_curve = []
    closes = {c: book.get(c)[3] for c in kl}          # code -> close array
    dmap = {c: book.get(c)[4] for c in kl}
    n_trades = 0
    net_realized = []
    for day in cal:
        dstr = str(day.date())
        # 1) 平仓:exit_date == 今天 的持仓
        still = []
        for p in open_pos:
            if p.exit_date <= day:
                cash += p.cost_basis * (1 + p.net_pct / 100.0)   # 按已扣成本净收益实现
                net_realized.append(p.net_pct)
                n_trades += 1
            else:
                still.append(p)
        open_pos = still
        # 2) 开仓:今日为 entry_date 的候选,填空槽
        slots = cfg.n_max - len(open_pos)
        if slots > 0 and day in cand:
            cs = sorted(cand[day], key=lambda x: x["cold_rank"])   # 最冷行业优先
            for c in cs[:slots]:
                alloc = (cash + _mtm(open_pos, closes, dmap, day)) / cfg.n_max  # 目标每仓=当前权益/N
                alloc = min(alloc, cash)                                          # 不超现金
                if alloc <= 0:
                    break
                shares = alloc / c["entry_px"]
                cash -= alloc
                open_pos.append(_Pos(c["code"], alloc, shares, c["net_pct"],
                                     c["exit_idx"], c["exit_date"]))
        # 3) MTM
        equity = cash + _mtm(open_pos, closes, dmap, day)
        equity_curve.append((day, equity))
    eq = pd.Series({d: v for d, v in equity_curve}).sort_index()
    return {"equity": eq, "stats": _stats(eq, net_realized, n_trades, len(cal)),
            "n_trades": n_trades, "net_realized": net_realized}


def _mtm(open_pos, closes, dmap, day):
    """持仓按当日收盘计值(找不到当日则用最近≤day)。"""
    dstr = str(day.date())
    tot = 0.0
    for p in open_pos:
        arr = closes[p.code]; dm = dmap[p.code]
        idx = dm.get(dstr)
        px = arr[idx] if idx is not None else p.entry_px
        tot += p.shares * px
    return tot


def _stats(eq: pd.Series, net_realized, n_trades, n_days):
    if len(eq) < 2:
        return {}
    ret = eq.pct_change().dropna()
    total = eq.iloc[-1] / eq.iloc[0] - 1
    years = len(eq) / 244.0
    ann = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
    sharpe = ret.mean() / ret.std() * np.sqrt(244) if ret.std() > 0 else np.nan
    peak = eq.cummax(); dd = (eq / peak - 1).min()
    nr = np.array(net_realized) if net_realized else np.array([0.0])
    return {"总收益%": round(total * 100, 1), "年化%": round(ann * 100, 1),
            "Sharpe": round(sharpe, 2), "最大回撤%": round(dd * 100, 1),
            "成交笔数": n_trades, "逐笔胜率": round(float((nr > 0).mean()), 3),
            "逐笔均净%": round(float(nr.mean()), 3), "逐笔中位%": round(float(np.median(nr)), 3)}
