"""带离场的可实现收益模拟器(eval_v3.1)。

现状 eval_v3 的 `r`=**固定持有**(T+1 入场、死拿到 T+h 收盘),会把"涨过又跌回"的利润算没。
本模块升级到**带离场的可实现收益**——从 T+1 入场后逐日推进,按下列优先级择机离场:

  1. **盘中止盈**:某日 `high ≥ 入场价×(1+止盈%)` → **在触线价成交**(=入场价×(1+止盈%),
     **不是当天收盘价**;过了 T+1 可在任意时点离场、离场价=触线价)。
  2. **盘中止跌**:某日 `low ≤ 入场价×(1−止跌%)` → 触线价成交(=入场价×(1−止跌%))。
  3. **同日高摸止盈 ∧ 低摸止跌**:日K 看不出先后 → **保守假设先触止跌**,并给该样本打 `path_ambiguous`
     标记(报告里统计占比,如实标注这是日频回测的边界)。
  4. 都没触:
     · **危险信号离场**(可选开关):策略自身在该日**不再选该票** → 按**当日收盘**卖(纯 as-of,
       用策略每日输出当离场信号,不作弊)。
     · **时间止损**:持有到第 time_stop 日(退出锚 idx+time_stop,与固定持有 horizon=time_stop 完全对齐)
       仍未触 → 按当日**收盘**卖。

离场毛收益 = 离场价/入场价 − 1;净收益 = 毛收益 − 往返成本(0.1%/0.2% 双档在上层扫)。

**防未来函数**:只用该票入场后自身价格路径 + 策略自身每日 as-of 再选(危险信号);maturity 要求
`idx+time_stop < n`(与固定持有 horizon=time_stop 同一到期条件),保证三口径在**同一样本**上配对可比。

**方向**:当前所有存活策略均 long-only(direction=+1),本模拟器只实现多头;direction≠+1 记不支持。
"""
from __future__ import annotations

from . import prices as _pr

# 预注册参数网格(别拍单值,每组都跑)。
TP_GRID = (5.0, 8.0, 10.0)        # 止盈线 %
SL_GRID = (3.0, 5.0, 8.0)         # 止跌线 %
TIME_STOP_GRID = (5, 10)          # 时间止损(交易日;退出锚 idx+N,与固定持有 horizon=N 对齐)
COST_GRID = (0.1, 0.2)            # 往返成本 %(双档)

# 离场原因枚举
R_TP = "止盈"
R_SL = "止跌"
R_SL_AMBIG = "止跌(同日双触)"
R_DANGER = "危险信号"
R_TIME = "时间止损"


def simulate_long_exit(rec, idx: int, tp_pct: float, sl_pct: float, time_stop: int,
                       cost_pct: float = 0.0, is_selected_on=None,
                       idx2date=None) -> dict:
    """单票多头离场模拟(纯函数,可注入 is_selected_on 离线单测)。

    参数:
      rec        = (open[], high[], low[], close[], dmap),来自 PriceBook.get。
      idx        = 信号日 T 在该票 kline 的下标;入场价 = open[idx+1](缺→close[idx+1],复用 prices.entry_price)。
      tp_pct/sl_pct = 止盈/止跌线(正数%);time_stop = 时间止损(交易日,退出锚 idx+time_stop)。
      cost_pct   = 往返成本%(从毛收益里扣)。
      is_selected_on(bar_date)->Optional[bool]:该票在 bar_date 是否仍被策略选中。
                   None=当日非该策略预测日(无法判断→继续持有);True=仍选;False=已剔除(危险信号)。
                   传 None(默认)则**关闭危险信号离场**(只走止盈/止跌/时间止损)。
      idx2date   = {bar_idx: 'YYYY-MM-DD'},供危险信号把 bar 下标映射回日期查 is_selected_on。

    返回 dict:
      {matured, exit_reason, path_ambiguous, hold_days, exit_idx, entry, entry_fallback,
       gross_pct, net_pct}
    未成熟(idx+time_stop 越界 / 无法 T+1 入场)→ matured=False 其余 None。
    """
    out = {"matured": False, "exit_reason": None, "path_ambiguous": False,
           "hold_days": None, "exit_idx": None, "entry": None, "entry_fallback": None,
           "gross_pct": None, "net_pct": None}
    if rec is None:
        return out
    op, high, low, close, _dmap = rec
    n = len(close)
    if idx is None or idx < 0 or idx >= n or not (close[idx] > 0):
        return out
    # 到期条件与固定持有 horizon=time_stop 完全一致(idx+time_stop<n),保证配对同样本。
    if idx + time_stop >= n:
        return out
    entry, used_fb = _pr.entry_price(rec, idx)
    if entry is None:
        return out
    out.update(matured=True, entry=entry, entry_fallback=used_fb)

    tp_price = entry * (1.0 + tp_pct / 100.0)
    sl_price = entry * (1.0 - sl_pct / 100.0)
    danger_on = is_selected_on is not None

    for j in range(1, time_stop + 1):
        k = idx + j
        hi, lo = float(high[k]), float(low[k])
        tp_hit = hi >= tp_price
        sl_hit = lo <= sl_price
        exit_px = None
        if tp_hit and sl_hit:
            # 同日双触:日K 看不出先后 → 保守假设先触止跌,打 path_ambiguous。
            exit_px, reason, out["path_ambiguous"] = sl_price, R_SL_AMBIG, True
        elif tp_hit:
            exit_px, reason = tp_price, R_TP
        elif sl_hit:
            exit_px, reason = sl_price, R_SL
        else:
            # 无盘中触线。若开危险信号且当日该策略"不再选该票" → 收盘离场(纯 as-of)。
            if danger_on and j < time_stop and idx2date is not None:
                bar_date = idx2date.get(k)
                sel = is_selected_on(bar_date) if bar_date is not None else None
                if sel is False:
                    exit_px, reason = float(close[k]), R_DANGER
            if exit_px is None and j == time_stop:
                exit_px, reason = float(close[k]), R_TIME
        if exit_px is not None:
            gross = (exit_px / entry - 1.0) * 100.0
            out.update(exit_reason=reason, hold_days=j, exit_idx=k,
                       gross_pct=round(gross, 4), net_pct=round(gross - cost_pct, 4))
            return out
    # 理论不可达(j==time_stop 必给时间止损),兜底。
    exit_px = float(close[idx + time_stop])
    gross = (exit_px / entry - 1.0) * 100.0
    out.update(exit_reason=R_TIME, hold_days=time_stop, exit_idx=idx + time_stop,
               gross_pct=round(gross, 4), net_pct=round(gross - cost_pct, 4))
    return out
