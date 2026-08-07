"""形态几何识别引擎(V1 模块二核心)。

四类经典形态的**可计算几何匹配**(不交给模型自由发挥,全部落特征):
  箱体/平台突破 · 欧奈尔杯柄 · 楔形(收敛)· 旗形(旗杆+旗面)。
所有几何参数走 Config(`strategy.THRESHOLDS["形态选股"]`),可配可调。

输入:kline DataFrame(列含 date/open/high/low/close/volume,时间升序)。
输出:`detect()` → {命中形态:[...], 达标:bool, 明细:{形态名:{达标,特征}}}。
诚实性:几何阈值为占位初值(待策略端标定),形态识别是启发式近似,非唯一定义。
需求见 docs/计划/V1_形态选股与市场状态系统.md F2.1。
"""
from __future__ import annotations

import pandas as pd

from tools.config.strategy import THRESHOLDS

_CFG = THRESHOLDS["形态选股"]


def _pct(a: float, b: float) -> float:
    """(a-b)/b*100;b=0 返回 0。"""
    return (a - b) / b * 100.0 if b else 0.0


def _vol_confirm(vol: pd.Series, i: int, lookback: int, mult: float) -> bool:
    """第 i 根量 > 前 lookback 根均量 × mult。"""
    if i < 1:
        return False
    base = vol.iloc[max(0, i - lookback):i]
    if len(base) == 0:
        return False
    avg = base.mean()
    return bool(avg > 0 and vol.iloc[i] > avg * mult)


# ————————————————————————————————————————————————
# 箱体 / 平台突破
# ————————————————————————————————————————————————
def detect_box(df: pd.DataFrame, cfg: dict = None) -> dict:
    """末根放量突破一段窄幅箱体上沿 → 达标。"""
    c = (cfg or _CFG)["箱体"]
    win = int(c["窗口"])
    n = len(df)
    if n < win + 1:
        return {"达标": False, "特征": {"原因": "样本不足"}}
    high, low, close, vol = df["high"], df["low"], df["close"], df["volume"]
    base_hi = float(high.iloc[n - win - 1:n - 1].max())   # 不含末根的箱体
    base_lo = float(low.iloc[n - win - 1:n - 1].min())
    height = _pct(base_hi, base_lo)
    last = float(close.iloc[-1])
    tight = height <= c["高度上限%"]
    broke = last > base_hi * (1 + c["突破幅度%"] / 100.0)
    volok = _vol_confirm(vol, n - 1, win, c["突破放量倍数"])
    ok = bool(tight and broke and volok)
    return {"达标": ok, "特征": {"箱高%": round(height, 2), "箱顶": round(base_hi, 2),
                                 "收盘": round(last, 2), "窄幅": tight, "突破": broke, "放量": volok}}


# ————————————————————————————————————————————————
# 欧奈尔杯柄
# ————————————————————————————————————————————————
def detect_cup_handle(df: pd.DataFrame, cfg: dict = None) -> dict:
    """左沿高点→回落成杯(深度达标)→回补接近左沿→浅手柄回调→末根放量突破左沿。"""
    c = (cfg or _CFG)["杯柄"]
    close, vol = df["close"].reset_index(drop=True), df["volume"].reset_index(drop=True)
    n = len(close)
    if n < c["杯最短天数"] + 3:
        return {"达标": False, "特征": {"原因": "样本不足"}}

    left = close.iloc[: max(3, n // 3)]
    rim_idx = int(left.idxmax())                      # 左沿高点
    rim = float(close.iloc[rim_idx])
    after = close.iloc[rim_idx + 1:]
    if len(after) < 3:
        return {"达标": False, "特征": {"原因": "左沿过晚"}}
    trough_idx = int(after.idxmin())                  # 杯底
    trough = float(close.iloc[trough_idx])
    depth = _pct(rim, trough)                          # 杯深%(正值)
    lo, hi = c["杯深%区间"]
    depth_ok = lo <= depth <= hi
    cup_len = trough_idx - rim_idx
    len_ok = cup_len >= c["杯最短天数"] // 2           # 左半杯长度(到杯底)

    # 回补点:杯底之后**首次**触及左沿容差(右沿),非全局最高(全局最高是末根突破)
    thr = rim * (1 - c["回补前高容差%"] / 100.0)
    rr_idx = next((j for j in range(trough_idx + 1, n) if float(close.iloc[j]) >= thr), None)
    recov_ok = rr_idx is not None
    if not recov_ok:
        return {"达标": False, "特征": {"杯深%": round(depth, 2), "回补": False}}

    # 手柄:回补点 → 末根前一根 的浅回调(末根留作突破)
    handle = close.iloc[rr_idx:n - 1]
    handle_len = len(handle)
    handle_dd = _pct(float(handle.max()), float(handle.min())) if handle_len else 0.0
    handle_ok = 0 < handle_len <= c["手柄最长天数"] and handle_dd <= c["手柄最大回撤%"]

    last = float(close.iloc[-1])
    broke = last > rim                                  # 末根突破左沿
    volok = _vol_confirm(vol, n - 1, c["杯最短天数"], c["突破放量倍数"])
    ok = bool(depth_ok and len_ok and recov_ok and handle_ok and broke and volok)
    return {"达标": ok, "特征": {"杯深%": round(depth, 2), "左沿": round(rim, 2),
                                 "杯长": cup_len, "回补": recov_ok, "手柄天数": handle_len,
                                 "手柄回撤%": round(handle_dd, 2), "突破": broke, "放量": volok}}


# ————————————————————————————————————————————————
# 楔形(区间收敛)
# ————————————————————————————————————————————————
def detect_wedge(df: pd.DataFrame, cfg: dict = None) -> dict:
    """窗口内区间显著收敛(后半区间 ≤ 前半 × 收敛比)+ 末根突破 → 达标。"""
    c = (cfg or _CFG)["楔形"]
    win = int(c["窗口"])
    n = len(df)
    if n < win + 1:
        return {"达标": False, "特征": {"原因": "样本不足"}}
    seg = df.iloc[n - win - 1:n - 1]                    # 不含末根的收敛段
    half = len(seg) // 2
    h1, l1 = seg["high"].iloc[:half], seg["low"].iloc[:half]
    h2, l2 = seg["high"].iloc[half:], seg["low"].iloc[half:]
    r1 = float(h1.max() - l1.min())
    r2 = float(h2.max() - l2.min())
    converg = r1 > 0 and r2 <= r1 * c["最小收敛比"]
    apex_hi = float(seg["high"].iloc[half:].max())
    last = float(df["close"].iloc[-1])
    broke = last > apex_hi
    volok = _vol_confirm(df["volume"], n - 1, win, c["突破放量倍数"])
    ok = bool(converg and broke and volok)
    return {"达标": ok, "特征": {"前段幅": round(r1, 2), "后段幅": round(r2, 2),
                                 "收敛": converg, "突破": broke, "放量": volok}}


# ————————————————————————————————————————————————
# 旗形(旗杆 + 旗面)
# ————————————————————————————————————————————————
def detect_flag(df: pd.DataFrame, cfg: dict = None) -> dict:
    """近端出现急涨旗杆(短期涨幅达标)+ 随后浅幅横盘旗面(回撤受限)→ 达标。"""
    c = (cfg or _CFG)["旗形"]
    close = df["close"].reset_index(drop=True)
    n = len(close)
    pole_max, flag_max = int(c["旗杆最长天数"]), int(c["旗面最长天数"])
    if n < pole_max + 3:
        return {"达标": False, "特征": {"原因": "样本不足"}}

    flag = close.iloc[n - flag_max:]                    # 近端旗面
    flag_dd = _pct(float(flag.max()), float(flag.min()))
    flag_ok = flag_dd <= c["旗面最大回撤%"]
    pole_start = close.iloc[n - flag_max - pole_max: n - flag_max]   # 旗面之前的旗杆窗
    pole_gain = _pct(float(pole_start.iloc[-1]), float(pole_start.iloc[0])) if len(pole_start) else 0
    pole_ok = pole_gain >= c["旗杆最短涨幅%"]
    ok = bool(pole_ok and flag_ok)
    return {"达标": ok, "特征": {"旗杆涨幅%": round(pole_gain, 2), "旗面回撤%": round(flag_dd, 2),
                                 "旗杆": pole_ok, "旗面": flag_ok}}


PATTERNS = {"箱体": detect_box, "杯柄": detect_cup_handle,
            "楔形": detect_wedge, "旗形": detect_flag}


def detect(kline_df: pd.DataFrame, cfg: dict = None) -> dict:
    """跑全部形态检测器。返回命中形态列表 + 是否达标(任一命中)+ 各形态明细。

    单个检测器抛错不影响其它(记 error),形态识别失败降级为不达标。
    """
    cfg = cfg or _CFG
    detail = {}
    for name, fn in PATTERNS.items():
        try:
            detail[name] = fn(kline_df, cfg)
        except Exception as e:
            detail[name] = {"达标": False, "特征": {"error": str(e)[:60]}}
    matched = [n for n, r in detail.items() if r.get("达标")]
    return {"命中形态": matched, "达标": bool(matched), "明细": detail}
