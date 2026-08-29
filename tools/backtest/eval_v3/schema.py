"""预测记录契约(内部接口):live 轨与 replay 轨都产出这张统一表,喂给统一打分层。

一条"预测记录" = 某策略在某信号日 T 对某票的一次预测。字段:

  strategy_id   策略稳定 ID(与登记表一致,如 'S01'/'4'/'11')
  strategy      策略展示名
  pred_date     信号日 T(YYYY-MM-DD 字符串);预测在 T 收盘后生成,现实最早 T+1 入场
  code          6 位代码
  direction     方向 +1/-1/0(纯多头选股默认 +1;0=中性不计方向命中)
  rank_score    连续打分(可排序型策略有,广筛/参考型为 NaN)——用于 Top-N 分档 + 截面 rank-IC
  source        "live"(线上落盘)/ "replay"(本地历史复现)
  stype         "directional"(广筛型:全部票等权 vs 市场基准)/ "rankable"(可排序型:广筛全量+Top-N+rank-IC)/
                "reference"(参考·非alpha:伪排序,只广筛口径评、不跑 rank-IC/Top-N)
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

# 策略类型标签(评测口径三分流)。
#   ▸ 广筛型 DIRECTIONAL:布尔达标"全上",个股间无连续区分 → 全部票等权 vs 市场基准
#     (命中率+收益质量+超额+显著性)。
#   ▸ 可排序型 RANKABLE:有连续打分(综合分/动量分/RPS 等) → 除广筛全量指标外,
#     额外按打分降序取 Top-5/10/20 算分档精度(命中/期望收益/超额),并报 rank-IC/ICIR。
#     "全部票等权"浪费其排序信息,Top-N 精度检验"分数越高是否越准/越赚"。
#   ▸ 参考/非alpha REFERENCE:伪排序——打分是离散状态格、个股间无真实区分(如指标条件化),
#     跑 rank-IC/Top-N 会得"看着有排序其实是噪声"的误导结论 → 只作参考,按广筛口径评,
#     明确不计入排序榜、不跑 rank-IC/Top-N。
DIRECTIONAL = "directional"   # 广筛型:布尔达标全上 → 命中率+收益质量+超额(全部票等权 vs 市场基准)
RANKABLE = "rankable"         # 可排序型:有连续打分 → 广筛全量指标 + Top-N精度 + rank-IC/ICIR
REFERENCE = "reference"       # 参考/非alpha:伪排序(离散状态格) → 只作参考,不跑 rank-IC/Top-N
# 向后兼容别名(旧口径 RANKING 拆成 RANKABLE/REFERENCE 两类,勿再新用)。
RANKING = RANKABLE


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
    # ── 可排序型(有连续打分):广筛全量指标 + Top-N精度 + rank-IC ──
    "策略0合议": StrategyMeta("0", "多专家合议(全A)", RANKABLE, False,
                              "含新闻/LLM/情绪专家,不可历史回放;综合分作排序分→Top-N精度+rank-IC"),
    "动量组合": StrategyMeta("4", "动量组合(A腿)", RANKABLE, True,
                            "动量分连续排序→Top-N精度+rank-IC(动量分嵌于'特征')"),
    "半导体多因子": StrategyMeta("5", "半导体多因子", RANKABLE, True,
                                "综合分连续排序→Top-N精度+rank-IC"),
    "反转低换手组合": StrategyMeta("10", "反转低换手组合", RANKABLE, True,
                                  "综合分连续排序→Top-N精度+rank-IC"),
    "SEPA合格池": StrategyMeta("SEPA-合格", "SEPA 趋势模板·合格池", RANKABLE, True,
                              "趋势模板 RPS250 排序;live payload 未透出连续RPS则Top-N/IC自动降级为空(标注)"),
    "SEPA观察池": StrategyMeta("SEPA-观察", "SEPA 趋势模板·观察池", RANKABLE, True,
                              "同合格池口径,RPS250 排序"),
    # ── 广筛型(布尔达标全上):全部票等权 vs 市场基准 ──
    # ⚠️ S01 趋势深跌反包 / 箱体3 箱体形态 已因全史深诊断显著负下线,不再进 screenall/回放/前端
    #    (记分卡不当在产);仅保留登记项以便历史 live/replay 记录的稳定命名与归类,note 标注存档。
    "趋势深跌反包": StrategyMeta("S01", "趋势深跌反包", DIRECTIONAL, True,
                              "已下线·显著负仅存档:入场方向反(dead-cat bounce),不进生产。"
                              "详见 docs/计划/S01诊断_删除判定_20260829.md"),
    "放量后缩量回踩": StrategyMeta("S02", "放量后缩量回踩", DIRECTIONAL, True),
    "箱体形态": StrategyMeta("3", "箱体形态", DIRECTIONAL, True,
                          "已下线·显著负仅存档:追高见光死,固定持有期强显著负,不进生产。"
                          "详见 docs/计划/箱体3_显著负根因诊断与救改删判定.md"),
    "最强选股": StrategyMeta("9", "最强选股", DIRECTIONAL, False,
                            "含 Tushare 筹码/资金面,不可历史回放,仅 live 观测"),
    "最大范围选股": StrategyMeta("S03", "最大范围选股", DIRECTIONAL, True),
    "量价放量": StrategyMeta("S04", "量价放量", DIRECTIONAL, True),
    "形态选股": StrategyMeta("形态", "形态选股(RS/杯柄等)", DIRECTIONAL, True),
    # ── 参考/非alpha(伪排序):离散状态格、个股无区分,不跑 rank-IC/Top-N ──
    "指标条件化状态排序": StrategyMeta("11", "指标条件化状态排序", REFERENCE, True,
                                      "打分为离散状态格(上涨概率%分层)、个股间无真实区分,"
                                      "代码自认非alpha;跑rank-IC/Top-N会得误导结论→仅参考、不计排序榜"),
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
