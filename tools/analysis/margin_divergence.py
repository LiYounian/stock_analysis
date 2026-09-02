"""资金流「主力净流入 vs 融资买入」背离甄别(治本核心)。

需求:docs/每日分析/策略建议/资金流融资盘甄别.md §3.1(治本)+ §4(回测口径)。
命题:东财「主力净流入」按成交额/手数对已成交逐笔分类反推,**无法剔除融资盘**。当某票
「主力净流入」为大额正值、但当日**融资买入额**能解释掉其大部分时,该「流入」更可能是
杠杆资金追高、而非主力吸筹 → 应对资金流看多信号**降权 / 翻风险提示**(样本外靶子:
新易盛300502「20亿」实为 16-17 亿融资买入撑起)。

分层(与 codebase 纯函数→IO→便捷 一致,照 strategy.reversal_veto 模式):
  · 纯函数 `divergence_verdict(net_inflow, margin_rec, ...)`:吃预取特征,出裁决;脱 IO、可单测。
  · IO 层 `load_margin_asof(code, as_of)`:as-of 安全地取 ≤as_of 的最新一日两融记录;缺数据→None。
  · 便捷 `divergence_asof(code, as_of, net_inflow, ...)` = load + verdict(恒不抛,缺数据保守不触发)。

防未来函数(硬红线):两融明细为盘后披露,只取 **date ≤ as_of** 的记录(collectors.margin.summarize_asof
保证);纯函数只吃入参、同入同出。缺数据 → 不触发(保守:绝不无中生有地压制正常资金流信号)。
kill-switch:config「资金流融资盘甄别.启用」=False → enabled()=False → 全链路 no-op(资金流现状不回归)。
⚠️ 非投资建议:甄别层只改资金流信号强度/方向,不构成买卖建议。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.margin_divergence")

_CFG_KEY = "资金流融资盘甄别"


def cfg() -> dict:
    """读甄别配置(缺失 → 空 dict → 关停默认,不炸)。单一真源。"""
    try:
        return (THRESHOLDS.get(_CFG_KEY, {}) or {})
    except Exception:                                      # noqa: BLE001
        return {}


def enabled(c: Optional[dict] = None) -> bool:
    """总开关(kill-switch)。关 → 全链路 no-op(资金流纯符号现状不回归)。"""
    c = c if c is not None else cfg()
    return bool(c.get("启用", False))


def _f(v):
    try:
        if v is None or (isinstance(v, bool)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ————————————————————————————————————————————————————————————————
# 一、纯裁决函数(吃预取特征,出背离裁决;脱 IO、可单测)
# ————————————————————————————————————————————————————————————————
def divergence_verdict(net_inflow, margin_rec: Optional[dict],
                       turnover=None, float_mv=None, c: Optional[dict] = None) -> dict:
    """「主力净流入 vs 融资买入」背离裁决。纯函数(同入同出,不触数据/网络)。

    Args:
        net_inflow: 今日主力净流入(元);来自 record['fundflow']['今日主力净流入']。
        margin_rec: 当日(≤as_of)两融记录 dict(含「融资买入额」/「融资余额」);None=无两融数据。
        turnover:   当日成交额(元,可选);算「融资买入/成交额」占比(仅信息档位)。
        float_mv:   流通市值(元,可选);算「融资余额/流通市值」占比(仅信息档位)。
        c:          配置覆盖(测试用);缺省读 THRESHOLDS['资金流融资盘甄别']。
    Returns dict:
        应用       —— 总开关是否开;
        命中       —— 是否判「主力流入疑似融资盘驱动」;
        动作       —— "降级"/"翻风险"(未命中→None);
        强度系数   —— 命中且降级时资金流看多强度应乘的系数(未命中=1.0);
        融资买入额 / 主力净流入 / 融资解释比 —— 判据数值(人读/审计);
        融资占成交额 / 融资余额占流通市值 —— 信息档位(缺 turnover/float_mv → None);
        高两融占比 —— bool(任一档位达高档;仅信息标注);
        依据       —— list[str](人读归因)。

    判据(核心):net_inflow 为大额正值(≥ 最小主力净流入门槛)且
      融资解释比 = 融资买入额 / net_inflow ≥ 融资解释比阈值 → 命中。
    保守:net_inflow 非正 / 无 margin_rec / 无融资买入额 → 不命中(不误压制)。
    """
    c = c if c is not None else cfg()
    on = bool(c.get("启用", False))

    net = _f(net_inflow)
    rz_buy = _f((margin_rec or {}).get("融资买入额")) if margin_rec else None
    rz_bal = _f((margin_rec or {}).get("融资余额")) if margin_rec else None
    tv = _f(turnover)
    fmv = _f(float_mv)

    min_net = float(c.get("最小主力净流入_元", 5e7))
    ratio_thr = float(c.get("融资解释比阈值", 0.5))
    action = c.get("动作", "降级")
    down_k = float(c.get("降级系数", 0.3))
    hi_tv = float(c.get("融资占成交额_高档", 0.15))
    hi_mv = float(c.get("融资余额占流通市值_高档", 0.04))

    # —— 信息档位(与命中判据独立;缺分母 → None)——
    rz_over_tv = (rz_buy / tv) if (rz_buy is not None and tv and tv > 0) else None
    rz_over_mv = (rz_bal / fmv) if (rz_bal is not None and fmv and fmv > 0) else None
    high_margin = bool((rz_over_tv is not None and rz_over_tv >= hi_tv)
                       or (rz_over_mv is not None and rz_over_mv >= hi_mv))

    # —— 融资解释比 = 融资买入额 / 主力净流入(仅在 net>0 时有意义)——
    explain_ratio = None
    hit = False
    reasons: list[str] = []
    if on and net is not None and net > 0 and rz_buy is not None and rz_buy >= 0:
        explain_ratio = rz_buy / net if net > 0 else None
        if net >= min_net and explain_ratio is not None and explain_ratio >= ratio_thr:
            hit = True
            reasons.append(
                f"⚠疑似融资盘驱动:融资买入{rz_buy/1e8:.2f}亿 / 主力净流入{net/1e8:.2f}亿 "
                f"= {explain_ratio*100:.0f}%(≥{ratio_thr*100:.0f}%)")
            if high_margin:
                bits = []
                if rz_over_tv is not None:
                    bits.append(f"融资占成交额{rz_over_tv*100:.0f}%")
                if rz_over_mv is not None:
                    bits.append(f"融资余额占流通市值{rz_over_mv*100:.1f}%")
                if bits:
                    reasons.append("高两融占比(" + "/".join(bits) + ")")

    强度系数 = 1.0
    动作 = None
    if hit:
        动作 = action
        强度系数 = down_k if action == "降级" else 0.0   # 翻风险 → 强度归0(方向由消费侧翻中性)

    return {
        "应用": on,
        "命中": hit,
        "动作": 动作,
        "强度系数": 强度系数,
        "融资买入额": rz_buy,
        "主力净流入": net,
        "融资解释比": round(explain_ratio, 4) if explain_ratio is not None else None,
        "融资占成交额": round(rz_over_tv, 4) if rz_over_tv is not None else None,
        "融资余额占流通市值": round(rz_over_mv, 4) if rz_over_mv is not None else None,
        "高两融占比": high_margin,
        "依据": reasons,
    }


# ————————————————————————————————————————————————————————————————
# 二、as-of IO 层(缺数据保守降级,不误触发)
# ————————————————————————————————————————————————————————————————
def load_margin_asof(code: str, as_of: Optional[str]) -> Optional[dict]:
    """as-of 安全地取 ≤as_of 的最新一日两融记录。缺采集/异常 → None(不发声)。

    防未来函数:collectors.margin.summarize_asof 只取 date ≤ as_of 的记录。
    """
    try:
        from tools.collectors import margin as mg
        recs = mg.load_margin(code)
        return mg.summarize_asof(recs, as_of)
    except FileNotFoundError:
        return None
    except Exception as e:                                 # noqa: BLE001
        logger.debug("两融 as-of 读取降级 %s @ %s: %s", code, as_of, str(e)[:80])
        return None


def divergence_asof(code: str, as_of: Optional[str], net_inflow,
                    turnover=None, float_mv=None, c: Optional[dict] = None) -> dict:
    """便捷:load_margin_asof + divergence_verdict(恒不抛,缺数据保守不命中)。

    kill-switch 关 → 直接返回未命中裁决(不触盘,零开销)。
    """
    c = c if c is not None else cfg()
    if not enabled(c):
        return divergence_verdict(net_inflow, None, c=c)   # 应用=False、命中=False
    rec = load_margin_asof(code, as_of)
    return divergence_verdict(net_inflow, rec, turnover=turnover, float_mv=float_mv, c=c)
