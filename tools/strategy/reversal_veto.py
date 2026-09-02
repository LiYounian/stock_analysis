"""反转策略「基本面/消息面否决层」(策略10 专属·否决/降级)。

需求:docs/每日分析/策略建议/反转策略否决层.md(诊断+§3提议);实现方案:
docs/计划/反转策略否决层_实现方案.md。

命题:反转低换手为**纯量价**打分,技术超跌形态与「壳股/停复牌重组/游资博弈/基本面空心」重叠时
无法区分,高分假阳性(样本外靶子:中南文化 002445 反转0.83)。本层给反转高分票叠加**四轴风险否决**:
  1. 基本面空心:高危红旗剂量达门槛 / 扣非为负 / 扣非增速大幅下滑。
  2. 事件博弈:龙虎榜净买上榜 / 停复牌重组 / 交易异常波动(公告标题命中)。
  3. 治理风险:ST/*ST。
  4. 重组未完成:重组预期在途但未过会/未实施(存终止风险)。
命中任一轴 → 降级(综合分减罚分沉底、标"高风险博弈")或否决(剔除/强制沉底)。

分层(与 codebase 纯因子→record→薄管线一致):
  · 纯函数 `veto_verdict(features, cfg)`:吃**预抽取的四轴特征 dict**,出裁决;脱 IO、可单测。
  · IO 层 `extract_features(code, as_of, ann)`:as-of 安全地从落盘取风险特征;缺数据保守降级(不误否决)。
  · 便捷 `veto_asof(code, as_of)` = extract + verdict。

复用底层数据源,不重造轮子:
  · 基本面:tools.analysis.financial.analyzer.build_financial_block + flags.high_flag_count。
  · 龙虎榜:tools.analysis.risk_veto.lhb_verdict_asof(as-of 严格 list_date<as_of)。
  · 公告:tools.collectors.announcement.load_announcements(**本层自行按 date<=as_of 且窗口内过滤**)。
  · ST:config/code_name.json 名称标签("ST" in name.upper())。

防未来函数(硬红线):财报 disclosure_date<=as_of、龙虎榜 list_date<as_of、公告 date<=as_of 且窗口内;
  纯函数 veto_verdict 只吃入参、同入同出。⚠️ ST 名称为**当前快照**(无 as-of,状态粘性),回测中作近似,
  在 extract_features 里以 as_of_st_approx 标注,不假装 as-of 精确。
⚠️ 非投资建议:否决层只改选股展示/入选排序,不构成买卖建议。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("strategy.reversal_veto")

_CFG_KEY = "反转否决层"

# 空特征模板(缺数据保守降级:全 False/None → 不误否决)
_EMPTY_FEATURES = {
    "fund_high_flags": 0,       # 财报高危红旗剂量(严重度=高)
    "扣非为负": False,          # 财报块 flags 含「扣非为负」
    "扣非增速": None,           # 利润表摘要.扣非净利增速(%);None=无数据
    "lhb": None,                # 龙虎榜 as-of 裁决 dict(含 triggered);None=该轴不发声
    "is_st": False,             # ST/*ST(名称标签)
    "停复牌重组": False,        # 近窗停牌/复牌/重组类公告
    "异常波动": False,          # 近窗交易异常波动公告
    "重组未完成": False,        # 近窗重组在途且无落地词
    "event_hits": [],           # 命中公告标题(人读)
    "as_of_st_approx": False,   # ST 是否用了"当前名称近似"(无 as-of 精确)
}


def cfg() -> dict:
    """读反转否决层配置(缺失 → 空 dict → 关停默认,不炸)。单一真源。"""
    try:
        return (THRESHOLDS.get(_CFG_KEY, {}) or {})
    except Exception:                                      # noqa: BLE001
        return {}


def enabled(c: Optional[dict] = None) -> bool:
    """总开关(kill-switch)。关 → 全链路 no-op(纯量价现状不回归)。"""
    c = c if c is not None else cfg()
    return bool(c.get("启用", False))


# ————————————————————————————————————————————————————————————————
# 一、纯裁决函数(吃预抽取特征,出否决/降级裁决;脱 IO、可单测)
# ————————————————————————————————————————————————————————————————
def veto_verdict(features: Optional[dict], c: Optional[dict] = None) -> dict:
    """四轴风险特征 → 反转否决/降级裁决。纯函数(同入同出,不触数据/网络)。

    Args:
        features: extract_features 产出的特征 dict(见 _EMPTY_FEATURES);None/空 → 不触发。
        c: 反转否决层配置(测试可覆盖);缺省读 THRESHOLDS['反转否决层']。
    Returns dict:
        应用     —— 总开关是否开(未开→全 no-op);
        触发     —— 是否命中任一风险轴;
        动作     —— "降级"/"否决"(未触发→None);
        命中轴数 —— dose;罚分 —— 降级模式下从综合分扣的分(dose 单调、封顶);
        否决/剔除 —— 否决模式标记(剔除=否决且不保留展示);
        原因     —— list[str](人读归因);
        轴       —— {基本面空心/事件博弈/治理风险/重组未完成: bool}。
    """
    c = c if c is not None else cfg()
    f = dict(_EMPTY_FEATURES)
    if features:
        f.update({k: features.get(k, f[k]) for k in _EMPTY_FEATURES})

    on = bool(c.get("启用", False))
    axes_cfg = c.get("轴", {}) or {}

    def _axis_on(name: str) -> bool:
        return bool((axes_cfg.get(name, {}) or {}).get("启用", False))

    axes: dict[str, bool] = {"基本面空心": False, "事件博弈": False,
                             "治理风险": False, "重组未完成": False}
    reasons: list[str] = []

    if on:
        # —— 轴1 基本面空心 ——
        a1 = axes_cfg.get("基本面空心", {}) or {}
        if _axis_on("基本面空心"):
            dose_thr = int(a1.get("高危红旗数门槛", 1))
            drop_thr = a1.get("扣非大幅下滑%", -50.0)
            gr = f.get("扣非增速")
            hit_dose = int(f.get("fund_high_flags") or 0) >= dose_thr
            hit_neg = bool(f.get("扣非为负"))
            hit_drop = (isinstance(gr, (int, float)) and drop_thr is not None
                        and gr <= float(drop_thr))
            if hit_dose or hit_neg or hit_drop:
                axes["基本面空心"] = True
                bits = []
                if hit_dose:
                    bits.append(f"高危红旗×{int(f.get('fund_high_flags') or 0)}")
                if hit_neg:
                    bits.append("扣非为负")
                if hit_drop:
                    bits.append(f"扣非增速{gr:.0f}%")
                reasons.append("基本面空心(" + "/".join(bits) + ")")

        # —— 轴2 事件博弈 ——
        if _axis_on("事件博弈"):
            lhb = f.get("lhb") or {}
            lhb_trig = bool(lhb.get("triggered"))
            halt = bool(f.get("停复牌重组"))
            abn = bool(f.get("异常波动"))
            if lhb_trig or halt or abn:
                axes["事件博弈"] = True
                bits = []
                if lhb_trig:
                    bits.append("龙虎榜:" + str(lhb.get("reason") or "净买上榜"))
                if halt:
                    bits.append("停复牌/重组公告")
                if abn:
                    bits.append("交易异常波动")
                reasons.append("事件博弈(" + "/".join(bits) + ")")

        # —— 轴3 治理风险 ——
        if _axis_on("治理风险") and bool(f.get("is_st")):
            axes["治理风险"] = True
            reasons.append("治理风险(ST/*ST)")

        # —— 轴4 重组未完成 ——
        if _axis_on("重组未完成") and bool(f.get("重组未完成")):
            axes["重组未完成"] = True
            reasons.append("重组未完成(在途未落地,存终止风险)")

    n = sum(1 for v in axes.values() if v)
    triggered = on and n > 0

    per = float(c.get("每轴罚分", 1.5))
    cap = float(c.get("罚分上限", 4.0))
    penalty = min(per * n, cap) if triggered else 0.0

    mode = c.get("模式", "降级")
    is_veto = bool(triggered and mode == "否决")
    is_drop = bool(is_veto and not c.get("否决沉底保留展示", True))

    return {
        "应用": on,
        "触发": triggered,
        "动作": (mode if triggered else None),
        "命中轴数": n,
        "罚分": round(penalty, 4),
        "否决": is_veto,
        "剔除": is_drop,
        "原因": reasons,
        "轴": axes,
    }


def apply_to_score(base_score, verdict: dict) -> float:
    """把裁决落到综合排序分。降级=base−罚分;否决=强制沉底(base−∞级罚分,但仍返回有限值);
    未触发=base。base=None → 视为 0。纯函数,符号安全(减正罚分对任意符号单调下沉)。

    否决(强制沉底)取一个足够低的常量偏移,确保被否决票排到所有正常票之后。
    """
    base = float(base_score) if isinstance(base_score, (int, float)) and not isinstance(
        base_score, bool) else 0.0
    if not verdict or not verdict.get("触发"):
        return base
    if verdict.get("否决"):
        return base - 1e6                                  # 强制沉底(仍展示,靠 code 顺序稳定)
    return base - float(verdict.get("罚分") or 0.0)         # 降级


# ————————————————————————————————————————————————————————————————
# 二、as-of 特征抽取(IO 层;缺数据保守降级,不误否决)
# ————————————————————————————————————————————————————————————————
_NAMES_CACHE: Optional[dict] = None


def _names() -> dict:
    """code→name 映射(config/code_name.json)。缺失/异常 → 空 dict(ST 轴降级不发声)。"""
    global _NAMES_CACHE
    if _NAMES_CACHE is None:
        try:
            from tools.config import settings
            p = Path(settings.PROJECT_ROOT) / "config" / "code_name.json"
            _NAMES_CACHE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception as e:                             # noqa: BLE001
            logger.debug("code_name.json 读取失败,ST 轴降级: %s", str(e)[:80])
            _NAMES_CACHE = {}
    return _NAMES_CACHE


def is_st_name(code: str, name: Optional[str] = None) -> bool:
    """ST/*ST 判定(名称标签)。name 缺省从 code_name.json 查。⚠️ 当前快照、无 as-of。"""
    nm = name if name is not None else _names().get(str(code))
    return "ST" in (nm or "").replace(" ", "").upper()


def _fund_features(code: str, as_of: Optional[str]) -> dict:
    """基本面特征:高危红旗剂量 + 扣非为负 + 扣非增速(as-of:disclosure_date<=as_of)。

    缺数据/异常 → 全默认(0/False/None),该轴不触发(保守,不误否决)。
    """
    out = {"fund_high_flags": 0, "扣非为负": False, "扣非增速": None}
    try:
        from tools.analysis.financial import analyzer as fin_an
        from tools.analysis.financial import flags as fin_flags
        block = fin_an.build_financial_block(code, as_of=as_of)
        if not block:
            return out
        out["fund_high_flags"] = int(fin_flags.high_flag_count(block) or 0)
        names = [str(x) for x in (block.get("flags") or [])]
        out["扣非为负"] = "扣非为负" in names
        digest = block.get("利润表摘要") or {}
        gr = digest.get("扣非净利增速")
        if isinstance(gr, (int, float)):
            out["扣非增速"] = float(gr)
    except FileNotFoundError:
        pass                                               # 无财报快照 → 该轴降级
    except Exception as e:                                 # noqa: BLE001
        logger.debug("基本面特征降级 %s @ %s: %s", code, as_of, str(e)[:80])
    return out


def _lhb_feature(code: str, as_of: Optional[str]) -> Optional[dict]:
    """龙虎榜 as-of 裁决(复用 analysis.risk_veto)。缺失/异常/无 as_of → None(该轴不发声)。"""
    if not as_of:
        return None
    try:
        from tools.analysis import risk_veto as rv
        return rv.lhb_verdict_asof(code, as_of)
    except Exception as e:                                 # noqa: BLE001
        logger.debug("龙虎榜特征降级 %s @ %s: %s", code, as_of, str(e)[:80])
        return None


def _event_features(code: str, as_of: Optional[str], ann=None, c: Optional[dict] = None) -> dict:
    """公告事件特征:停复牌/重组 + 交易异常波动 + 重组未完成(标题关键词;date<=as_of 且窗口内)。

    ann:可预传公告列表(避免重复读盘);None → load_announcements(code)。
    无未来函数:只用 date<=as_of 且 (as_of-date).days ≤ 窗口 的公告标题。
    缺数据/异常 → 全 False(该轴不触发)。
    """
    c = c if c is not None else cfg()
    out = {"停复牌重组": False, "异常波动": False, "重组未完成": False, "event_hits": []}
    try:
        items = ann
        if items is None:
            from tools.collectors import announcement as ann_mod
            items = ann_mod.load_announcements(code)
        if not items:
            return out
    except FileNotFoundError:
        return out
    except Exception as e:                                 # noqa: BLE001
        logger.debug("公告特征降级 %s @ %s: %s", code, as_of, str(e)[:80])
        return out

    import pandas as pd
    asof_ts = pd.Timestamp(as_of) if as_of else None

    kw_halt = c.get("停复牌重组关键词", []) or []
    kw_abn = c.get("异常波动关键词", []) or []
    kw_reorg = c.get("重组在途关键词", []) or []
    kw_done = c.get("重组落地关键词", []) or []
    axes_cfg = c.get("轴", {}) or {}
    win_event = int((axes_cfg.get("事件博弈", {}) or {}).get("窗口天数", 30))
    win_reorg = int((axes_cfg.get("重组未完成", {}) or {}).get("窗口天数", 90))

    reorg_in_progress = False
    reorg_done = False
    hits: list[str] = []
    for it in items:
        title = str((it or {}).get("title") or "")
        d = str((it or {}).get("date") or "")[:10]
        if not title or not d:
            continue
        try:
            dts = pd.Timestamp(d)
        except Exception:                                  # noqa: BLE001
            continue
        if asof_ts is not None:
            dd = (asof_ts - dts).days
            if dd < 0:                                      # date>as_of:未来公告,剔(防未来函数)
                continue
        else:
            dd = 0

        # 事件博弈窗:停复牌/重组类 + 异常波动
        if dd <= win_event:
            if any(k in title for k in kw_halt):
                out["停复牌重组"] = True
                hits.append(title)
            if any(k in title for k in kw_abn):
                out["异常波动"] = True
                hits.append(title)
        # 重组未完成窗(通常更长):记在途 / 落地
        if dd <= win_reorg:
            if any(k in title for k in kw_reorg):
                reorg_in_progress = True
                hits.append(title)
            if any(k in title for k in kw_done):
                reorg_done = True

    # 重组在途且窗口内无落地词 → 未完成(存终止风险)
    out["重组未完成"] = bool(reorg_in_progress and not reorg_done)
    # 去重保序
    seen = set()
    out["event_hits"] = [t for t in hits if not (t in seen or seen.add(t))][:12]
    return out


def extract_features(code: str, as_of: Optional[str], ann=None,
                     name: Optional[str] = None, c: Optional[dict] = None) -> dict:
    """as-of 安全地抽取单票四轴风险特征。缺数据/异常各轴独立保守降级(不误否决)。

    Args:
        code: 6 位代码;as_of: 信号日(防未来函数锚);ann: 可预传公告列表;
        name: 可预传名称(免查 code_name.json);c: 配置覆盖(测试用)。
    Returns: 见 _EMPTY_FEATURES 的特征 dict。
    """
    c = c if c is not None else cfg()
    f = dict(_EMPTY_FEATURES)
    axes_cfg = c.get("轴", {}) or {}

    if (axes_cfg.get("基本面空心", {}) or {}).get("启用", False):
        f.update(_fund_features(code, as_of))
    if (axes_cfg.get("事件博弈", {}) or {}).get("启用", False):
        f["lhb"] = _lhb_feature(code, as_of)
        f.update({k: v for k, v in _event_features(code, as_of, ann=ann, c=c).items()
                  if k in ("停复牌重组", "异常波动", "event_hits")})
    if (axes_cfg.get("治理风险", {}) or {}).get("启用", False):
        f["is_st"] = is_st_name(code, name)
        f["as_of_st_approx"] = True                        # 诚实标注:ST 用当前名称近似
    if (axes_cfg.get("重组未完成", {}) or {}).get("启用", False):
        ev = _event_features(code, as_of, ann=ann, c=c)
        f["重组未完成"] = ev["重组未完成"]
        if ev["event_hits"]:
            merged = list(f.get("event_hits") or []) + ev["event_hits"]
            seen = set()
            f["event_hits"] = [t for t in merged if not (t in seen or seen.add(t))][:12]
    return f


def veto_asof(code: str, as_of: Optional[str], ann=None,
              name: Optional[str] = None, c: Optional[dict] = None) -> dict:
    """便捷:extract_features + veto_verdict。返回裁决 dict(附 features 供审计)。"""
    c = c if c is not None else cfg()
    if not enabled(c):
        v = veto_verdict(None, c)
        v["features"] = None
        return v
    feats = extract_features(code, as_of, ann=ann, name=name, c=c)
    v = veto_verdict(feats, c)
    v["features"] = feats
    return v
