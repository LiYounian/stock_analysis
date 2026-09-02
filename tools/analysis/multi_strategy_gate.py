"""多策略命中「同源信号闸门」(选股汇总层·统一一处 + kill-switch)。

需求:docs/每日分析/策略建议/多策略命中同源信号闸门.md(诊断 §2 + 提议 §3 + 验收 §4)。

命题:经验#8 把"≥2 策略同时命中"当"多维确认",但多个策略可能读**同源信号**(如都基于
价格涨幅:距高点/动量/5日反转),命中数越多只说明"涨得越猛"、不代表证据更强,反而是"过热"的度量;
且游资连板过热票(样本外靶子:海鸥住工 002084 复牌7连板 +95.54%,同源命中 3 策略却居首)不该
仅因命中数居首。本层在**选股汇总层**(不在各策略内重复)叠加三道闸:

  A 口径族计数(核心):把同源策略归族(config.STRATEGY_FAMILY),数"独立信号族数"而非
    "命中策略数"。002084(最大范围+动量+反转,均价格动量族)→ 独立口径命中数 = 1(而非 3);
    002811(量价放量[形态量能族] + 最大范围[价格动量族])→ 独立口径命中数 = 2(真·双维确认)。

  B 游资情绪过热前置闸:多轴(超买共振/涨幅透支/换手极端/事件博弈/基本面空心)**同时命中才裁决**
    (对齐动量抑制层"双轴命中才抑制",避免误杀正常强势股),默认软降级(保留展示+⚠+沉排序)。

  C 统一风控 veto 汇聚复用:直接消费 config.risk_veto_adjust(财报红旗 + 龙虎榜否决 OR 合成),
    对"多策略命中"排序做同向降权/否决 —— 避免"合议已否决、选股仍推荐"。

分层(与 reversal_veto / momentum_overheat 同型):
  · 纯函数 `overheat_verdict(features, c)` / `independent_hit_count(labels, c)`:吃预抽取输入,
    出裁决/计数;脱 IO、可单测。
  · IO 层 `extract_gate_features(code, as_of, ...)`:as-of 安全地从落盘取过热特征(复用
    momentum_overheat + reversal_veto 的特征抽取,不重造轮子);缺数据保守降级(不误裁决)。
  · 编排 `evaluate(picks_by_label, as_of, ...)`:对候选逐票计独立口径命中数 + 过热闸 + veto 汇聚,
    产出选股层裁决 + 分层排序键(feature_provider 可注入,供单测脱 IO)。
  · `sort_key(verdict)`:把裁决落到分层排序键 (tier, -独立口径命中数, -原始命中数);tier 优先于
    命中数,magnitude-robust —— 过热/否决票**无条件**排到 clean 多维确认票之后,同层保留命中数序。

防未来函数(硬红线):过热/龙虎榜/财报特征各自由被复用层按 date<=as_of / list_date<as_of 严格保证,
  本层只做纯变换与编排,不放松口径。⚠️ 非投资建议:闸门只改选股展示/入选排序,不构成买卖建议。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from tools.config import strategy as cfg_mod
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.multi_strategy_gate")

_CFG_KEY = "多策略命中闸门"

# 过热闸排序分层:tier 越小越靠前。clean=0(正常多维确认);软降级=1、沉底=2(保留展示,排 clean 后);
# 剔除/否决=3(调用方剔除后不入榜)。分层优先于命中数,magnitude-robust(见模块头)。
_TIER = {"软降级": 1, "沉底": 2, "剔除": 3}

# 过热闸空特征模板(缺数据保守降级:None/0/False → 该轴不发声,不误裁决)
_EMPTY_FEATURES = {
    "ob_os_verdict": None,      # momentum_overheat:超买/超卖/中性/None
    "ob_os_resonance": 0,       # 共振数
    "bias20": None,             # 距 MA20 乖离%
    "ret_n": None,              # 近 N 日累计涨幅%
    "ret_window": None,         # N(诊断用)
    "turnover": None,           # 当日换手率%(kline turnover 列;None=缺列/无数据)
    "lhb": None,                # 龙虎榜 as-of 裁决 dict(含 triggered);None=不发声
    "fund_high_flags": 0,       # 财报高危红旗剂量
    "扣非为负": False,          # 财报块 flags 含「扣非为负」
}


def cfg() -> dict:
    """读多策略命中闸门配置(缺失 → 空 dict → 关停默认,不炸)。单一真源。"""
    try:
        return (THRESHOLDS.get(_CFG_KEY, {}) or {})
    except Exception:                                      # noqa: BLE001
        return {}


def enabled(c: Optional[dict] = None) -> bool:
    """总开关(kill-switch)。关 → 全链路 no-op(退回原命中数排序,现状不回归)。"""
    c = c if c is not None else cfg()
    return bool(c.get("启用", False))


# ————————————————————————————————————————————————————————————————
# A、口径族计数(纯函数:命中策略名列表 → 独立口径命中数)
# ————————————————————————————————————————————————————————————————
def independent_hit_count(hit_labels, c: Optional[dict] = None) -> dict:
    """命中策略 view 名列表 → 独立口径命中数(同族只计 1 次)。纯函数。

    Args:
        hit_labels: 命中该票的策略 view 名列表(如 ["最大范围选股","动量组合","反转低换手组合"])。
        c: 闸门配置(测试可覆盖);读"非确认族"。
    Returns dict:
        原始命中数     —— 去重后命中的策略数(旧口径,= len(set(labels)));
        独立口径命中数 —— 计入确认的独立族数(排除"非确认族");
        命中族         —— {族名: [该族命中的策略名…]}(人读归因,含非确认族);
        非确认族命中   —— 命中但不计入确认的族名列表(如 状态参考族);
        未登记策略     —— STRATEGY_FAMILY 未登记的策略名(各自按独立族保守计,不误合并)。
    """
    c = c if c is not None else cfg()
    non_conf = set(c.get("非确认族", []) or [])
    labels = []
    seen = set()
    for x in (hit_labels or []):
        s = str(x)
        if s and s not in seen:
            seen.add(s)
            labels.append(s)

    fam_to_labels: dict[str, list[str]] = {}
    unregistered: list[str] = []
    for lab in labels:
        fam = cfg_mod.family_of(lab)
        if fam is None:
            # 未登记 → 按"独立族"保守计(用策略名占位一个唯一族,绝不误并入已有族)
            fam = f"未登记:{lab}"
            unregistered.append(lab)
        fam_to_labels.setdefault(fam, []).append(lab)

    confirm_fams = [f for f in fam_to_labels if f not in non_conf]
    non_conf_hit = [f for f in fam_to_labels if f in non_conf]

    return {
        "原始命中数": len(labels),
        "独立口径命中数": len(confirm_fams),
        "命中族": fam_to_labels,
        "非确认族命中": non_conf_hit,
        "未登记策略": unregistered,
    }


# ————————————————————————————————————————————————————————————————
# B、游资情绪过热前置闸(纯裁决:预抽取过热特征 → 多轴同时命中才裁决)
# ————————————————————————————————————————————————————————————————
def overheat_verdict(features: Optional[dict], c: Optional[dict] = None) -> dict:
    """多轴过热特征 → 游资情绪过热裁决。纯函数(同入同出,不触数据/网络)。

    "多轴同时命中才裁决"(默认最少命中轴数=2,对齐动量抑制层,避免误杀正常强势股)。

    Args:
        features: extract_gate_features 产出的特征 dict(见 _EMPTY_FEATURES);None/空 → 不触发。
        c: 闸门配置(测试可覆盖);读"过热闸"。
    Returns dict:
        应用/触发/动作(软降级/沉底/剔除)/命中轴数/降级系数/沉底/剔除/原因/轴。
    """
    c = c if c is not None else cfg()
    gate = (c.get("过热闸", {}) or {})
    f = dict(_EMPTY_FEATURES)
    if features:
        f.update({k: features.get(k, f[k]) for k in _EMPTY_FEATURES})

    on = bool(c.get("启用", False)) and bool(gate.get("启用", False))
    axes_cfg = gate.get("轴", {}) or {}

    def _axis_on(name: str) -> bool:
        return bool((axes_cfg.get(name, {}) or {}).get("启用", False))

    axes: dict[str, bool] = {"超买共振": False, "涨幅透支": False, "换手极端": False,
                             "事件博弈": False, "基本面空心": False}
    reasons: list[str] = []

    if on:
        # —— 轴1 超买共振 ——
        a = axes_cfg.get("超买共振", {}) or {}
        if _axis_on("超买共振"):
            thr = int(a.get("共振门槛", 2))
            reson = int(f.get("ob_os_resonance") or 0)
            if f.get("ob_os_verdict") == "超买" and reson >= thr:
                axes["超买共振"] = True
                reasons.append(f"超买共振(ob_os=超买/共振{reson}≥{thr})")

        # —— 轴2 涨幅透支 ——
        a = axes_cfg.get("涨幅透支", {}) or {}
        if _axis_on("涨幅透支"):
            bias_thr = a.get("bias20门槛")
            ret_thr = a.get("涨幅门槛%")
            bias, ret = f.get("bias20"), f.get("ret_n")
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

        # —— 轴3 换手极端 ——
        a = axes_cfg.get("换手极端", {}) or {}
        if _axis_on("换手极端"):
            turn_thr = a.get("换手门槛%")
            turn = f.get("turnover")
            if isinstance(turn, (int, float)) and turn_thr is not None and turn >= float(turn_thr):
                axes["换手极端"] = True
                reasons.append(f"换手极端({turn:.1f}%≥{turn_thr}%)")

        # —— 轴4 事件博弈(龙虎榜净买上榜)——
        a = axes_cfg.get("事件博弈", {}) or {}
        if _axis_on("事件博弈"):
            lhb = f.get("lhb") or {}
            if bool(lhb.get("triggered")):
                axes["事件博弈"] = True
                reasons.append("事件博弈(龙虎榜:" + str(lhb.get("reason") or "净买上榜") + ")")

        # —— 轴5 基本面空心 ——
        a = axes_cfg.get("基本面空心", {}) or {}
        if _axis_on("基本面空心"):
            dose_thr = int(a.get("高危红旗数门槛", 1))
            hit_dose = int(f.get("fund_high_flags") or 0) >= dose_thr
            hit_neg = bool(f.get("扣非为负"))
            if hit_dose or hit_neg:
                axes["基本面空心"] = True
                bits = []
                if hit_dose:
                    bits.append(f"高危红旗×{int(f.get('fund_high_flags') or 0)}")
                if hit_neg:
                    bits.append("扣非为负")
                reasons.append("基本面空心(" + "/".join(bits) + ")")

    n = sum(1 for v in axes.values() if v)
    min_axes = int(gate.get("最少命中轴数", 2))
    triggered = on and n >= max(1, min_axes)

    mode = gate.get("模式", "软降级")
    coef = float(gate.get("降级系数", 0.3)) if (triggered and mode == "软降级") else None
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


# ————————————————————————————————————————————————————————————————
# C、统一风控 veto 汇聚复用(薄封装 config.risk_veto_adjust;选股层同向消费)
# ————————————————————————————————————————————————————————————————
def veto_verdict(high_flag_count: int, lhb_verdict: Optional[dict],
                 c: Optional[dict] = None) -> dict:
    """选股层 veto 汇聚:直接复用 config.risk_veto_adjust(财报红旗 + 龙虎榜 OR 合成)。

    风控汇聚复用.启用=False → no-op(应用=False、否决=False)。base=None(选股层无打分区块,
    只用否决标记沉底,不引入外来分数);返回 risk_veto_adjust 原 dict 附"应用"总开关。
    """
    c = c if c is not None else cfg()
    reuse = bool(c.get("启用", False)) and bool((c.get("风控汇聚复用", {}) or {}).get("启用", False))
    if not reuse:
        return {"应用": False, "否决": False, "剔除": False, "罚分": 0.0, "归因": []}
    v = cfg_mod.risk_veto_adjust(None, high_flag_count, lhb_verdict)
    v["应用"] = bool(v.get("应用"))
    return v


# ————————————————————————————————————————————————————————————————
# 排序分层键(tier 优先于命中数,magnitude-robust)
# ————————————————————————————————————————————————————————————————
def sort_key(verdict: dict) -> tuple[int, int, int]:
    """选股裁决 → 分层排序键 (tier, -独立口径命中数, -原始命中数)。调用方按**升序**排即可。

      · clean(过热闸未触发 且 veto 未否决)→ tier 0:正常多维确认票,按独立口径命中数降序。
      · 过热软降级 → 1、沉底 → 2:排到 clean 之后,同层按命中数序。
      · 过热剔除 / veto 否决 → 3:调用方据「剔除」/「否决」标记直接移除或沉底。

    tier 优先保证:任意命中数,过热/否决票都排到 clean 多维确认票之后(magnitude-robust)。
    """
    oh = verdict.get("过热闸") or {}
    veto = verdict.get("veto") or {}
    indep = int(verdict.get("独立口径命中数") or 0)
    raw = int(verdict.get("原始命中数") or 0)
    tier = 0
    if bool(veto.get("否决")):
        tier = 3
    elif oh.get("触发"):
        tier = _TIER.get(oh.get("动作"), 1)
    return (tier, -indep, -raw)


# ————————————————————————————————————————————————————————————————
# IO 层:as-of 安全地抽取单票过热特征(复用现有抽取器,不重造轮子)
# ————————————————————————————————————————————————————————————————
def extract_gate_features(code: str, as_of: Optional[str], kline=None, ann=None,
                          name: Optional[str] = None, c: Optional[dict] = None) -> dict:
    """as-of 安全地抽取单票过热闸特征。各轴独立保守降级(缺数据 → 该轴不发声,不误裁决)。

    复用:momentum_overheat(超买共振/涨幅透支/bias/ret)+ reversal_veto(龙虎榜/基本面空心)+
    kline turnover 列(换手极端)。防未来函数由被复用层按 date<=as_of / list_date<as_of 各自保证。
    """
    c = c if c is not None else cfg()
    gate = (c.get("过热闸", {}) or {})
    axes_cfg = gate.get("轴", {}) or {}
    f = dict(_EMPTY_FEATURES)

    # —— 超买共振 + 涨幅透支(复用 momentum_overheat.extract_features;它内部读盘 + 切片)——
    if (axes_cfg.get("超买共振", {}) or {}).get("启用", False) or \
       (axes_cfg.get("涨幅透支", {}) or {}).get("启用", False) or \
       (axes_cfg.get("换手极端", {}) or {}).get("启用", False):
        try:
            from tools.strategy import momentum_overheat as _oh
            # 借用其涨幅窗口配置(与过热闸涨幅透支轴对齐)
            oh_cfg = {"轴": {"涨幅透支": axes_cfg.get("涨幅透支", {}) or {}}}
            mf = _oh.extract_features(code, as_of, kline=kline, c=oh_cfg)
            for k in ("ob_os_verdict", "ob_os_resonance", "bias20", "ret_n", "ret_window"):
                f[k] = mf.get(k, f[k])
            # 换手极端:取信号日 t(as-of 切片后最后一根)的 turnover
            f["turnover"] = _last_turnover(code, as_of, kline)
        except Exception as e:                            # noqa: BLE001
            logger.debug("过热价格/换手特征降级 %s @ %s: %s", code, as_of, str(e)[:80])

    # —— 事件博弈(龙虎榜)+ 基本面空心(复用 reversal_veto 的 as-of 抽取器)——
    try:
        from tools.strategy import reversal_veto as _rv
        if (axes_cfg.get("事件博弈", {}) or {}).get("启用", False):
            f["lhb"] = _rv._lhb_feature(code, as_of)
        if (axes_cfg.get("基本面空心", {}) or {}).get("启用", False):
            ff = _rv._fund_features(code, as_of)
            f["fund_high_flags"] = int(ff.get("fund_high_flags") or 0)
            f["扣非为负"] = bool(ff.get("扣非为负"))
    except Exception as e:                                # noqa: BLE001
        logger.debug("过热事件/基本面特征降级 %s @ %s: %s", code, as_of, str(e)[:80])

    return f


def _last_turnover(code: str, as_of: Optional[str], kline=None) -> Optional[float]:
    """信号日 t(date<=as_of 最后一根)的换手率%。缺列/无数据/异常 → None(换手轴不发声)。"""
    try:
        k = kline
        if k is None:
            from tools.collectors import market
            k = market.load_kline_recent(code)
        if k is None or "turnover" not in getattr(k, "columns", []):
            return None
        if as_of is not None and "date" in k.columns:
            ds = k["date"].astype(str).str.slice(0, 10)
            k = k[ds <= str(as_of)[:10]]
        if k is None or len(k) == 0:
            return None
        import math
        v = k["turnover"].iloc[-1]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        return round(float(v), 4)
    except Exception:                                     # noqa: BLE001
        return None


def _default_feature_provider(code: str, as_of: Optional[str], c: Optional[dict] = None):
    """默认特征供给:返回 (过热特征 dict, 高危红旗数, 龙虎榜裁决 dict)。评测/单测可注入替代。"""
    feats = extract_gate_features(code, as_of, c=c)
    return feats, int(feats.get("fund_high_flags") or 0), feats.get("lhb")


# ————————————————————————————————————————————————————————————————
# 编排:候选逐票 独立口径命中数 + 过热闸 + veto 汇聚 → 选股层裁决 + 分层排序
# ————————————————————————————————————————————————————————————————
def evaluate(picks_by_label: dict, as_of: Optional[str], c: Optional[dict] = None,
             feature_provider: Optional[Callable] = None) -> dict:
    """选股汇总层三闸编排。

    Args:
        picks_by_label: {策略 view 名: [该策略选出的 code…]}(run_screen_all 各 screener picks)。
        as_of: 信号日(防未来函数锚,透传给特征抽取器)。
        c: 闸门配置(测试可覆盖)。
        feature_provider: 可注入的特征供给 (code, as_of, c) -> (过热特征, 高危红旗数, 龙虎榜裁决);
                          缺省 = _default_feature_provider(真实读盘)。kill-switch 关时**完全不调用**。
    Returns dict:
        启用           —— 总开关;
        票             —— 逐票裁决 list(已按分层排序):
            {code, 命中策略, 命中族, 原始命中数, 独立口径命中数, 非确认族命中,
             过热闸(overheat_verdict 摘要), veto(汇聚裁决摘要), tier, 排序键}
        过热命中/否决   —— 命中过热闸 / 被 veto 否决的 code 列表(人读);
        说明           —— kill-switch 关时的 no-op 标注。
    """
    c = c if c is not None else cfg()
    on = enabled(c)

    # code → 命中的策略名列表(去重保序:同策略对同票重复选出只记一次)
    hits: dict[str, list[str]] = {}
    for label, picks in (picks_by_label or {}).items():
        lab = str(label)
        for code in dict.fromkeys(str(x) for x in (picks or []) if str(x)):
            lst = hits.setdefault(code, [])
            if lab not in lst:
                lst.append(lab)

    # kill-switch 关:退回"原命中数"排序(不过 B/C 闸、不读盘),现状不回归
    if not on:
        rows = []
        for code, labels in hits.items():
            raw = len(dict.fromkeys(labels))
            rows.append({"code": code, "命中策略": list(dict.fromkeys(labels)),
                         "原始命中数": raw, "独立口径命中数": raw,   # 关闸:独立数=原始数(不归族)
                         "命中族": {}, "非确认族命中": [],
                         "过热闸": {"触发": False, "应用": False},
                         "veto": {"应用": False, "否决": False},
                         "tier": 0, "排序键": (0, -raw, -raw)})
        rows.sort(key=lambda r: (0, -r["原始命中数"], -r["原始命中数"]))
        return {"启用": False, "票": rows, "过热命中": [], "否决": [],
                "说明": "多策略命中闸门 kill-switch 关闭:退回原命中数排序(no-op)"}

    fp = feature_provider or _default_feature_provider
    rows = []
    hot_codes: list[str] = []
    veto_codes: list[str] = []
    for code, labels in hits.items():
        hit = independent_hit_count(labels, c)
        feats, high_flags, lhb = fp(code, as_of, c)
        oh = overheat_verdict(feats, c)
        vv = veto_verdict(high_flags, lhb, c)
        verdict = {
            "code": code,
            "命中策略": list(dict.fromkeys(labels)),
            "命中族": hit["命中族"],
            "原始命中数": hit["原始命中数"],
            "独立口径命中数": hit["独立口径命中数"],
            "非确认族命中": hit["非确认族命中"],
            "未登记策略": hit["未登记策略"],
            "过热闸": {"触发": oh["触发"], "动作": oh["动作"], "命中轴数": oh["命中轴数"],
                     "剔除": oh["剔除"], "沉底": oh["沉底"], "原因": oh["原因"], "轴": oh["轴"]},
            "veto": {"应用": vv.get("应用", False), "否决": bool(vv.get("否决")),
                     "剔除": bool(vv.get("剔除")), "罚分": vv.get("罚分", 0.0),
                     "归因": vv.get("归因", [])},
        }
        verdict["排序键"] = sort_key(verdict)
        verdict["tier"] = verdict["排序键"][0]
        if oh["触发"]:
            hot_codes.append(code)
        if bool(vv.get("否决")):
            veto_codes.append(code)
        rows.append(verdict)

    rows.sort(key=lambda r: r["排序键"])
    return {"启用": True, "票": rows, "过热命中": hot_codes, "否决": veto_codes,
            "说明": "独立口径命中数替代命中数;游资过热多轴同命中软降级;风控 veto 汇聚同向消费"}
