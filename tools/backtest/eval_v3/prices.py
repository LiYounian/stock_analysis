"""PriceBook:按 code 缓存前复权 open/high/low/close + date→idx,供 T+1 入场定价与实现收益。

与旧 strategy_scorecard.KlineBook 的区别:**多缓存 open**(T+1 入场价默认取 open[T+1]),
并暴露 `entry_at` 统一封装 T+1 入场口径 + 隔夜跳空拆分。单测可注入 loader 离线跑。
"""
from __future__ import annotations

import logging

import numpy as np

from tools.collectors import market

logger = logging.getLogger("backtest.eval_v3.prices")


class PriceBook:
    """一次加载多次查:code → (open[], high[], low[], close[], {date:idx}) 或 None。"""

    def __init__(self, loader=market.load_kline):
        self._loader = loader
        self._cache: dict[str, tuple | None] = {}

    def get(self, code: str):
        if code not in self._cache:
            try:
                df = self._loader(code).reset_index(drop=True)
                dmap = ({str(x)[:10]: i for i, x in enumerate(df["date"].tolist())}
                        if "date" in df.columns else {})
                op = (df["open"].to_numpy(float) if "open" in df.columns
                      else df["close"].to_numpy(float))   # 无 open 兜底用 close(注明)
                self._cache[code] = (op, df["high"].to_numpy(float),
                                     df["low"].to_numpy(float),
                                     df["close"].to_numpy(float), dmap)
            except Exception as e:   # noqa: BLE001
                logger.debug("kline 加载失败 %s: %s", code, str(e)[:60])
                self._cache[code] = None
        return self._cache[code]

    def idx_of(self, code: str, date: str):
        rec = self.get(code)
        if rec is None:
            return None
        return rec[4].get(str(date)[:10])


def entry_price(rec, idx: int) -> tuple[float | None, bool]:
    """T+1 入场价:默认 open[idx+1];无 open(退化=close 数组)时用 close[idx+1]。

    返回 (entry_px, used_close_fallback)。idx+1 越界或价≤0 → (None, False)(无法入场)。
    """
    op, _high, _low, close, _dmap = rec
    j = idx + 1
    if j >= len(close):
        return None, False
    px = float(op[j])
    used_fallback = False
    if not (px > 0):                 # open 缺失/为 0 → 退化用 close[T+1]
        px = float(close[j])
        used_fallback = True
    return (px, used_fallback) if px > 0 else (None, used_fallback)


def realized(rec, idx: int, direction: int, horizons) -> dict:
    """T+1 入场口径下,各 horizon 的实现收益 + 双口径命中 + 隔夜跳空拆分。

    口径(见任务②):
      · 入场 = T+1 入场价 entry(open[idx+1] 默认);信号日 T 的 close 仅用于拆隔夜跳空。
      · horizon h 实现收益 r_h = close[idx+h]/entry − 1(**分母改为 T+1 入场价**,退出点仍锚信号日 T+h)。
      · 隔夜跳空 gap = entry/close[idx] − 1,**单列、不算策略功劳**;
        总收益 close[idx+h]/close[idx]−1 = (1+gap)(1+r_h)−1。
      · 期末命中 hit_end = sign(r_h)==sign(direction)。
      · 期内触及 hit_intra:入场后窗口 [idx+1, idx+h] 内,看多=max(high)>entry;看空=min(low)<entry。
      · 防未来函数:h 仅当 idx+h < len 才 matured;需能 T+1 入场(idx+1<len,h≥1 时由 idx+h<len 保证)。

    返回 {h: {matured, r, hit_end, hit_intra, gap, entry, entry_fallback}}。
    未成熟/无法入场 → 该 h 全 None。direction=0 → hit 记 None(不计方向命中)。
    """
    out = {h: {"matured": False, "r": None, "hit_end": None, "hit_intra": None,
               "gap": None, "entry": None, "entry_fallback": None} for h in horizons}
    if rec is None:
        return out
    op, high, low, close, _dmap = rec
    n = len(close)
    if idx is None or idx < 0 or idx >= n or not (close[idx] > 0):
        return out
    entry, used_fb = entry_price(rec, idx)
    if entry is None:
        return out                       # 无法 T+1 入场(信号日为最后一根)
    gap = float(entry / close[idx] - 1.0) * 100.0
    for h in horizons:
        if idx + h >= n:
            continue                     # 退出点未到 → pending
        r = float(close[idx + h] / entry - 1.0) * 100.0
        cell = out[h]
        cell.update(matured=True, r=r, gap=gap, entry=entry, entry_fallback=used_fb)
        if direction == 0:
            continue
        cell["hit_end"] = int(np.sign(r) == np.sign(direction))
        win_hi = float(np.max(high[idx + 1:idx + h + 1]))
        win_lo = float(np.min(low[idx + 1:idx + h + 1]))
        cell["hit_intra"] = int(win_hi > entry) if direction > 0 else int(win_lo < entry)
    return out
