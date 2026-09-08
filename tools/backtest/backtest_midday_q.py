"""午盘 Q · M3.a 日线粗回测(降级版)。

**注意:这是降级回测,不是真回测**。
拿不到历史分时数据(东财墙/mootdx 不通)→ 用当日 open/high/low/close 假装 14:30/14:50 分时。
够回答"策略方向对不对",不够回答"精确胜率/最优阈值"。

降级点(诚实标注):
    · Q1 IntradayReturn = (close/open - 1)代替 14:30 现价/open
      → 会高估:实盘 14:30 时 close 未知,策略入选后 14:30-15:00 可能崩
    · Q1 DistanceToDayHigh = (1 - close/high)代替 14:30 距日高
      → 会低估:实盘 14:30 距日高比日终小
    · Q2 IntradayLow / Rebound 用 (low/open - 1) 和 (close/low - 1)
      → 与 Q1 同,收盘价代理
    · AmPmRatio 无法算(需 10:30 vs 14:30 快照差) → 用 volume 相对前 20 日均量 作近似
    · Q3 分时资金流跳过 → 只用价量条件,失去 Q3 的核心信号
    · 板块日内涨幅缺 → SectorRank 恒 False
    · 大盘闸门 G3/G4/G5 用焦点池内广度(126 只)代理全 A

买卖口径(降级):
    · 买入:T 日收盘价(代替 14:55-15:00 分时 VWAP)—— 会给策略"看到收盘再决定"的先知优势
    · 卖出:T+1 日开盘价(代替 9:35-10:00 分时 VWAP)—— 与方案接近,可接受
    · 成本:15bp(买 5bp + 卖 10bp,含印花税)

产出:
    · docs/每日分析_午盘Q/M3a_粗回测报告_YYYYMMDD.md

⚠️ 非投资建议;历史回测≠未来保证。
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from tools.config import midday_q_universe as UNIV
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("backtest.midday_q")

TOTAL_COST_BPS = 15.0     # 买入 5bp + 卖出 10bp(印花税+手续费+滑点)
TOP_N_PER_STRATEGY = 5

# ────────────────────────────── 数据加载 ──────────────────────────────

def load_focus_klines() -> dict[str, pd.DataFrame]:
    """加载焦点池所有票的日 K 线。缺失票跳过并记 warning。"""
    codes = UNIV.get_focus_codes()
    out: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for c in codes:
        try:
            df = store.get_master_kline(c)
            if df is None or df.empty:
                missing.append(c)
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").reset_index(drop=True)
            out[c] = df
        except FileNotFoundError:
            missing.append(c)
    if missing:
        logger.warning("焦点池 %d/%d 只 K 线缺失,回测将跳过", len(missing), len(codes))
    return out


def trading_dates(klines: dict[str, pd.DataFrame]) -> list[str]:
    """所有 K 线的日期并集,升序。"""
    dates: set[str] = set()
    for df in klines.values():
        dates.update(df["date"].tolist())
    return sorted(dates)


# ────────────────────────────── 大盘闸门(简化:焦点池内广度) ──────────────────────────────

def compute_gate_daily(klines: dict[str, pd.DataFrame], date: str) -> dict:
    """按当日收盘计算简化大盘闸门。

    信号(用当日日线代理):
      G1/G2  焦点池等权涨跌幅(代替沪深300/中证1000)
      G3     up_count / max(down_count, 1)
      G4     limit_up_n / max(limit_down_n, 1)
      G5     net_breadth = (up - down) / total
    """
    ups = downs = flats = limit_up = limit_down = 0
    pcts: list[float] = []
    for c, df in klines.items():
        row = df[df["date"] == date]
        if row.empty: continue
        pct = float(row["pct_chg"].iloc[0])
        pcts.append(pct)
        if pct > 0.1: ups += 1
        elif pct < -0.1: downs += 1
        else: flats += 1
        # 涨跌停(简化:创板/科创板 20%,主板 10%)
        lim = 20 if (c.startswith("30") or c.startswith("68")) else 10
        if pct >= lim - 0.5: limit_up += 1
        elif pct <= -(lim - 0.5): limit_down += 1

    total = ups + downs + flats
    if total == 0:
        return {"state": "未知", "allowed_strategies": [], "position_pct": 0.0}

    mean_pct = sum(pcts) / total
    g1 = g2 = mean_pct / 100.0        # 归一到小数
    g3 = ups / max(downs, 1)
    g4 = limit_up / max(limit_down, 1)
    g5 = (ups - downs) / total

    # 阈值同 gate.THRESHOLDS
    strong_hits = sum([g1 >= 0.005, g2 >= 0.005, g3 >= 1.5, g4 >= 5.0, g5 >= 0.2])
    weak_hits   = sum([g1 <= -0.005, g2 <= -0.005, g3 <= 0.67, g4 <= 1.0, g5 <= -0.2])

    if g4 <= 0.5 and g5 <= -0.5:
        state = "崩盘"
    elif strong_hits >= 4:
        state = "强势"
    elif weak_hits >= 4:
        state = "弱势"
    else:
        state = "震荡"

    # v2 优化(2026-09-08 250 日回测验证):
    # · 强势日 Q1/Q3 均亏(追涨熊反弹=接盘) → 强势日只允许 Q3(且加严 Q3 阈值)
    # · 震荡日 Q1/Q3 有微弱 alpha → 保留
    # · 弱势日 Q2 反抽有胜率但均值负 → 保留观察
    # · 崩盘日全停(不变)
    # v3 优化(2026-09-08 双卖出口径回测):
    # · Q2 在早盘卖(AM)口径下所有阈值组合都亏,但在尾盘卖(PM)口径下 v1 阈值 226 笔胜率 54% 累计+200%
    #   → Q2 保留(不再全停),看 PM 表现
    # · 强势日 Q1 无论 AM/PM 都亏(追涨型接盘) → 强势日只允许 Q3
    STATE_ALLOW = {
        "强势": (["Q3"], 1.0),
        "弱势": (["Q2", "Q3"], 0.7),
        "震荡": (["Q1", "Q2", "Q3"], 0.5),
        "崩盘": ([], 0.0),
        "未知": ([], 0.0),
    }
    allowed, pos = STATE_ALLOW[state]
    return {
        "state": state, "allowed_strategies": list(allowed), "position_pct": pos,
        "indicators": {"g1": g1, "g3": g3, "g4": g4, "g5": g5,
                       "up_count": ups, "down_count": downs,
                       "limit_up_n": limit_up, "limit_down_n": limit_down},
    }


# ────────────────────────────── 三策略(降级版) ──────────────────────────────

def _volume_expand(df: pd.DataFrame, idx: int, window: int = 20) -> float | None:
    """当日 volume 相对前 window 日均量的比值(近似 AmPmRatio)。样本不足 → None。"""
    if idx < window: return None
    today_vol = float(df["volume"].iloc[idx])
    hist_mean = float(df["volume"].iloc[idx-window:idx].mean())
    if hist_mean == 0: return None
    return today_vol / hist_mean


def _ma(df: pd.DataFrame, idx: int, window: int) -> float | None:
    """T-1 及以前 window 日 close 均线。样本不足 → None。"""
    if idx < window: return None
    return float(df["close"].iloc[idx-window:idx].mean())


def _limit_up_price(pct_yesterday_close: float, code: str) -> float:
    """今日涨停价 = 昨收 × (1 + 涨幅上限)。简化:创板 20%,主板 10%(ST 忽略)。"""
    pct = 0.20 if (code.startswith("30") or code.startswith("68")) else 0.10
    return pct_yesterday_close * (1 + pct)


def q1_hits(klines: dict[str, pd.DataFrame], date: str) -> list[dict]:
    """Q1 强势接力:降级信号。"""
    hits = []
    for c, df in klines.items():
        # 找到 date 在 df 的位置
        loc = df.index[df["date"] == date].tolist()
        if not loc: continue
        i = loc[0]
        if i < 20: continue      # 需要 20 日均线

        opn = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])
        high = float(df["high"].iloc[i])
        prev_close = float(df["close"].iloc[i-1])
        if opn <= 0: continue

        intraday_return = close / opn - 1
        # Q1 条件: 温和强势 3-6%
        if not (0.03 <= intraday_return <= 0.06):
            continue
        # 距日高 <1.5%
        distance = 1 - close / high if high > 0 else 999
        if distance > 0.015:
            continue
        # 成交量放大
        vol_exp = _volume_expand(df, i)
        if vol_exp is None or vol_exp < 0.8:
            continue
        # MA5>MA10>MA20
        ma5 = _ma(df, i, 5)
        ma10 = _ma(df, i, 10)
        ma20 = _ma(df, i, 20)
        if not (ma5 and ma10 and ma20 and ma5 > ma10 > ma20 and opn > ma5):
            continue
        # 排除接近涨停(降级:用 close 判)
        lu = _limit_up_price(prev_close, c)
        if close >= lu * 0.985:
            continue
        # 流动性 (成交额 ≥ 5000 万,单位:元)
        if float(df["amount"].iloc[i]) < 5000 * 10000:
            continue

        rank_score = 0.4 * intraday_return + 0.3 * (vol_exp - 0.8) + 0.2 * (1 - distance)
        hits.append({"code": c, "rank": rank_score,
                     "signals": {"ir": round(intraday_return, 4),
                                  "vol_exp": round(vol_exp, 2),
                                  "dh": round(distance, 4)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


def q2_hits(klines: dict[str, pd.DataFrame], date: str) -> list[dict]:
    """Q2 弱转强反抽:降级信号。"""
    hits = []
    for c, df in klines.items():
        loc = df.index[df["date"] == date].tolist()
        if not loc: continue
        i = loc[0]
        if i < 60: continue      # 需要 MA60

        opn = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])
        low = float(df["low"].iloc[i])
        prev_close = float(df["close"].iloc[i-1])
        if opn <= 0 or low <= 0: continue

        intraday_low = low / opn - 1
        rebound = close / low - 1
        current_return = close / opn - 1

        # Q2 v1 严格阈值(250 日样本扫描后:所有 Q2 阈值组合都亏,方向错)
        # 保留代码但由 STATE_ALLOW 决定实际是否触发(未来样本外市况可能有效)
        if intraday_low > -0.03:            # 日内跌 ≥3%
            continue
        if rebound < 0.02:                    # 反弹 ≥2%
            continue
        if not (-0.01 <= current_return <= 0.02):
            continue
        vol_exp = _volume_expand(df, i)
        if vol_exp is None or vol_exp < 1.2:
            continue
        ma60 = _ma(df, i, 60)
        if not (ma60 and opn > ma60 * 0.9):    # 非长期熊
            continue
        # 排除跌停附近
        ld = _limit_up_price(prev_close, c) * 0    # 简化不算,直接看跌幅
        if current_return < -0.09:                   # 接近跌停
            continue

        rank_score = 0.4 * rebound + 0.3 * (vol_exp - 1.2) + 0.2 * abs(intraday_low) - 0.1 * current_return
        hits.append({"code": c, "rank": rank_score,
                     "signals": {"il": round(intraday_low, 4),
                                  "rebound": round(rebound, 4),
                                  "cr": round(current_return, 4),
                                  "vol_exp": round(vol_exp, 2)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


def q3_hits(klines: dict[str, pd.DataFrame], date: str) -> list[dict]:
    """Q3 资金流前瞻:降级(无分时资金流)版。

    降级:用"当日大单占比代理"缺失,只用 (放量 + 涨幅温和 + 流动性) 三条。
    这几乎丢掉了 Q3 的核心 alpha,回测结果仅供方向参考。
    """
    hits = []
    for c, df in klines.items():
        loc = df.index[df["date"] == date].tolist()
        if not loc: continue
        i = loc[0]
        if i < 20: continue

        opn = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])
        prev_close = float(df["close"].iloc[i-1])
        amount = float(df["amount"].iloc[i])
        if opn <= 0: continue

        current_return = close / opn - 1
        # v2 优化(250 日样本网格扫描,2026-09-08):
        # "超放量+" 配置:vol≥5x + amt≥3亿 + [-2,3]% → 71 笔 · 胜率 15.5% · 均值+0.24% · 累计+17.4%
        # 高赔率低胜率:15% 的票暴涨兜住 85% 小亏,与"主力大额买入"思路一致
        if not (-0.02 <= current_return <= 0.03):
            continue
        vol_exp = _volume_expand(df, i)
        if vol_exp is None or vol_exp < 5.0:      # 放量 ≥5x(超放量)
            continue
        if amount < 30000 * 10000:                   # 流动性 ≥3 亿(只挑大票)
            continue
        # 排除接近涨停
        lu = _limit_up_price(prev_close, c)
        if close >= lu * 0.985:
            continue

        rank_score = 0.4 * vol_exp + 0.3 * (amount / 1e9)
        hits.append({"code": c, "rank": rank_score,
                     "signals": {"cr": round(current_return, 4),
                                  "vol_exp": round(vol_exp, 2),
                                  "amount_yi": round(amount / 1e8, 2)}})
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return hits[:TOP_N_PER_STRATEGY]


STRATEGIES = {"Q1": q1_hits, "Q2": q2_hits, "Q3": q3_hits}


# ────────────────────────────── 收益计算(双卖出口径) ──────────────────────────────
# EXIT MODE:
#   "morning"  次日 9:35-10:00 VWAP 卖 → 用 T+1 日 open 代理(方案原始设计)
#   "afternoon" 次日尾盘 14:30-14:50 卖 → 日线无 14:30 精确价,用 T+1 日 close 代理
#              高估:实盘 14:30 价通常比 close 略低(尾盘 15:00 涨多为主),这个偏差应该<0.3%
# 两口径都以 T 日 close 作买入价(方案设计的 14:55 尾盘 VWAP 近似)

def _return_with_exit(klines, code: str, date: str, exit_mode: str) -> float | None:
    df = klines[code]
    loc = df.index[df["date"] == date].tolist()
    if not loc: return None
    i = loc[0]
    if i + 1 >= len(df): return None
    buy = float(df["close"].iloc[i])
    if buy <= 0: return None
    if exit_mode == "morning":
        sell = float(df["open"].iloc[i+1])
    elif exit_mode == "afternoon":
        sell = float(df["close"].iloc[i+1])
    else:
        raise ValueError(f"未知 exit_mode: {exit_mode!r}")
    return sell / buy - 1 - TOTAL_COST_BPS / 10000


def next_day_return(klines, code: str, date: str,
                     exit_mode: str = "morning") -> float | None:
    """默认为原始的 morning(早盘卖);向后兼容原接口。"""
    return _return_with_exit(klines, code, date, exit_mode)


# ────────────────────────────── 回测主循环 ──────────────────────────────

def backtest(klines: dict[str, pd.DataFrame],
              start_date: str | None = None,
              end_date: str | None = None) -> dict:
    """回放:逐日跑闸门+策略,记录次日收益。"""
    all_dates = trading_dates(klines)
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
    if end_date:
        all_dates = [d for d in all_dates if d <= end_date]

    # 至少 T+1 才能算收益 → 去掉最后一天
    trade_dates = all_dates[:-1] if len(all_dates) > 1 else []

    # 每策略的记录
    log = {s: [] for s in STRATEGIES}
    gate_log = []

    for date in trade_dates:
        gate = compute_gate_daily(klines, date)
        gate_log.append({"date": date, **{k: gate.get(k) for k in ("state", "position_pct")}})
        allowed = gate.get("allowed_strategies") or []

        for strat, fn in STRATEGIES.items():
            if strat not in allowed:
                continue
            hits = fn(klines, date)
            for h in hits:
                ret_am = _return_with_exit(klines, h["code"], date, "morning")
                ret_pm = _return_with_exit(klines, h["code"], date, "afternoon")
                if ret_am is None or ret_pm is None: continue
                log[strat].append({"date": date, "code": h["code"],
                                    "rank": h["rank"],
                                    "ret_am": ret_am, "ret_pm": ret_pm,
                                    "gate_state": gate["state"]})

    return {"trades": log, "gate_log": gate_log,
            "n_dates": len(trade_dates),
            "dates": {"start": trade_dates[0] if trade_dates else None,
                       "end": trade_dates[-1] if trade_dates else None}}


# ────────────────────────────── 指标 ──────────────────────────────

def _metrics_from_rets(rets: list[float]) -> dict:
    """rets → 指标 dict(n/win_rate/mean/median/max_dd/total/sharpe)。"""
    if not rets:
        return {"n": 0, "win_rate": None, "mean_ret": None, "median_ret": None,
                "max_dd": None, "total_ret": None, "sharpe": None}
    n = len(rets); wins = sum(1 for r in rets if r > 0); mean = sum(rets) / n
    sorted_r = sorted(rets)
    median = sorted_r[n // 2] if n % 2 else (sorted_r[n//2-1] + sorted_r[n//2]) / 2
    cumsum = 0.0; peak = 0.0; max_dd = 0.0
    for r in rets:
        cumsum += r; peak = max(peak, cumsum)
        max_dd = min(max_dd, cumsum - peak)
    import statistics
    std = statistics.pstdev(rets) if n > 1 else 0.0
    sharpe = (mean / std * (244 ** 0.5)) if std > 0 else None
    return {"n": n, "win_rate": wins / n, "mean_ret": mean, "median_ret": median,
            "max_dd": max_dd, "total_ret": cumsum, "sharpe": sharpe}


def compute_metrics(trades: list[dict]) -> dict:
    """双卖出口径指标 {am: {...}, pm: {...}}。

    向后兼容:trades 里若只有 'ret'(旧接口),视为 am 值,pm 缺失。
    """
    if not trades:
        return {"am": _metrics_from_rets([]), "pm": _metrics_from_rets([])}
    if "ret_am" in trades[0]:
        return {"am": _metrics_from_rets([t["ret_am"] for t in trades]),
                "pm": _metrics_from_rets([t["ret_pm"] for t in trades])}
    # 兼容旧字段
    return {"am": _metrics_from_rets([t["ret"] for t in trades]),
            "pm": _metrics_from_rets([])}


def summarize(result: dict) -> dict:
    """按策略 + 大盘状态 双维汇总(每维给 am/pm 两套指标)。"""
    out: dict = {"n_dates": result["n_dates"], "dates": result["dates"], "by_strategy": {}}
    for strat, trades in result["trades"].items():
        out["by_strategy"][strat] = {
            "overall": compute_metrics(trades),
            "by_gate_state": {
                s: compute_metrics([t for t in trades if t["gate_state"] == s])
                for s in ("强势", "震荡", "弱势", "崩盘")
            },
        }
    from collections import Counter
    out["gate_state_dist"] = dict(Counter(g["state"] for g in result["gate_log"]))
    return out


# ────────────────────────────── 报告 ──────────────────────────────

def _fmt_pct(x, dp=2):
    if x is None: return "N/A"
    return f"{x*100:.{dp}f}%"


def _fmt(x, dp=2):
    if x is None: return "N/A"
    return f"{x:.{dp}f}"


def render_markdown(summary: dict) -> str:
    lines = []
    lines.append("# 午盘 Q 策略 · M3.a 日线粗回测报告")
    lines.append("")
    lines.append(f"- **回测区间**: {summary['dates']['start']} → {summary['dates']['end']}")
    lines.append(f"- **交易日数**: {summary['n_dates']}")
    lines.append(f"- **票池**: 焦点池 126 只(半导体+CPO)")
    lines.append(f"- **成本**: 双边 15 bp(印花税+手续费+滑点)")
    lines.append("")
    lines.append("## 🚨 降级说明(结果解读前必读)")
    lines.append("")
    lines.append("1. **日线代替分时** — 用当日 open/close 假装 14:30/14:50,不是真实盘中信号")
    lines.append("2. **买入 = T 日 close** — 策略「看到收盘」再决定,有**先知优势** → 实盘不会有这么好")
    lines.append("3. **双卖出口径同时评估**:")
    lines.append("   - **AM 早盘**:卖 = T+1 日 open(方案原始设计,对应 9:35-10:00 VWAP 卖)")
    lines.append("   - **PM 尾盘**:卖 = T+1 日 close(近似 14:30-14:50 卖,日线无 14:30 精确价 → 用 close 代理)")
    lines.append("   - PM 相对 AM 多持有 4-6 小时,包含次日日内涨幅;A 股结构性次日低开 → **AM 通常吃亏**")
    lines.append("4. **Q3 无分时资金流** — 只剩放量+涨温和+流动性三条,丢掉核心 alpha")
    lines.append("5. **大盘广度用焦点池 126 只代理全 A** — 半导体板块自身强弱,而非大盘")
    lines.append("")
    lines.append("**这份报告只能回答「方向对不对」,不能回答「精确胜率/最优阈值」。**")
    lines.append("")
    lines.append("## 大盘状态分布")
    lines.append("")
    lines.append("| 状态 | 天数 |")
    lines.append("|---|---|")
    for s, n in summary["gate_state_dist"].items():
        lines.append(f"| {s} | {n} |")
    lines.append("")
    lines.append("## 三策略总体指标(AM = 早盘卖 · PM = 尾盘卖)")
    lines.append("")
    lines.append("| 策略 | 笔数 | AM 胜率 | AM 均值 | AM 累计 | AM 回撤 | PM 胜率 | PM 均值 | PM 累计 | PM 回撤 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for strat in ("Q1", "Q2", "Q3"):
        both = summary["by_strategy"][strat]["overall"]
        am, pm = both["am"], both["pm"]
        lines.append(f"| {strat} | {am['n']} | "
                     f"{_fmt_pct(am['win_rate'])} | {_fmt_pct(am['mean_ret'])} | "
                     f"{_fmt_pct(am['total_ret'])} | {_fmt_pct(am['max_dd'])} | "
                     f"{_fmt_pct(pm['win_rate'])} | {_fmt_pct(pm['mean_ret'])} | "
                     f"{_fmt_pct(pm['total_ret'])} | {_fmt_pct(pm['max_dd'])} |")
    lines.append("")
    lines.append("## 分大盘状态指标")
    lines.append("")
    for strat in ("Q1", "Q2", "Q3"):
        lines.append(f"### {strat}")
        lines.append("")
        lines.append("| 大盘状态 | 笔数 | AM 胜率 | AM 均值 | AM 累计 | PM 胜率 | PM 均值 | PM 累计 |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for state in ("强势", "震荡", "弱势", "崩盘"):
            both = summary["by_strategy"][strat]["by_gate_state"][state]
            am, pm = both["am"], both["pm"]
            if am["n"] == 0: continue
            lines.append(f"| {state} | {am['n']} | "
                         f"{_fmt_pct(am['win_rate'])} | {_fmt_pct(am['mean_ret'])} | {_fmt_pct(am['total_ret'])} | "
                         f"{_fmt_pct(pm['win_rate'])} | {_fmt_pct(pm['mean_ret'])} | {_fmt_pct(pm['total_ret'])} |")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("**⚠️ 非投资建议**。历史回测≠未来保证。降级点见开头说明。")
    lines.append(f"生成时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


# ────────────────────────────── CLI ──────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="午盘 Q · M3.a 日线粗回测")
    ap.add_argument("--start", default=None, help="回测起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="回测终止日期 YYYY-MM-DD")
    ap.add_argument("--json", default=None, help="产出 JSON 明细路径(可选)")
    ap.add_argument("--md", default=None, help="产出 Markdown 报告路径(默认 docs 下)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger.info("加载焦点池 K 线...")
    klines = load_focus_klines()
    logger.info("加载 %d 只 K 线", len(klines))

    logger.info("跑回测 %s → %s ...", args.start or "最早", args.end or "最新")
    result = backtest(klines, start_date=args.start, end_date=args.end)
    summary = summarize(result)

    # JSON 明细
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"summary": summary, "trades": result["trades"]},
                      f, ensure_ascii=False, indent=2, default=str)
        logger.info("落 JSON: %s", args.json)

    # Markdown 报告
    md_path = Path(args.md) if args.md else (
        settings.PROJECT_ROOT / "docs" / "每日分析_午盘Q" /
        f"M3a_粗回测报告_{datetime.now().strftime('%Y%m%d')}.md"
    )
    md_path.write_text(render_markdown(summary), encoding="utf-8")
    logger.info("落报告: %s", md_path)

    # 命令行摘要(AM 早盘卖 vs PM 尾盘卖)
    print(f"\n{'='*90}")
    print(f"回测区间: {summary['dates']['start']} → {summary['dates']['end']} · 交易日: {summary['n_dates']}")
    print(f"大盘状态分布: {summary['gate_state_dist']}")
    print(f"{'='*90}")
    print(f"{'策略':<4} {'笔数':>5} | {'AM 胜率':>8} {'AM 均值':>9} {'AM 累计':>9} {'AM 回撤':>9} | "
          f"{'PM 胜率':>8} {'PM 均值':>9} {'PM 累计':>9} {'PM 回撤':>9}")
    print("-" * 90)
    for strat in ("Q1", "Q2", "Q3"):
        both = summary["by_strategy"][strat]["overall"]
        am, pm = both["am"], both["pm"]
        if am["n"] == 0:
            print(f"{strat:<4} {'0':>5} | (无入选)")
            continue
        print(f"{strat:<4} {am['n']:>5} | "
              f"{_fmt_pct(am['win_rate']):>8} {_fmt_pct(am['mean_ret']):>9} {_fmt_pct(am['total_ret']):>9} {_fmt_pct(am['max_dd']):>9} | "
              f"{_fmt_pct(pm['win_rate']):>8} {_fmt_pct(pm['mean_ret']):>9} {_fmt_pct(pm['total_ret']):>9} {_fmt_pct(pm['max_dd']):>9}")
    print(f"{'='*90}")
    print("AM = 次日 open 卖(方案原设计); PM = 次日 close 卖(尾盘近似)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
