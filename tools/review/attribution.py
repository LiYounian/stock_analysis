"""规则归因引擎：选对靠什么 / 选错错在哪（大复盘的"为什么"层）。

映射权威：`docs/计划/2026-09-17_板块线依据到归因桶_映射.md`（板块线 wizardly-swirles 定稿·合入 main）
+ 本项目计划 §5。**规则判不了 → None（有声缺失，不猜）**；LLM 层（可选）另填。

诚实边界：事后涨跌来自 kline（r_exit），**是否成交来自执行侧（Model-A labels）**，板块线只给"信号确为强"
一半——踏空须 join 两侧（板块强信号 × Model-A 未触发 × runaway_up）。
"""
from __future__ import annotations

from tools.review.types import Attribution, ModelALabels, Pick

MOVED_PCT = 0.03      # 已动阈（当日涨幅≥3%），与 news_focus_block.MOVED_PCT / sector_news_forward 一致
POS_HIGH = 0.9        # 位置pos60 高位阈
NEAR_60_HIGH = -0.03  # 距60高 接近（≥此=贴近前高）


# ── 依据谓词（读 Pick 结构化字段）────────────────────────────────────────
def _events(pick: Pick) -> list[dict]:
    return pick.板块消息面.get("关键事件") or []


def _has_strong_catalyst(pick: Pick) -> bool:
    """真强催化：利好方向 + 影响大 + 一手 + 执行高（板块线的"信号确为强"半）。"""
    for e in _events(pick):
        if (e.get("方向") == "利好" and e.get("影响程度") == "大"
                and e.get("来源") == "一手" and e.get("执行度") == "高"):
            return True
    return False


def _has_weak_catalyst(pick: Pick) -> bool:
    """假利好探测器：有利好事件但质量弱（可信度存疑/不可信 或 二手 或 执行度低），或板块强弱中/弱。"""
    weak_events = False
    for e in _events(pick):
        if e.get("方向") != "利好":
            continue
        if (e.get("可信度") in ("存疑", "不可信") or e.get("来源") == "二手"
                or e.get("执行度") == "低"):
            weak_events = True
    return weak_events or pick.板块消息面.get("强弱") in ("中", "弱")


def _moved(pick: Pick) -> bool:
    v = pick.形态.get("当日涨跌")
    return isinstance(v, (int, float)) and v >= MOVED_PCT


def _high_position(pick: Pick) -> bool:
    pos = pick.形态.get("位置pos60")
    near = pick.形态.get("距60高")
    return (isinstance(pos, (int, float)) and pos >= POS_HIGH) \
        or (isinstance(near, (int, float)) and near >= NEAR_60_HIGH) \
        or bool(pick.形态.get("涨停不可买"))


def _flag_count(pick: Pick) -> int:
    v = pick.council.get("财报红旗数")
    return v if isinstance(v, int) else 0


def _is_recommended(pick: Pick) -> bool:
    return pick.档 in ("推荐", "观察")


def _cold_hot_off(pick: Pick) -> bool:
    """板块基调踏错：sector_regime 冷热标签 过热/拐点。"""
    return pick.board_regime.get("冷热标签") in ("过热", "拐点")


def _market_bearish(market_ctx: dict | None) -> bool:
    if not market_ctx:
        return False
    return market_ctx.get("宏观净方向") in ("看空", "偏空", "空") \
        or market_ctx.get("风险偏好") in ("低", "偏弱", "谨慎")


# ── 选对 / 选错 分桶 ──────────────────────────────────────────────────────
def _correct_bucket(pick: Pick) -> tuple[str | None, str]:
    src, role = pick.来源, pick.角色
    if src == "板块催化" and role == "龙头":
        return "板块-龙头催化", f"板块催化·龙头·强弱={pick.板块消息面.get('强弱')}"
    if src == "板块催化" and role == "跟涨":
        return "板块-联动跟涨", "板块催化·跟涨（二阶联动·权重低于龙头）"
    if src == "策略直选":
        hits = pick.策略命中 or []
        if any("财报" in str(h) for h in hits):
            return "财报", f"策略直选·财报命中={hits}"
        if hits:
            return "策略", f"策略直选·命中={hits}"
        if pick.形态.get("均线多头"):
            return "形态", "策略直选·均线多头（量价形态）"
    return None, "规则判不了（有声缺失）"


def _wrong_bucket(pick: Pick, lab: ModelALabels, market_ctx: dict | None) -> tuple[str | None, str]:
    # 未触发分支：只判踏空（板块强信号 × 未成交 × 冲走）
    if lab.untriggered:
        if lab.runaway_up and (_has_strong_catalyst(pick)
                               or pick.板块消息面.get("强弱") == "强"):
            return "踏空", "板块强信号+真催化，但回踩限价未触发、票高开冲走（踏空）"
        return None, "未触发但非踏空（无强信号/未冲走·有声缺失）"

    # 已成交且亏（r_exit≤0）：按优先级
    if _flag_count(pick) > 0 and _is_recommended(pick):
        return "财报红旗漏判", f"财报红旗数={_flag_count(pick)} 仍入{pick.档}档、成交后亏"
    if _moved(pick) or pick.形态.get("涨停不可买") or _high_position(pick):
        return "追高", f"已动/高位追（涨跌={pick.形态.get('当日涨跌')}·涨停不可买={pick.形态.get('涨停不可买')}·pos60={pick.形态.get('位置pos60')}）成交后亏"
    if pick.来源 == "板块催化" and _has_weak_catalyst(pick):
        return "假利好", f"板块催化但催化质量弱（强弱={pick.板块消息面.get('强弱')}·事件三档弱）成交后亏"
    if _cold_hot_off(pick):
        return "踏错板块基调", f"板块冷热={pick.board_regime.get('冷热标签')}（过热/拐点）成交后亏"
    if _market_bearish(market_ctx):
        return "踏错大盘基调", "弱市高β跟跌（大盘净方向偏空）"
    return None, "亏因规则判不了（有声缺失）"


def classify(pick: Pick, lab: ModelALabels, market_ctx: dict | None = None) -> Attribution:
    """判对错并归因。r_exit>0→选对桶；r_exit≤0 或 未触发→选错桶；pending→两桶 None（不判）。"""
    # 未触发：走选错分支（踏空）；已成交但收益未定（pending）→ 不判
    if not lab.untriggered and lab.r_exit is None:
        return Attribution(evidence="收益未定（pending·不判）")

    if lab.r_exit is not None and lab.r_exit > 0:
        bucket, ev = _correct_bucket(pick)
        return Attribution(correct_bucket=bucket, evidence=ev)

    bucket, ev = _wrong_bucket(pick, lab, market_ctx)
    return Attribution(wrong_bucket=bucket, evidence=ev)
