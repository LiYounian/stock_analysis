"""统一风控 veto 数据生产层(WI-6 Phase 3 · analysis 层)。

把「财报红旗(质量轴)」+「龙虎榜否决(微结构轴)」两轴的**数据**汇聚到一处,
再交给 config 层纯映射 `strategy.risk_veto_adjust` 合成排序分调整(OR 合成)。

分层理由(守 §9.3 依赖方向):
  · **纯映射**(排序分怎么调)在 `tools.config.strategy`(零依赖,web 展示层可 import);
  · **数据生产**(龙虎榜 as-of 裁决从落盘取)在本模块(analysis 层,可 import backtest.lhb_veto)。
消费侧:
  · `serialize.build_record` 调 `lhb_verdict_asof` 把裁决挂进 record['lhb_veto'](web 只读取用);
  · `pipeline.screen_council` 调 `lhb_verdict_asof` + `strategy.risk_veto_adjust` 做全A策略0排序。

防未来函数(红线):龙虎榜 as-of 裁决走 lhb_veto.entry_veto_asof → lhb.lhb_asof(list_date < as_of
严格小于,盘后披露当天不可用)。本模块只搬运裁决,不放松未来性约束。
⚠️ 非投资建议:风控层只改展示排序/入选,不构成买卖建议。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("analysis.risk_veto")


def lhb_verdict_asof(code: str, as_of: str | None,
                     window_days: int | None = None,
                     min_net_buy_ratio: float | None = None,
                     part_date: str | None = None) -> dict | None:
    """龙虎榜「入选否决」as-of 裁决(轻量 dict),供 record 落库 / 排序汇聚。

    返回 lhb_veto.Verdict.to_dict()(含 triggered/reason/n_recent/list_date/…);
    as_of 缺失 / 无快照 / 采集缺失 / 任意异常 → None(优雅降级,汇聚器视为该轴不发声)。
    window_days / min_net_buy_ratio 缺省读 config 风控汇聚.龙虎榜;可显式覆盖。
    """
    if not as_of:
        return None
    from tools.backtest import lhb_veto
    from tools.config import strategy as cfg
    axis = (cfg.risk_veto_cfg().get("龙虎榜", {}) or {})
    wd = int(axis.get("窗口天数", lhb_veto.VETO_WINDOW_DAYS)) if window_days is None else window_days
    mr = float(axis.get("最小净买占比", 0.0)) if min_net_buy_ratio is None else min_net_buy_ratio
    try:
        v = lhb_veto.entry_veto_asof(code, as_of, window_days=wd,
                                     min_net_buy_ratio=mr, part_date=part_date)
        return v.to_dict()
    except Exception as exc:                           # noqa: BLE001
        logger.debug("龙虎榜裁决降级(无快照/异常)%s @ %s: %s", code, as_of, str(exc)[:80])
        return None


def aggregate(base_score, high_flag_count: int, lhb_verdict: dict | None = None) -> dict:
    """便捷合成:直接把 (base, 高危红旗数, 龙虎榜裁决) 交给 config 纯映射汇聚。

    仅一层薄封装,便于分析/离线侧从 analysis 层统一入口取用;单一真源仍在 config.risk_veto_adjust。
    """
    from tools.config import strategy as cfg
    return cfg.risk_veto_adjust(base_score, high_flag_count, lhb_verdict)
