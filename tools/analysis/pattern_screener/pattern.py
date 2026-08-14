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


# ————————————————————————————————————————————————
# 箱体 v2(按策略提供者规格重构;v1 detect_box 保留供 A/B 对比)
# 均衡口径:振幅带 + 站稳突破 + 放量 = 硬门;触碰/缩量/横盘 = 软信号 → 结构评分 ≥ 下限才达标。
# 趋势门(MA200+短均线多头)在 screen_box 层叠加(需更长历史)。只用 K线,防未来函数由调用方按 t 截取保证。
# ————————————————————————————————————————————————
def _count_touches(highs, lows, rail: float, tol: float, rebound: float, upper: bool) -> int:
    """带容差触轨 + 回落/反弹确认的有效触碰次数。

    upper=True:high 进入上轨容差带([箱顶×(1-tol), ∞))算触上轨,其后需回落 ≥rebound 才算一次有效触碰;
    upper=False:low 进入下轨容差带算触下轨,其后需反弹 ≥rebound。tol/rebound 为小数(如 0.01)。
    相邻同一波只计一次(找到确认后从确认点继续)。
    """
    n = len(highs)
    cnt = 0
    i = 0
    while i < n:
        touch = (highs[i] >= rail * (1 - tol)) if upper else (lows[i] <= rail * (1 + tol))
        if not touch:
            i += 1
            continue
        peak = highs[i] if upper else lows[i]
        j = i + 1
        confirmed = False
        while j < n:
            if upper and lows[j] <= peak * (1 - rebound):        # 触上轨后回落≥rebound
                confirmed = True
                break
            if (not upper) and highs[j] >= peak * (1 + rebound):  # 触下轨后反弹≥rebound
                confirmed = True
                break
            j += 1
        if confirmed:
            cnt += 1
            i = j                                                 # 从回落/反弹处继续找下一次
        else:
            i += 1
    return cnt


def _shrinking_volume(vols, back_frac: float, ratio: float) -> tuple[bool, float]:
    """后段(末 back_frac 窗)均量 ≤ 前段均量 ×ratio 视作"后期缩量"。返回 (是否缩量, 量比)。"""
    n = len(vols)
    k = max(1, int(round(n * back_frac)))
    if n - k < 1:
        return False, float("nan")
    front = vols[: n - k]
    back = vols[n - k:]
    fmean = sum(front) / len(front)
    bmean = sum(back) / len(back)
    q = bmean / fmean if fmean > 0 else float("nan")
    return (bool(fmean > 0 and bmean <= fmean * ratio), round(q, 3) if q == q else q)


def _sideways(closes, slope_cap: float) -> tuple[bool, float]:
    """箱体内收盘对时间的归一化净漂移 |k×N/mean| ≤ slope_cap 视作横盘不单边。返回 (是否横盘, 归一斜率)。"""
    n = len(closes)
    if n < 3:
        return False, float("nan")
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(closes) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0 or my <= 0:
        return False, float("nan")
    slope = sum((xs[i] - mx) * (closes[i] - my) for i in range(n)) / denom
    drift = slope * (n - 1) / my                       # 窗口内净漂移(占均价比例)
    return (abs(drift) <= slope_cap, round(drift, 4))


def _pick_box_window(df: pd.DataFrame, c: dict):
    """在窗口区间内选箱体窗(不含末根):振幅落 [下界,上界] 的窗中,取结构评分最高者。

    返回 (win, 箱顶, 箱底, 振幅%, 结构评分, 软信号明细) 或 None(无合规窗)。
    """
    n = len(df)
    lo_w, hi_w = int(c["窗口区间"][0]), int(c["窗口区间"][1])
    tol = c["触碰容差%"] / 100.0
    reb = c["回落反弹%"] / 100.0
    high, low, vol, close = (df["high"].to_numpy(float), df["low"].to_numpy(float),
                             df["volume"].to_numpy(float), df["close"].to_numpy(float))
    best = None
    for win in range(lo_w, hi_w + 1, 5):               # 步长5控性能(回测逐根扫)
        if n < win + 1:
            continue
        s, e = n - win - 1, n - 1                       # 不含末根的箱体段 [s, e)
        seg_hi, seg_lo = high[s:e], low[s:e]
        seg_vol, seg_close = vol[s:e], close[s:e]
        top, bot = float(seg_hi.max()), float(seg_lo.min())
        if bot <= 0:
            continue
        amp = (top - bot) / bot * 100.0
        if not (c["振幅下界%"] <= amp <= c["振幅上界%"]):
            continue
        up_t = _count_touches(seg_hi, seg_lo, top, tol, reb, upper=True)
        dn_t = _count_touches(seg_hi, seg_lo, bot, tol, reb, upper=False)
        touch_ok = up_t >= c["触碰次数"] and dn_t >= c["触碰次数"]
        shrink_ok, qratio = _shrinking_volume(seg_vol, c["缩量后段占比"], c["缩量比"])
        flat_ok, drift = _sideways(seg_close, c["横盘斜率上限"])
        score = round((int(touch_ok) + int(shrink_ok) + int(flat_ok)) / 3.0, 3)
        cand = (win, round(top, 3), round(bot, 3), round(amp, 2), score,
                {"触上次数": up_t, "触下次数": dn_t, "触碰达标": touch_ok,
                 "缩量": shrink_ok, "量比": qratio, "横盘": flat_ok, "净漂移": drift})
        if best is None or cand[4] > best[4]:
            best = cand
    return best


def detect_box_v2(df: pd.DataFrame, cfg: dict = None) -> dict:
    """箱体 v2 几何判定(均衡口径)。硬门:振幅带 + 站稳突破 + 放量;软门:结构评分 ≥ 下限。

    结构评分 = 触碰达标/缩量/横盘 三软信号等权(0~1)。达标 = 突破 AND 放量 AND 结构评分≥下限
    (振幅带在选窗时已保证)。趋势门不在此(在 screen_box,需 MA200)。
    输出 特征 含 箱顶/箱底(供结构化输出与止损)、各软信号、结构评分。
    """
    c = (cfg or _CFG).get("箱体v2", _CFG.get("箱体v2", {}))
    n = len(df)
    need = int(c["窗口区间"][0]) + 1
    if n < need:
        return {"达标": False, "命中": "箱体v2", "特征": {"原因": f"样本不足(<{need})"}}
    picked = _pick_box_window(df, c)
    if picked is None:
        return {"达标": False, "命中": "箱体v2", "特征": {"原因": "无合规箱体窗(振幅不在带内)"}}
    win, top, bot, amp, score, soft = picked
    last_c = float(df["close"].iloc[-1])
    last_v = float(df["volume"].iloc[-1])
    seg_vol = df["volume"].to_numpy(float)[n - win - 1:n - 1]
    seg_vmean = float(seg_vol.mean()) if len(seg_vol) else 0.0
    站稳 = last_c > top * (1 + c["站稳容差%"] / 100.0)
    放量 = bool(seg_vmean > 0 and last_v > seg_vmean * c["突破放量倍数"])
    结构达标 = score >= c["结构分下限"]
    ok = bool(站稳 and 放量 and 结构达标)
    return {"达标": ok, "命中": "箱体v2",
            "特征": {"窗口": win, "箱顶": top, "箱底": bot, "振幅%": amp,
                     "站稳": 站稳, "放量": 放量, "量比突破": round(last_v / seg_vmean, 2) if seg_vmean else None,
                     "结构评分": score, "结构达标": 结构达标, **soft}}


def _pick_box_window_strict(df: pd.DataFrame, c: dict):
    """严格横盘箱体选窗(不含末根):振幅落**窄带** [下界,上界] 且 触碰/缩量/横盘三软信号
    **全部为硬门**(不再结构分放行)的窗中,取振幅最窄者(最贴近严格横盘矩形)。

    与 v2 `_pick_box_window` 的差异:三软信号由「加权评分」升格为「全True 才收」(由软改硬),
    带宽由 v2 的宽带收紧到策略给的窄带。返回 (win, 箱顶, 箱底, 振幅%, 触碰合计, 软信号明细) 或 None。
    """
    n = len(df)
    lo_w, hi_w = int(c["窗口区间"][0]), int(c["窗口区间"][1])
    tol = c["触碰容差%"] / 100.0
    reb = c["回落反弹%"] / 100.0
    high, low, vol, close = (df["high"].to_numpy(float), df["low"].to_numpy(float),
                             df["volume"].to_numpy(float), df["close"].to_numpy(float))
    best = None
    for win in range(lo_w, hi_w + 1, 5):
        if n < win + 1:
            continue
        s, e = n - win - 1, n - 1                       # 不含末根的箱体段 [s, e)
        seg_hi, seg_lo = high[s:e], low[s:e]
        seg_vol, seg_close = vol[s:e], close[s:e]
        top, bot = float(seg_hi.max()), float(seg_lo.min())
        if bot <= 0:
            continue
        amp = (top - bot) / bot * 100.0
        if not (c["振幅下界%"] <= amp <= c["振幅上界%"]):   # 窄带(硬门)
            continue
        up_t = _count_touches(seg_hi, seg_lo, top, tol, reb, upper=True)
        dn_t = _count_touches(seg_hi, seg_lo, bot, tol, reb, upper=False)
        touch_ok = up_t >= c["触碰次数"] and dn_t >= c["触碰次数"]
        shrink_ok, qratio = _shrinking_volume(seg_vol, c["缩量后段占比"], c["缩量比"])
        flat_ok, drift = _sideways(seg_close, c["横盘斜率上限"])
        if not (touch_ok and shrink_ok and flat_ok):    # 三软信号全部硬门(由软改硬)
            continue
        cand = (win, round(top, 3), round(bot, 3), round(amp, 2), up_t + dn_t,
                {"触上次数": up_t, "触下次数": dn_t, "缩量": shrink_ok, "量比": qratio,
                 "横盘": flat_ok, "净漂移": drift})
        # 严格口径:取振幅最窄者(最像横盘矩形);同振幅取触碰更多者
        if best is None or (cand[3], -cand[4]) < (best[3], -best[4]):
            best = cand
    return best


def detect_box_strict(df: pd.DataFrame, cfg: dict = None) -> dict:
    """严格横盘箱体放量突破(Stage1 观察池触发源)。

    收紧 v2:振幅窄带 [8,20] + 触碰≥2/横盘/缩量**三软改硬** + 站稳突破 + 放量,全部硬门。
    cfg = THRESHOLDS["回踩低吸"]["严格箱体"] 结构(缺省从此读)。
    输出 特征.箱顶 = 突破位/后续支撑参考(供 Stage2 回踩判定与止损)。
    """
    c = cfg if cfg is not None else THRESHOLDS["回踩低吸"]["严格箱体"]
    n = len(df)
    lo_w = int(c["窗口区间"][0])
    if n < lo_w + 1:
        return {"达标": False, "命中": "严格箱体", "特征": {"原因": f"样本不足(<{lo_w + 1})"}}
    close = df["close"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    # 便宜快筛(回测逐根扫的性能命门):突破日须为上涨且放量,否则免去多窗扫描。
    if close[-1] <= close[-2]:
        return {"达标": False, "命中": "严格箱体", "特征": {"原因": "末根非上涨,非突破日"}}
    back = vol[max(0, n - 21):n - 1]
    if len(back) == 0 or vol[-1] <= (back.mean() * c["突破放量倍数"]) * 0.5:
        return {"达标": False, "命中": "严格箱体", "特征": {"原因": "末根量不足,非放量突破"}}
    picked = _pick_box_window_strict(df, c)
    if picked is None:
        return {"达标": False, "命中": "严格箱体", "特征": {"原因": "无合规严格箱体窗"}}
    win, top, bot, amp, touch_sum, soft = picked
    last_c = float(close[-1])
    last_v = float(vol[-1])
    seg_vol = vol[n - win - 1:n - 1]
    seg_vmean = float(seg_vol.mean()) if len(seg_vol) else 0.0
    站稳 = last_c > top * (1 + c["站稳容差%"] / 100.0)
    放量 = bool(seg_vmean > 0 and last_v > seg_vmean * c["突破放量倍数"])
    ok = bool(站稳 and 放量)
    return {"达标": ok, "命中": "严格箱体",
            "特征": {"窗口": win, "箱顶": top, "箱底": bot, "振幅%": amp,
                     "站稳": 站稳, "放量": 放量,
                     "量比突破": round(last_v / seg_vmean, 2) if seg_vmean else None,
                     **soft}}


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
