"""预测记录契约(内部接口):live 轨与 replay 轨都产出这张统一表,喂给统一打分层。

一条"预测记录" = 某策略在某信号日 T 对某票的一次预测。字段:

  strategy_id   策略稳定 ID(与登记表一致,如 'S01'/'4'/'11')
  strategy      策略展示名
  pred_date     信号日 T(YYYY-MM-DD 字符串);预测在 T 收盘后生成,现实最早 T+1 入场
  code          6 位代码
  direction     方向 +1/-1/0(纯多头选股默认 +1;0=中性不计方向命中)
  rank_score    排序分(仅排序型策略有,方向型为 NaN)——用于截面 rank-IC
  source        "live"(线上落盘)/ "replay"(本地历史复现)
  stype         "directional"(方向型选股:命中率+收益质量+超额)/ "ranking"(排序型:rank-IC/ICIR)
  replayable    该策略是否可历史回放(含 LLM/新闻/情绪/筹码的不可回放,仅 live 观测)

打分层对这张表统一处理:定位 T+1 入场→算各 horizon T+1 基准实现收益→方向命中(期末/期内触及)
+ 收益质量 + 超额 + 显著性;排序型走 rank-IC 分支。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# 统一表列顺序(所有生产者/消费者共用)。
PRED_COLUMNS = [
    "strategy_id", "strategy", "pred_date", "code",
    "direction", "rank_score", "source", "stype", "replayable",
]

# 策略类型标签。
DIRECTIONAL = "directional"   # 方向型选股:选出即看多/看空 → 命中率+收益质量+超额
RANKING = "ranking"           # 排序型:截面打分排序 → rank-IC/ICIR(不套方向命中)


@dataclass
class StrategyMeta:
    """策略元信息登记项:稳定 ID/展示名/类型/是否可回放。"""

    strategy_id: str
    name: str
    stype: str = DIRECTIONAL
    replayable: bool = True
    note: str = ""


# 策略登记表(评测口径):file stem → StrategyMeta。
# stype/replayable 决定该策略走哪套指标、进哪条轨。
# 不可回放理由:策略0=多专家含新闻/LLM/情绪打分;策略9最强选股=含 Tushare 筹码/资金面。
STRATEGY_META: dict[str, StrategyMeta] = {
    "策略0合议": StrategyMeta("0", "多专家合议(全A)", RANKING, False,
                              "含新闻/LLM/情绪专家,不可历史回放;综合分作排序分走 rank-IC"),
    "趋势深跌反包": StrategyMeta("S01", "趋势深跌反包", DIRECTIONAL, True),
    "放量后缩量回踩": StrategyMeta("S02", "放量后缩量回踩", DIRECTIONAL, True),
    "箱体形态": StrategyMeta("3", "箱体形态", DIRECTIONAL, True),
    "动量组合": StrategyMeta("4", "动量组合(A腿)", DIRECTIONAL, True),
    "半导体多因子": StrategyMeta("5", "半导体多因子", DIRECTIONAL, True),
    "最强选股": StrategyMeta("9", "最强选股", DIRECTIONAL, False,
                            "含 Tushare 筹码/资金面,不可历史回放,仅 live 观测"),
    "反转低换手组合": StrategyMeta("10", "反转低换手组合", RANKING, True,
                                  "综合分作排序分;方向型多头选股,同时给 rank-IC"),
    "指标条件化状态排序": StrategyMeta("11", "指标条件化状态排序", RANKING, True,
                                      "天然中性/排序型,用 rank-IC 而非方向命中"),
    "最大范围选股": StrategyMeta("S03", "最大范围选股", DIRECTIONAL, True),
    "量价放量": StrategyMeta("S04", "量价放量", DIRECTIONAL, True),
    "SEPA合格池": StrategyMeta("SEPA-合格", "SEPA 趋势模板·合格池", DIRECTIONAL, True),
    "SEPA观察池": StrategyMeta("SEPA-观察", "SEPA 趋势模板·观察池", DIRECTIONAL, True),
    "形态选股": StrategyMeta("形态", "形态选股(RS/杯柄等)", DIRECTIONAL, True),
}


def meta_for(stem: str) -> StrategyMeta:
    """按文件 stem 取元信息;未登记 → 兜底为方向型可回放(ID=stem)。"""
    return STRATEGY_META.get(stem, StrategyMeta(stem, stem, DIRECTIONAL, True, "未登记,兜底方向型"))


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=PRED_COLUMNS)


def make_frame(records: list[dict]) -> pd.DataFrame:
    """把预测记录 dict 列表规整成统一表(补全缺列、定列序、去空 code)。"""
    df = pd.DataFrame(records, columns=PRED_COLUMNS)
    if df.empty:
        return df
    df = df[df["code"].notna() & (df["code"].astype(str) != "")]
    df["code"] = df["code"].astype(str)
    df["pred_date"] = df["pred_date"].astype(str).str.slice(0, 10)
    df["direction"] = df["direction"].fillna(0).astype(int)
    return df.reset_index(drop=True)
