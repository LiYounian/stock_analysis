"""形态选股·"金叉前兆·冲高兑现" walk-forward 回测引擎。

策略环（见 docs/计划/2026-09-15_形态选股策略_计划.md）：
  D 晚扫金叉票（触发件×趋势门，pattern_precursor_signals）→ D+1 在回踩限价
  P_entry(=昨收×(1-dip%)) **marketable 埋伏撮合** → 持有 ≤5 交易日 → 盘中触固定止盈
  P_target(=P_entry×(1+tp%)) 即卖 → 跌破止损 S_stop(=P_entry×(1-sl%)) 卖 → 第5日收盘了结。

次日实盘口径（复用 eval_v3）：
  · 取价 eval_v3.prices.PriceBook（前复权 OHLC）。
  · 退出循环 fork 自 eval_v3.exit_sim（改吃**绝对价** P_entry/P_target/S_stop）：
    盘中触线成交于线价、同日双触保守取止损、第5日收盘时间止损。
  · **限价埋伏撮合 + 涨停买/卖约束**为本模块自建（eval_v3 无）：
    买方 open≤P→成交于open(更优)、否则 low≤P→成交于P、否则弃单；涨停一字不可买弃单。
    卖方止盈日若涨停一字→当日卖不出，顺延下一可成交日。
  · α = 个股区间净收益 − **全A等权**同区间收益（统筹硬要求：纯动量/全A等权为主基准）。

无未来函数：信号只用 ≤D 收盘（compute_signal_frame 已锁）；入场/退出只用 D+1 及之后实际 K。
⚠️ 测试环境研究模拟，非投资建议。产物只写 worktree 本地，不写主检出、不合 main。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from tools.backtest.eval_v3 import prices as _pr
from tools.backtest import pattern_precursor_signals as sig
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("backtest.pattern_precursor")

_LIMIT_CFG = {"一字板": THRESHOLDS["趋势深跌反包"]["一字板"]}


def use_data_root(root: str) -> None:
    """把只读数据根指到主仓（worktree 无生产 data/）。patch repo 在 import 时定死的路径常量。
    对应工程约定"worktree 用 --data-root 指主仓只读"。只影响读取，不写主仓。"""
    from pathlib import Path
    from tools.store import repo as store
    r = Path(root)
    store._MASTER_DIR = r / "master"
    store._RAW_DIR = r / "raw"
    store._ANALYSIS_DIR = r / "analysis"
    logger.info("数据根指向 %s", r)

# 离场原因
R_TP, R_SL, R_SL_AMBIG, R_TIME = "止盈", "止跌", "止跌(同日双触)", "时间止损"
# 未成交原因
S_NO_RETRACE = "未回踩弃单"
S_LIMIT_UP = "涨停不可买弃单"


def _limit_frac(code: str | None) -> float:
    lc = _LIMIT_CFG["一字板"]
    if code:
        for pre in lc.get("科创创业前缀", []):
            if code.startswith(pre):
                return float(lc["科创创业幅度"])
    return float(lc["涨跌停幅度"])


def _oneword_up(hi: float, lo: float, close: float, prev_close: float, code: str | None) -> bool:
    """涨停一字（零振幅 + 涨停）→ 买方买不进 / 卖方卖不出。prev_close≤0 视为不可判→False。"""
    if prev_close is None or prev_close <= 0:
        return False
    if abs(hi - lo) > 1e-9:
        return False
    return close / prev_close - 1.0 >= _limit_frac(code) - 1e-9


def match_entry(rec, idx: int, dip_pct: float, code: str | None) -> tuple[float | None, str]:
    """D+1 限价埋伏撮合（marketable 买方口径）。

    P_entry = 昨收(close[idx]) × (1 - dip_pct/100)。次日 = idx+1。
      · idx+1 涨停一字 → 买不进，弃单(S_LIMIT_UP)。
      · open[idx+1] ≤ P_entry（跳空低开穿价）→ 成交于 open[idx+1]（更优）。
      · 否则 low[idx+1] ≤ P_entry（盘中回踩到限价）→ 成交于 P_entry。
      · 否则（全天未回踩）→ 弃单(S_NO_RETRACE)。
    返回 (成交价 或 None, 原因/成交标记)。
    """
    op, high, low, close, _ = rec
    j = idx + 1
    if j >= len(close) or not (close[idx] > 0):
        return None, S_NO_RETRACE
    prev_close = float(close[idx])
    p_entry = prev_close * (1.0 - dip_pct / 100.0)
    o, hi, lo = float(op[j]), float(high[j]), float(low[j])
    if _oneword_up(hi, lo, float(close[j]), prev_close, code):
        return None, S_LIMIT_UP
    if o > 0 and o <= p_entry:
        return o, "成交(跳空)"
    if lo <= p_entry:
        return p_entry, "成交(回踩)"
    return None, S_NO_RETRACE


def simulate_exit(rec, idx: int, entry: float, tp_pct: float, sl_pct: float,
                  time_stop: int, cost_pct: float, code: str | None) -> dict:
    """退出模拟（fork 自 eval_v3.exit_sim，改吃绝对入场价 + 卖方涨停一字顺延）。

    入场已在 idx+1 以 entry 价成交。扫 j=1..time_stop（k=idx+j，含入场日）：
      盘中 high≥tp_price→止盈成交于 tp_price；low≤sl_price→止损成交于 sl_price；
      同日双触→保守取止损；第 time_stop 日仍未触→收盘时间止损。
      卖方保护：命中止盈当日若涨停一字（卖不出）→ 跳过当日、顺延下一可成交日。
    返回 {matured, exit_reason, hold_days, exit_idx, entry, gross_pct, net_pct, path_ambiguous}。
    """
    out = {"matured": False, "exit_reason": None, "hold_days": None, "exit_idx": None,
           "entry": entry, "gross_pct": None, "net_pct": None, "path_ambiguous": False}
    op, high, low, close, _ = rec
    n = len(close)
    if idx + time_stop >= n or not (entry > 0):
        return out
    out["matured"] = True
    tp_price = entry * (1.0 + tp_pct / 100.0)
    sl_price = entry * (1.0 - sl_pct / 100.0)
    for j in range(1, time_stop + 1):
        k = idx + j
        hi, lo, cl = float(high[k]), float(low[k]), float(close[k])
        prev_cl = float(close[k - 1])
        tp_hit = hi >= tp_price
        sl_hit = lo <= sl_price
        exit_px = reason = None
        if tp_hit and sl_hit:
            exit_px, reason, out["path_ambiguous"] = sl_price, R_SL_AMBIG, True
        elif tp_hit:
            # 卖方涨停一字保护：封板卖不出 → 顺延（不在当日成交）
            if _oneword_up(hi, lo, cl, prev_cl, code) and j < time_stop:
                continue
            exit_px, reason = tp_price, R_TP
        elif sl_hit:
            exit_px, reason = sl_price, R_SL
        elif j == time_stop:
            exit_px, reason = cl, R_TIME
        if exit_px is not None:
            gross = (exit_px / entry - 1.0) * 100.0
            out.update(exit_reason=reason, hold_days=j, exit_idx=k,
                       gross_pct=round(gross, 4), net_pct=round(gross - cost_pct, 4))
            return out
    exit_px = float(close[idx + time_stop])
    gross = (exit_px / entry - 1.0) * 100.0
    out.update(exit_reason=R_TIME, hold_days=time_stop, exit_idx=idx + time_stop,
               gross_pct=round(gross, 4), net_pct=round(gross - cost_pct, 4))
    return out


# ---------- 数据装配 ----------
def load_universe(cap: int | None = None, min_bars: int = 260, seed: int = 7):
    """加载全A(排北交所)全历史K线;cap 限票数(MVP 抽样,均匀), min_bars 丢历史不足。
    返回 {code: df(升序, date为Timestamp)}。"""
    from tools.store import repo as store
    from tools.collectors import market
    codes = sorted(c for c in store.list_master_codes() if c[:1] not in ("8", "4"))
    if cap and len(codes) > cap:
        rng = np.random.default_rng(seed)
        codes = sorted(rng.choice(codes, size=cap, replace=False).tolist())
    out = {}
    for c in codes:
        try:
            df = market.load_kline(c)
        except FileNotFoundError:
            continue
        if df is None or len(df) < min_bars:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        out[c] = df
    logger.info("加载 %d 只(min_bars=%d, cap=%s)", len(out), min_bars, cap)
    return out


def build_ew_index(klines: dict[str, pd.DataFrame]) -> pd.Series:
    """全A等权收益指数(level)：每日横截面 mean(pct_chg)，累乘成 level。index=Timestamp。"""
    frames = []
    for df in klines.values():
        s = df.set_index("date")["pct_chg"] / 100.0
        frames.append(s)
    mat = pd.concat(frames, axis=1)
    daily_mean = mat.mean(axis=1).sort_index()
    level = (1.0 + daily_mean).cumprod()
    return level


@dataclass
class Config:
    trigger: str = "trig_kdj"
    gate: str = "gate_N"
    dip_pct: float = 0.0
    tp_pct: float = 8.0
    sl_pct: float = 5.0
    time_stop: int = 5
    cost_pct: float = 0.2

    def label(self) -> str:
        return f"{self.trigger}|{self.gate}|dip{self.dip_pct}|tp{self.tp_pct}|sl{self.sl_pct}|ts{self.time_stop}"


def run_config(klines, frames, book, ew_level: pd.Series, cfg: Config,
               date_from: str | None = None, date_to: str | None = None) -> dict:
    """跑单配置：扫全票全日入选→撮合→退出→收集。返回 {trades, n_trigger, n_filled, records}。
    date_from/date_to 限选股日区间(YYYY-MM-DD)，用于 OOS 切分。"""
    ew = ew_level
    records = []
    n_trigger = n_filled = 0
    for code, df in klines.items():
        frame = frames[code]
        rec = book.get(code)
        if rec is None:
            continue
        n = len(df)
        dates = df["date"]
        col_t = frame[cfg.trigger].to_numpy()
        col_g = (np.ones(n, bool) if cfg.gate == "gate_N" else frame[cfg.gate].to_numpy())
        for t in range(60, n - cfg.time_stop - 1):
            if not (col_t[t] and col_g[t]):
                continue
            d = dates.iloc[t]
            if date_from and str(d.date()) < date_from:
                continue
            if date_to and str(d.date()) > date_to:
                continue
            n_trigger += 1
            entry, mark = match_entry(rec, t, cfg.dip_pct, code)
            if entry is None:
                continue
            ex = simulate_exit(rec, t, entry, cfg.tp_pct, cfg.sl_pct, cfg.time_stop, cfg.cost_pct, code)
            if not ex["matured"]:
                continue
            n_filled += 1
            # α vs 全A等权：入场日→退出日 区间收益
            entry_date = dates.iloc[t + 1]
            exit_date = dates.iloc[ex["exit_idx"]]
            ew_ret = _ew_period(ew, entry_date, exit_date)
            alpha = ex["net_pct"] - (ew_ret * 100.0 if ew_ret is not None else 0.0)
            records.append({
                "code": code, "signal_date": str(d.date()), "entry_date": str(entry_date.date()),
                "exit_date": str(exit_date.date()), "reason": ex["exit_reason"],
                "hold_days": ex["hold_days"], "net_pct": ex["net_pct"], "alpha": round(alpha, 4),
                "win": ex["net_pct"] > 0,
            })
    return {"records": records, "n_trigger": n_trigger, "n_filled": n_filled}


def _ew_period(ew: pd.Series, d0, d1) -> float | None:
    """全A等权 level 从 d0 到 d1 的区间收益（含 d0 入场日当日？用入场日前一日 level 到退出日 level）。
    入场在 d0 的价内成交，基准用 d0→d1 的 level 比更贴近同期。"""
    try:
        i0 = ew.index.get_indexer([d0], method="nearest")[0]
        i1 = ew.index.get_indexer([d1], method="nearest")[0]
        if i0 <= 0 or i1 < i0:
            return None
        return ew.iloc[i1] / ew.iloc[i0 - 1] - 1.0
    except Exception:  # noqa: BLE001
        return None


def summarize(res: dict) -> dict:
    recs = res["records"]
    n = len(recs)
    if n == 0:
        return {"N": 0, "触发": res["n_trigger"], "成交": res["n_filled"], "弃单率": None,
                "胜率": None, "均值净%": None, "均值α%": None}
    net = np.array([r["net_pct"] for r in recs])
    al = np.array([r["alpha"] for r in recs])
    win = np.mean([r["win"] for r in recs])
    fill_rate = res["n_filled"] / res["n_trigger"] if res["n_trigger"] else None
    return {"N": n, "触发": res["n_trigger"], "成交": res["n_filled"],
            "弃单率": round(1 - fill_rate, 3) if fill_rate is not None else None,
            "胜率": round(win, 3), "均值净%": round(float(net.mean()), 3),
            "均值α%": round(float(al.mean()), 3), "中位净%": round(float(np.median(net)), 3)}
