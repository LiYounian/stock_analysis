"""形态选股·"金叉前兆·冲高兑现" 触发件与趋势门（回测研究层）。

策略立论（见 docs/计划/2026-09-15_形态选股策略_计划.md）：
  金叉是**领先信号**——反推样本里金叉多在"冲高"前 T-3~T-4 出现。
  D 晚扫出刚发生金叉的票 → D+1 回踩限价埋伏 → 5 日内冲高触固定止盈即卖。

本模块只产**信号布尔帧**（每票全历史一次算出各触发件/趋势门的布尔列），
交给回测引擎按日取值。**无未来函数**：每行只用 ≤该行的数据（均线/MACD/KDJ 皆滚动、
交叉用 shift(1) 比较、区间分位用 rolling 回看）。

研究隔离：放 backtest/ 不入生产选股注册；采用后再把阈值提升到
tools/config/strategy.py 的 THRESHOLDS["形态金叉前兆"]。当前阈值为**初值**，
回测按计划文档 §4.2 矩阵扫参。

⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import pandas as pd

from tools.analysis import technical

# —— 初值阈值（回测扫参对象；采用后进 THRESHOLDS）——
SLOPE_LOOKBACK = 5          # 均线斜率回看根数：MA(t)/MA(t-5)-1
POS_WINDOW = 60            # 区间分位窗口（60 日高低点）
STAGE_A_POS_MAX = 0.35     # 档A 底部反转：近60日区间分位 ≤ 35%
STAGE_B_MA20_TOL = 0.05    # 档B 上升回踩：|收/MA20-1| ≤ 5%（贴近支撑、非高点）
RESONANCE_WIN = 2          # 多信号共振：近 ≤2 日内任 2 个触发件成立
RESONANCE_MIN = 2


def _cross_up(a: pd.Series, b: pd.Series) -> pd.Series:
    """a 上穿 b 的布尔序列：a[t] > b[t] 且 a[t-1] <= b[t-1]。仅回看，无未来函数。"""
    return (a > b) & (a.shift(1) <= b.shift(1))


def compute_signal_frame(kline: pd.DataFrame) -> pd.DataFrame:
    """给一只票的全历史 K 线（升序，列含 open/high/low/close/volume），
    一次算出触发件与趋势门的布尔帧，index 对齐 kline.index。

    触发件（回测维度全交叉扫）：
      trig_kdj    KDJ 金叉：K 上穿 D 且 J 上行
      trig_ma5x10 短均线金叉：MA5 上穿 MA10
      trig_macd   MACD 金叉：DIF 上穿 DEA（两线交叉口径）
      trig_reso   多信号共振：近 ≤RESONANCE_WIN 日内 ≥RESONANCE_MIN 个触发件成立，
                  且当日至少 1 个新触发（供 D 日选股用）
    趋势门（回测维度扫；另有"不加门"= 任一触发即可）：
      gate_A 底部反转：收盘站上 MA20 且近60日区间分位 ≤ STAGE_A_POS_MAX
      gate_B 上升回踩：MA20&MA60 斜率>0 且 |收/MA20-1| ≤ STAGE_B_MA20_TOL
      gate_C 强势：多头排列 MA5≥MA10≥MA20≥MA60
    附：辅助数值列（entry 用昨收，止盈/诊断用）。
    """
    c = kline["close"]
    high, low = kline["high"], kline["low"]
    out = pd.DataFrame(index=kline.index)

    ma5 = technical.ma(c, 5)
    ma10 = technical.ma(c, 10)
    ma20 = technical.ma(c, 20)
    ma60 = technical.ma(c, 60)
    md = technical.macd(c)
    dif, dea = md["dif"], md["dea"]
    kd = technical.kdj(kline)
    K, D, J = kd["k"], kd["d"], kd["j"]

    # —— 触发件 ——
    out["trig_kdj"] = _cross_up(K, D) & (J > J.shift(1))
    out["trig_ma5x10"] = _cross_up(ma5, ma10)
    out["trig_macd"] = _cross_up(dif, dea)

    trig3 = out[["trig_kdj", "trig_ma5x10", "trig_macd"]].astype(int)
    # 近 RESONANCE_WIN 日内每个触发件是否至少出现一次（rolling max，仅回看）
    recent = trig3.rolling(RESONANCE_WIN, min_periods=1).max()
    distinct_recent = recent.sum(axis=1)
    fires_today = trig3.sum(axis=1)
    out["trig_reso"] = (distinct_recent >= RESONANCE_MIN) & (fires_today >= 1)

    # —— 趋势门 ——
    ma20_slope = ma20 / ma20.shift(SLOPE_LOOKBACK) - 1.0
    ma60_slope = ma60 / ma60.shift(SLOPE_LOOKBACK) - 1.0
    low_w = c.rolling(POS_WINDOW).min()
    high_w = c.rolling(POS_WINDOW).max()
    pos = (c - low_w) / (high_w - low_w)

    out["gate_A"] = (c > ma20) & (pos <= STAGE_A_POS_MAX)
    out["gate_B"] = (ma20_slope > 0) & (ma60_slope > 0) & ((c / ma20 - 1.0).abs() <= STAGE_B_MA20_TOL)
    out["gate_C"] = (ma5 >= ma10) & (ma10 >= ma20) & (ma20 >= ma60)

    # —— 辅助数值（entry/诊断）——
    out["prev_close"] = c            # 第 t 行的收盘 = 次日(t+1)入场的"昨收"
    out["ma20"] = ma20
    out["ma60"] = ma60
    out["J"] = J
    out["pos60"] = pos
    out["ma20_slope"] = ma20_slope
    out["ma60_slope"] = ma60_slope
    return out


TRIGGERS = ("trig_kdj", "trig_ma5x10", "trig_macd", "trig_reso")
GATES = ("gate_A", "gate_B", "gate_C", "gate_N")   # gate_N = 不加门


def passes(frame_row: pd.Series, trigger: str, gate: str) -> bool:
    """某票某日是否入选：触发件成立 且 趋势门成立（gate_N 恒真）。"""
    if not bool(frame_row.get(trigger, False)):
        return False
    if gate == "gate_N":
        return True
    return bool(frame_row.get(gate, False))
