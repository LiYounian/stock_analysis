"""预测/推荐引擎(统计版,纯本地)。

产出(全部百分比,与买入手数/金额无关):
- 近三次放量定位、支撑位/压力位
- 各持有期(1/5/10日)止盈止损位 + 最大亏损% + 目标盈利% + 风险收益比
- 各持有期情景预测:上涨概率 + 悲观/中位/乐观收益%(历史频率,非预言)
- 买卖倾向推荐

诚实性:概率是历史频率,区间基于波动率,均非未来保证。仅供参考,非投资建议。
参数见 tools/config/strategy.py THRESHOLDS['预测']。契约见 docs/计划/P3_Web展示与预测引擎.md P3-C。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

_P = THRESHOLDS["预测"]
DISCLAIMER = "概率基于历史频率、区间基于波动率,均非未来保证。仅供参考,非投资建议,风险自负。"


# ---------- 基础量 ----------
def atr(kline: pd.DataFrame, period: int = None) -> pd.Series:
    """ATR(真实波幅均值)。"""
    period = period or _P["ATR周期"]
    h, l, c = kline["high"], kline["low"], kline["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def recent_volume_spikes(kline: pd.DataFrame, n: int = None, ratio: float = None) -> list[dict]:
    """近 n 次放量日(量比>ratio),从最近往前找。返回 {date, close, 量比}。"""
    n = n or _P["近N次放量"]
    ratio = ratio or _P["放量_量比"]
    vol = kline["volume"]
    vol_ma5_prev = vol.rolling(5).mean().shift(1)
    vr = vol / vol_ma5_prev
    out = []
    for i in range(len(kline) - 1, -1, -1):
        if pd.notna(vr.iloc[i]) and vr.iloc[i] > ratio:
            out.append({"date": str(kline["date"].iloc[i])[:10],
                        "close": round(float(kline["close"].iloc[i]), 2),
                        "量比": round(float(vr.iloc[i]), 2)})
            if len(out) >= n:
                break
    return out


def support_resistance(kline: pd.DataFrame, window: int = None, keep: int = None) -> dict:
    """结构性支撑/压力位:swing 高低点 pivot 中,现价下方最近 keep 个为支撑、上方为压力。"""
    window = window or _P["swing窗口"]
    keep = keep or _P["支撑压力_取几档"]
    highs, lows = kline["high"], kline["low"]
    price = float(kline["close"].iloc[-1])
    piv_hi, piv_lo = [], []
    for i in range(window, len(kline) - window):
        seg_h = highs.iloc[i - window:i + window + 1]
        seg_l = lows.iloc[i - window:i + window + 1]
        if highs.iloc[i] == seg_h.max():
            piv_hi.append(round(float(highs.iloc[i]), 2))
        if lows.iloc[i] == seg_l.min():
            piv_lo.append(round(float(lows.iloc[i]), 2))
    supports = sorted({p for p in piv_lo if p < price}, reverse=True)[:keep]
    resistances = sorted({p for p in piv_hi if p > price})[:keep]
    return {"支撑位": supports, "压力位": resistances}


# ---------- 止盈止损(百分比,按持有期)----------
def stop_targets(price: float, atr_pct: float) -> dict:
    """各持有期止损/止盈位 + 最大亏损%/目标盈利% + 风险收益比。"""
    sk, tk = _P["止损_ATR倍数"], _P["止盈_ATR倍数"]
    out = {}
    for N in _P["持有期"]:
        band = atr_pct * math.sqrt(N)          # N 日预期波动(%)
        loss_pct = round(sk * band, 2)
        gain_pct = round(tk * band, 2)
        out[f"{N}日"] = {
            "止损位": round(price * (1 - loss_pct / 100), 2),
            "最大亏损%": loss_pct,
            "止盈位": round(price * (1 + gain_pct / 100), 2),
            "目标盈利%": gain_pct,
            "风险收益比": round(tk / sk, 2),
        }
    return out


# ---------- 情景预测(历史频率)----------
def scenarios(kline: pd.DataFrame) -> dict:
    """各持有期历史 N 日前瞻收益经验分布:上涨概率 + 悲观/中位/乐观分位(%)。

    ⚠️ 回测结论(2026-08-12,随机全A 29万观测):**「上涨概率%」几乎无区分力**——各预测
    分箱的实际上涨率都塌到基础率(~50%),Brier≈0.25(≈掷硬币)。因它是**无条件**历史频率、
    未条件化到当前形态/位置。**不应把它当"胜率"呈现**;区间[悲观,乐观]经分位调参(情景分位
    [7,93])后覆盖≈80%可信。详见 docs/计划/预测两条线_回测验证与调参_计划.md §6。"""
    close = kline["close"]
    ql, qm, qh = _P["情景分位"]
    out = {}
    for N in _P["持有期"]:
        fwd = (close.shift(-N) / close - 1).dropna() * 100
        if len(fwd) < 20:
            out[f"{N}日"] = {"上涨概率%": None, "样本数": int(len(fwd))}
            continue
        out[f"{N}日"] = {
            "上涨概率%": round(float((fwd > 0).mean() * 100), 1),
            f"悲观%(q{ql})": round(float(np.percentile(fwd, ql)), 2),
            f"中位%(q{qm})": round(float(np.percentile(fwd, qm)), 2),
            f"乐观%(q{qh})": round(float(np.percentile(fwd, qh)), 2),
            "样本数": int(len(fwd)),
        }
    return out


# ---------- 买卖倾向 ----------
def bias_recommendation(tech: dict, fundflow: dict | None, sentiment: dict | None = None) -> dict:
    """综合超买超卖/拐点/趋势/资金流/情绪打分 → 偏买入/偏卖出/观望。

    sentiment 为 record 的 sentiment 块 dict(含 净情绪分/样本数);None 或样本数为0时不计分,
    保证向后兼容。情绪仅一维输入并入,依据可追溯,非决定项。
    """
    score, reasons = 0, []
    ob = (tech.get("ob_os") or {}).get("结论")
    if ob == "超卖":
        score += 2; reasons.append("超卖+2")
    elif ob == "超买":
        score -= 2; reasons.append("超买-2")

    rev = (tech.get("reversal") or {}).get("拐点标签")
    if rev == "反弹启动":
        score += 2; reasons.append("拐点反弹启动+2")
    elif rev == "超跌待反弹":
        score += 1; reasons.append("超跌待反弹+1")

    rating = (tech.get("signal") or {}).get("评级")
    if rating == "偏多":
        score += 1; reasons.append("趋势偏多+1")
    elif rating == "偏空":
        score -= 1; reasons.append("趋势偏空-1")

    if fundflow:
        zhu = fundflow.get("今日主力净流入")
        streak = fundflow.get("主力连续净流入天数") or 0
        if isinstance(zhu, (int, float)):
            if zhu > 0:
                score += 1; reasons.append("主力净流入+1")
                if streak >= 2:
                    score += 1; reasons.append(f"主力连续{streak}天流入+1")
            elif zhu < 0:
                score -= 1; reasons.append("主力净流出-1")

    if sentiment:
        net = sentiment.get("净情绪分")
        n = sentiment.get("样本数") or 0
        if isinstance(net, (int, float)) and n > 0:
            w = _P["情绪权重"]
            if net >= _P["情绪偏多阈值"]:
                score += w; reasons.append(f"情绪偏多+{w}")
            elif net <= _P["情绪偏空阈值"]:
                score -= w; reasons.append(f"情绪偏空-{w}")

    conclusion = "偏买入" if score >= 2 else ("偏卖出" if score <= -2 else "观望")
    return {"结论": conclusion, "得分": score, "依据": reasons}


def bias_recommendation_council(tech: dict, fundflow: dict | None = None,
                                sentiment: dict | None = None) -> dict:
    """买卖倾向的合议迁移入口(F4 · D6=A)。委托 council 的「买卖倾向(默认组)」预设。

    与上面的 bias_recommendation 逐票 100% 等价(见 tests/test_council_bias_equiv.py 的 exhaustive 回归)。
    I1:旧函数 bias_recommendation 暂保留作等价对照,验收通过后再由 serialize 切换到本路径、并清理旧函数。
    """
    from tools.analysis import council
    return council.bias_council(tech, fundflow, sentiment)


# ---------- 消息面提示(第二步·保守版:只加一句话,绝不改任何预测数字)----------
def _sentiment_note(sentiment: dict | None, tech: dict | None = None) -> str | None:
    """保守版消息面提示:从 record 的 sentiment 块生成一句人话(看涨/看跌/中性 + 与技术面一致/背离 + 新鲜度)。

    红线:纯文本、仅供留意,**不改任何预测方向/数字**(情景预测/指标条件化/持有期/买卖倾向一律不动)。
    向后兼容:sentiment 缺失 / 样本数为 0 / 新鲜度=无数据 → 返回 None(不出提示)。
    方向阈值复用买卖倾向的 ±情绪阈值(_P['情绪偏多/偏空阈值']),不新造参数。
    与技术面比对用的是**纯技术趋势评级**(tech.signal.评级),避免与已并入情绪的买卖倾向循环。
    """
    if not sentiment:
        return None
    net = sentiment.get("净情绪分")
    n = sentiment.get("样本数") or 0
    if not isinstance(net, (int, float)) or n <= 0:
        return None
    fresh = sentiment.get("新鲜度")            # 新鲜/陈旧/无数据/None(旧记录无此字段)
    if fresh == "无数据":
        return None

    if net >= _P["情绪偏多阈值"]:
        mood, mdir = "看涨", "多"
    elif net <= _P["情绪偏空阈值"]:
        mood, mdir = "看跌", "空"
    else:
        mood, mdir = "中性", "中"

    cnt = f"共 {int(n)} 条"
    好, 坏 = sentiment.get("利好数"), sentiment.get("利空数")
    if isinstance(好, (int, float)) and isinstance(坏, (int, float)):
        cnt += f",利好 {int(好)}/利空 {int(坏)}"
    parts = [f"消息面{mood}(净情绪 {net:+.2f},{cnt})"]

    # 与纯技术趋势评级 一致/背离(仅在双方都有明确方向时说)
    rating = (tech.get("signal") or {}).get("评级") if tech else None
    tdir = {"偏多": "多", "偏空": "空"}.get(rating)   # 中性→None
    if tdir and mdir in ("多", "空"):
        parts.append("与技术面一致" if tdir == mdir
                     else f"与技术面({rating})背离,留意分歧")

    note = "、".join(parts) + "。仅供留意,不改预测。"
    if fresh == "陈旧":
        note += "(消息偏旧,仅参考)"
    return note


# ---------- L3:结构位 + 突破 + 情景锚定(纯数据,无 LLM)----------
def _anchor(price, atr_pct, S, R, rating, 突破, dist_sup, buf, near):
    """按 情景 锚定止损/止盈(数据验证结论:支撑压力作锚点、放量作突破确认)。返回(情景,止损,止盈,依据)。

    硬约束:止盈必须严格 > 现价、止损 < 现价(否则盈亏比无意义)。价已贴/触压力 → 判"待突破"。
    """
    band = (atr_pct / 100) if atr_pct == atr_pct else 0.03      # atr_pct 为 NaN 时退 3%
    S1 = S[0] if S else None
    R1 = R[0] if R else None
    R2 = R[1] if len(R) > 1 else None
    if S1 is None and R1 is None:
        return "数据不足(无结构位)", None, None, ["无 swing 支撑/压力,退回波动率括号"]
    if rating == "偏空" or 突破 == "放量跌破":
        return "跌破/下降(观望)", None, None, ["趋势偏空或放量跌破,不给多头目标"]

    def _tp_above(*prefer):
        """选严格高于现价(留 0.5% 间隔)的止盈锚:依次试 prefer,再 R2,最后量度目标。"""
        for c in (*prefer, R2):
            if c and c > price * 1.005:
                return round(c, 2)
        return round(price * (1 + 1.6 * band), 2)               # 量度目标(无上方压力可锚)

    sl_at = lambda lvl: round(lvl * (1 - buf), 2)
    if 突破 == "放量突破":
        return "放量突破上行", sl_at(S1 or price), _tp_above(R1), \
            ["放量突破前高:止损放突破位下方、止盈看上一档压力/量度目标"]
    if rating == "偏多" and S1 and dist_sup is not None and dist_sup <= near:
        return "趋势回踩", sl_at(S1), _tp_above(R1), ["上升趋势回踩近支撑:止损支撑下方、止盈看前高"]
    # 箱体 / 贴近压力
    sl = sl_at(S1) if S1 else round(price * (1 - band), 2)
    tp_pref = R1 * (1 - 0.3 * buf) if R1 else None              # 压力前留 0.3×缓冲
    if tp_pref and tp_pref > price * 1.005:
        return "箱体震荡", sl, round(tp_pref, 2), ["震荡区间:支撑下方止损、压力前止盈"]
    return "贴近压力(待突破)", sl, _tp_above(R2, R1), ["价已贴近压力:突破前不追、止盈看上一档压力/量度"]


def structure_anchor(kline: pd.DataFrame, price: float, atr_pct: float, sr: dict, tech: dict) -> dict:
    """结构位(支撑/压力/距离%/区间位置%/放量/突破)+ 情景化止盈止损锚定。全部纯数据,无 LLM。

    数据验证结论(L3 §4.1):放量=突破确认(降假突破)非收益预测;支撑压力=锚点非方向;τ=0.75~1%。
    """
    high, low, vol = kline["high"], kline["low"], kline["volume"]
    n = len(kline)
    S = sr.get("支撑位") or []
    R = sr.get("压力位") or []
    S1 = S[0] if S else None
    R1 = R[0] if R else None
    # 放量:当日量比(前 5 日均量,不含当日)
    vma = vol.iloc[-6:-1].mean() if n >= 6 else float("nan")
    vr = float(vol.iloc[-1] / vma) if (vma and not pd.isna(vma) and vma > 0) else float("nan")
    放量 = bool(vr >= _P["放量_量比"]) if not pd.isna(vr) else False
    # 突破:收盘超前 N 日高/低(带容差 + 放量确认)
    lb, tau = _P["突破回看"], _P["突破容差%"] / 100
    prior_hi = float(high.iloc[-(lb + 1):-1].max()) if n >= lb + 1 else float("nan")
    prior_lo = float(low.iloc[-(lb + 1):-1].min()) if n >= lb + 1 else float("nan")
    broke_up = (not pd.isna(prior_hi)) and price > prior_hi * (1 + tau)
    broke_dn = (not pd.isna(prior_lo)) and price < prior_lo * (1 - tau)
    突破 = ("放量突破" if 放量 else "疑似假突破(未放量)") if broke_up else \
        (("放量跌破" if 放量 else "跌破(未放量)") if broke_dn else "无")
    dist_sup = round((price - S1) / price * 100, 2) if S1 else None
    dist_res = round((R1 - price) / price * 100, 2) if R1 else None
    pos = round((price - S1) / (R1 - S1) * 100, 1) if (S1 and R1 and R1 > S1) else None
    rating = (tech.get("signal") or {}).get("评级")
    buf = max(_P["止损缓冲最小%"], _P["止损缓冲ATR倍数"] * atr_pct) / 100 if atr_pct == atr_pct else 0.01
    情景, sl, tp, why = _anchor(price, atr_pct, S, R, rating, 突破, dist_sup, buf, _P["贴近带%"])
    rr = round((tp - price) / (price - sl), 2) if (sl and tp and price > sl) else None
    # F2b:MA10/20/60 中位于现价下方者 = 候选止跌锚(动态支撑),按就近排序。
    # 仅作附加信息,不改上面已被 L3 调参的 swing 锚定 sl/tp;是否升级为主 sl 由回测(F6)定。
    ma_anchors = []
    _ma = tech.get("ma") or {}
    for _name in ("ma10", "ma20", "ma60"):
        _v = _ma.get(_name)
        if _v and _v < price:
            ma_anchors.append({"名称": _name.upper(), "价": round(float(_v), 2),
                               "距今%": round((price - _v) / price * 100, 2)})
    ma_anchors.sort(key=lambda x: x["距今%"])
    return {
        "支撑": [round(float(x), 2) for x in S[:2]], "压力": [round(float(x), 2) for x in R[:2]],
        "距支撑%": dist_sup, "距压力%": dist_res, "区间位置%": pos,
        "当日量比": round(vr, 2) if not pd.isna(vr) else None, "放量": 放量,
        "突破": 突破, "趋势": rating, "bias20": tech.get("bias20"),
        "均线支撑": ma_anchors,   # F2b:候选止跌锚(MA10/20/60 在现价下方者)
        "锚定": {"情景": 情景, "止损位": sl, "止盈位": tp, "盈亏比": rr, "依据": why},
    }


# ---------- 指标条件化预测(F3+F4)----------
def _conditional_block(kline: pd.DataFrame, tech: dict) -> dict:
    """指标条件化预测合并块:每 horizon = 方向/置信度(F4) + 条件化上涨概率/区间/期望/相似样本数/放宽层级(F3)。

    与无条件「情景预测」并列、供对照。池缺失/异常时 conditional_scenarios 内部优雅退回,绝不让 predict 崩。
    """
    try:
        from tools.analysis import conditional_predict as cpred
        idx = cpred.get_pool_index()               # 进程内缓存的索引(O(log n) 查询)
        as_of = kline["date"].iloc[-1]
        cond = cpred.conditional_scenarios(kline, tech, idx, as_of)
        dv = cpred.direction_view(cond)
        return {k: {**dv.get(k, {}), **cond[k]} for k in cond}
    except Exception:
        return {"error": "指标条件化预测暂不可用"}


# ---------- 汇总 ----------
def predict(kline: pd.DataFrame, tech: dict, fundflow: dict | None = None,
            sentiment: dict | None = None, with_conditional: bool = True) -> dict:
    """汇总预测/推荐。kline 需含 date/high/low/close/volume;tech=technical.compute 输出。

    sentiment 为 record 的 sentiment 块(可选),透传给买卖倾向作一维并入;None 时行为不变。
    with_conditional:是否附「指标条件化预测」块(F3+F4)。live/serialize 默认 True;
      旧两条线回测(backtest_predict)传 False 省去逐调用查池(F6 条件化 A/B 走独立高效路径)。
    """
    if kline is None or len(kline) < 30:
        return {"error": "数据不足", "n": 0 if kline is None else len(kline)}
    price = float(kline["close"].iloc[-1])
    atr_val = float(atr(kline).iloc[-1])
    atr_pct = atr_val / price * 100 if price else float("nan")
    sr = support_resistance(kline)

    out = {
        "现价": round(price, 2),
        "atr": round(atr_val, 3),
        "atr_pct": round(atr_pct, 2),
        "近三次放量": recent_volume_spikes(kline),
        **sr,
        "持有期建议": stop_targets(price, atr_pct),
        "结构位": structure_anchor(kline, price, atr_pct, sr, tech),  # L3:支撑压力/突破/情景锚定
        "情景预测": scenarios(kline),                       # 无条件历史频率(保留作对照)
        "买卖倾向": bias_recommendation(tech, fundflow, sentiment),
        "消息面提示": _sentiment_note(sentiment, tech),   # 保守版:纯文本提示,不改上面任何数字
        "免责": DISCLAIMER,
    }
    if with_conditional:
        out["指标条件化预测"] = _conditional_block(kline, tech)   # F3+F4:指标条件化(方向+区间+期望)
    return out
