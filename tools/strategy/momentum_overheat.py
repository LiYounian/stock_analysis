"""动量策略「高位超买抑制层」(策略4·动量组合 专属·软降级/沉底/剔除)。

需求:docs/每日分析/策略建议/动量策略高位超买抑制层.md(诊断 §1-2 + 提议 §3 + 回测口径 §4)。

命题:动量组合(screen_momentum,纯价格动量)对「动量越强分越高」,但动量最强处常是超买见顶区;
「高位超买 + 涨幅透支」时追在动量末端(样本外靶子:中石科技 300684 bias20 +27.5/RSI6 78/KDJ J≈90
+ 利好出尽,入选后连续两日大幅跑输板块)。本层给动量高分票叠加**双轴风险抑制**:
  轴1 超买共振:复用 technical.ob_os 的多指标(KDJ/RSI/BIAS20)共振裁决,verdict=超买 且 共振数≥门槛。
  轴2 涨幅透支:近 N 日累计涨幅 ≥ 门槛 或 距 MA20 乖离(bias20) ≥ 门槛(超买极端档)。
需**同时**命中「最少命中轴数」个轴(默认 2 = 双轴同时,避免误杀健康强票)→ 软降级/沉底/剔除。

分层(与 reversal_veto 同型,与 codebase 纯因子→薄管线一致):
  · 纯函数 `overheat_verdict(features, c)`:吃**预抽取的双轴特征 dict**,出裁决;脱 IO、可单测。
  · IO 层 `extract_features(code, as_of, kline)`:as-of 安全地从落盘 K 线算超买超卖 + 涨幅特征;
    缺数据保守降级(不误抑制)。
  · 便捷 `overheat_asof(code, as_of)` = extract + verdict。
  · `sort_key(base, verdict)`:把裁决落到**分层排序键** (tier, base);调用方按 tier 升序、base 降序
    排即可。分层(而非算术罚分)是**magnitude-robust** 的关键:动量分是无界比率(急拉票年化可达
    e^(slope·250) 量级),任何固定乘/减罚分都压不住极端 blow-off;分层让抑制票**无条件**排到未命中
    票之后,同层内保留相对序。tier:0 未命中/clean,1 软降级,2 沉底,3 剔除(调用方剔除后不入榜)。

复用底层数据源,不重造轮子:
  · 超买超卖:tools.analysis.technical 的 kdj/rsi/ma + `_overbought_oversold`(ob_os 共振裁决)。

防未来函数(硬红线):抑制特征只用信号日 t 及之前 K 线;extract_features 按 date<=as_of 严格切片;
  纯函数 overheat_verdict 只吃入参、同入同出。
⚠️ 非投资建议:抑制层只改选股展示/入选排序,不构成买卖建议。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("strategy.momentum_overheat")

_CFG_KEY = "动量高位超买抑制"

# 排序分层:tier 越小越靠前。未命中=0(与纯动量同层);软降级=1、沉底=2(均保留展示,排到 clean 之后);
# 剔除=3(调用方剔除后不入榜)。分层优先于分值,magnitude-robust(见模块头)。
_TIER = {"软降级": 1, "沉底": 2, "剔除": 3}

# 空特征模板(缺数据保守降级:verdict=None / 数值 None → 不误抑制)
_EMPTY_FEATURES = {
    "ob_os_verdict": None,      # technical.ob_os 裁决:超买/超卖/中性/None
    "ob_os_resonance": 0,       # 共振数(命中同方向的指标个数)
    "bias20": None,             # 距 MA20 乖离%(None=无数据)
    "ret_n": None,              # 近 N 日累计涨幅%(None=无数据)
    "ret_window": None,         # N(诊断用)
}


def cfg() -> dict:
    """读高位超买抑制配置(缺失 → 空 dict → 关停默认,不炸)。单一真源。"""
    try:
        return (THRESHOLDS.get(_CFG_KEY, {}) or {})
    except Exception:                                      # noqa: BLE001
        return {}


def enabled(c: Optional[dict] = None) -> bool:
    """总开关(kill-switch)。关 → 全链路 no-op(纯动量现状不回归)。"""
    c = c if c is not None else cfg()
    return bool(c.get("启用", False))


# ————————————————————————————————————————————————————————————————
# 一、纯裁决函数(吃预抽取特征,出抑制裁决;脱 IO、可单测)
# ————————————————————————————————————————————————————————————————
def overheat_verdict(features: Optional[dict], c: Optional[dict] = None) -> dict:
    """双轴超买/涨幅透支特征 → 抑制裁决。纯函数(同入同出,不触数据/网络)。

    Args:
        features: extract_features 产出的特征 dict(见 _EMPTY_FEATURES);None/空 → 不触发。
        c: 抑制层配置(测试可覆盖);缺省读 THRESHOLDS['动量高位超买抑制']。
    Returns dict:
        应用     —— 总开关是否开(未开→全 no-op);
        触发     —— 是否命中「最少命中轴数」个风险轴;
        动作     —— "软降级"/"沉底"/"剔除"(未触发→None);
        命中轴数 —— dose;降级系数 —— 软降级模式的乘数(其余模式为 None);
        沉底/剔除 —— 硬抑制标记(剔除=从入选清单移除;沉底=强制排到末尾但保留展示);
        原因     —— list[str](人读归因);
        轴       —— {超买共振/涨幅透支: bool}。
    """
    c = c if c is not None else cfg()
    f = dict(_EMPTY_FEATURES)
    if features:
        f.update({k: features.get(k, f[k]) for k in _EMPTY_FEATURES})

    on = bool(c.get("启用", False))
    axes_cfg = c.get("轴", {}) or {}

    def _axis_on(name: str) -> bool:
        return bool((axes_cfg.get(name, {}) or {}).get("启用", False))

    axes: dict[str, bool] = {"超买共振": False, "涨幅透支": False}
    reasons: list[str] = []

    if on:
        # —— 轴1 超买共振(复用 ob_os 共振裁决)——
        a1 = axes_cfg.get("超买共振", {}) or {}
        if _axis_on("超买共振"):
            reson_thr = int(a1.get("共振门槛", 2))
            verdict = f.get("ob_os_verdict")
            reson = int(f.get("ob_os_resonance") or 0)
            if verdict == "超买" and reson >= reson_thr:
                axes["超买共振"] = True
                reasons.append(f"超买共振(ob_os=超买/共振{reson}≥{reson_thr})")

        # —— 轴2 涨幅透支(bias20 极端 或 近 N 日累计涨幅超阈)——
        a2 = axes_cfg.get("涨幅透支", {}) or {}
        if _axis_on("涨幅透支"):
            bias_thr = a2.get("bias20门槛")
            ret_thr = a2.get("涨幅门槛%")
            bias = f.get("bias20")
            ret = f.get("ret_n")
            hit_bias = (isinstance(bias, (int, float)) and bias_thr is not None
                        and bias >= float(bias_thr))
            hit_ret = (isinstance(ret, (int, float)) and ret_thr is not None
                       and ret >= float(ret_thr))
            if hit_bias or hit_ret:
                axes["涨幅透支"] = True
                bits = []
                if hit_bias:
                    bits.append(f"bias20={bias:.1f}≥{bias_thr}")
                if hit_ret:
                    bits.append(f"近{f.get('ret_window')}日涨{ret:.1f}%≥{ret_thr}%")
                reasons.append("涨幅透支(" + "/".join(bits) + ")")

    n = sum(1 for v in axes.values() if v)
    min_axes = int(c.get("最少命中轴数", 2))
    triggered = on and n >= max(1, min_axes)

    mode = c.get("模式", "软降级")
    coef = float(c.get("降级系数", 0.3)) if (triggered and mode == "软降级") else None
    is_sink = bool(triggered and mode == "沉底")
    is_drop = bool(triggered and mode == "剔除")

    return {
        "应用": on,
        "触发": triggered,
        "动作": (mode if triggered else None),
        "命中轴数": n,
        "降级系数": coef,
        "沉底": is_sink,
        "剔除": is_drop,
        "原因": reasons,
        "轴": axes,
    }


def sort_key(base_score, verdict: dict) -> tuple[int, float]:
    """裁决 → 分层排序键 (tier, base)。调用方按 **tier 升序、base 降序** 排序即可
    (`sorted(items, key=lambda x: (tier, -base))`)。

      · 未触发 / 无裁决  → (0, base):与纯动量同层,顺序不变(向后兼容、无回归)。
      · 软降级           → (1, base):排到全部 clean 票之后,同层保留相对动量序。
      · 沉底             → (2, base):排到软降级票之后。
      · 剔除             → (3, base):调用方应据「剔除」标记直接移除,不入榜。

    分层优先保证:任意量级的动量分,抑制票都排到未命中票之后(magnitude-robust)。
    base=None → 视为 -inf(排最后)。纯函数,不触数据/网络。
    """
    base = float(base_score) if isinstance(base_score, (int, float)) and not isinstance(
        base_score, bool) else float("-inf")
    if not verdict or not verdict.get("触发"):
        return (0, base)
    tier = _TIER.get(verdict.get("动作"), 1)
    return (tier, base)


# ————————————————————————————————————————————————————————————————
# 二、as-of 特征抽取(IO 层;缺数据保守降级,不误抑制)
# ————————————————————————————————————————————————————————————————
def _slice_asof(kline, as_of: Optional[str]):
    """把 K 线严格切到 date<=as_of(防未来函数);无 date 列/无 as_of → 原样返回。"""
    if kline is None or as_of is None:
        return kline
    try:
        if "date" in kline.columns:
            ds = kline["date"].astype(str).str.slice(0, 10)
            return kline[ds <= str(as_of)[:10]]
    except Exception:                                     # noqa: BLE001
        return kline
    return kline


def extract_features(code: str, as_of: Optional[str], kline=None,
                     c: Optional[dict] = None) -> dict:
    """as-of 安全地抽取单票双轴超买/涨幅特征。缺数据/异常 → 保守空特征(不误抑制)。

    Args:
        code: 6 位代码;as_of: 信号日(防未来函数锚);kline: 可预传 OHLC(免重复读盘);
        c: 配置覆盖(测试用,读涨幅窗口)。
    Returns: 见 _EMPTY_FEATURES 的特征 dict。
    """
    c = c if c is not None else cfg()
    f = dict(_EMPTY_FEATURES)
    ret_win = int(((c.get("轴", {}) or {}).get("涨幅透支", {}) or {}).get("涨幅窗口", 10))
    f["ret_window"] = ret_win
    try:
        import numpy as np

        from tools.analysis import technical as ta
        if kline is None:
            from tools.collectors import market
            kline = market.load_kline_recent(code)
        kline = _slice_asof(kline, as_of)
        if kline is None or len(kline) < 25 or "close" not in kline.columns:
            return f
        close = kline["close"]
        # 超买共振(复用 ob_os 共振裁决):KDJ 需 high/low,RSI/BIAS 需 close
        kd = ta.kdj(kline) if {"high", "low"}.issubset(kline.columns) else None
        k = float(kd.iloc[-1]["k"]) if kd is not None else np.nan
        j = float(kd.iloc[-1]["j"]) if kd is not None else np.nan
        rsi12 = float(ta.rsi(close, 12).iloc[-1])
        ma20 = float(ta.ma(close, 20).iloc[-1])
        last = float(close.iloc[-1])
        bias20 = ((last - ma20) / ma20 * 100.0) if (ma20 and not np.isnan(ma20)) else np.nan
        ob = ta._overbought_oversold(k, j, rsi12, bias20)
        f["ob_os_verdict"] = ob.get("verdict")
        f["ob_os_resonance"] = int(ob.get("resonance") or 0)
        f["bias20"] = None if np.isnan(bias20) else round(float(bias20), 4)
        if len(close) >= ret_win + 1:
            base = float(close.iloc[-1 - ret_win])
            if base > 0:
                f["ret_n"] = round(float(last / base - 1.0) * 100.0, 4)
    except FileNotFoundError:
        pass                                              # 无 K 线 → 保守空特征
    except Exception as e:                                # noqa: BLE001
        logger.debug("高位超买特征降级 %s @ %s: %s", code, as_of, str(e)[:80])
    return f


def overheat_asof(code: str, as_of: Optional[str], kline=None,
                  c: Optional[dict] = None) -> dict:
    """便捷:extract_features + overheat_verdict。返回裁决 dict(附 features 供审计)。

    kill-switch 关 → 直接返回未触发裁决(不读盘、不算特征,纯动量 no-op)。
    """
    c = c if c is not None else cfg()
    if not enabled(c):
        v = overheat_verdict(None, c)
        v["features"] = None
        return v
    feats = extract_features(code, as_of, kline=kline, c=c)
    v = overheat_verdict(feats, c)
    v["features"] = feats
    return v
