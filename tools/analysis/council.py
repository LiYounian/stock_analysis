"""合议 / 仲裁层(F3)—— 收集勾选专家 → 加权合成 → 冲突标注 → 可追溯综合结论。

设计权威:docs/计划/多策略合议_专家投票架构_与新策略roadmap.md §三 + §八(D1 加权求和+冲突仅标注)。
仲裁规则(D1 锁定):**加权求和为准,冲突只标注、不改判**。

合成(§2.2):
    contrib_i = 强度_i × 置信度_i × 权重_i
    S = Σ contrib_i / Σ 权重_i           # 归一回 [-1,1](Σ权重=0 时 S=0)
    S ≥ +τ → 看多;S ≤ −τ → 看空;其间 → 中性

冲突(§3.2 第4步):同时存在"看多"专家与"看空"专家,且两方各自 |Σ贡献| ≥ conflict_epsilon。

依赖方向:分析层。依赖 契约(expert)+ experts 适配器 + config;
**不 import web / report / serialize / store**(守依赖方向,展示层只读本层落库产物)。
"""
from __future__ import annotations

from tools.analysis import experts
from tools.config.strategy import THRESHOLDS
from tools.contracts.expert import validate_verdict

_C = THRESHOLDS["合议"]


def convene(expert_names: list[str], record: dict, kline=None,
            weight_override: dict | None = None) -> dict:
    """对一只票召集指定专家合议,返回综合结论(含逐专家归因)。

    Args:
        expert_names: 参与合议的专家名列表(勾选)。
        record: 单票中心记录(只读)。
        kline: 可选,信号类专家需要。
        weight_override: 可选 {专家名: 权重} 临时覆盖默认权重(D2 用户可临时改权)。

    Returns(CouncilResult):
        {综合方向, 综合分, 参与专家[], 归因[], 是否冲突, 冲突说明, 口径}
    """
    tau = float(_C["tau"])
    eps = float(_C["conflict_epsilon"])
    wo = weight_override or {}

    verdicts = []
    for name in expert_names:
        v = experts.build(name, record, kline)
        if validate_verdict(v):                     # 守门:非法信封不入合成
            continue
        verdicts.append(v)

    归因 = []
    sum_w = 0.0
    sum_contrib = 0.0
    for v in verdicts:
        w = float(wo.get(v.专家, v.默认权重))
        contrib = float(v.强度) * float(v.置信度) * w
        sum_w += w
        sum_contrib += contrib
        归因.append({"专家": v.专家, "方向": v.方向, "强度": round(float(v.强度), 4),
                     "置信度": round(float(v.置信度), 4), "权重": w,
                     "贡献": round(contrib, 4), "依据": list(v.依据),
                     "数据充分度": v.数据充分度})

    S = (sum_contrib / sum_w) if sum_w > 0 else 0.0
    综合方向 = "看多" if S >= tau else ("看空" if S <= -tau else "中性")

    # 冲突:正反两方各自加权贡献都非微弱
    pos = sum(a["贡献"] for a in 归因 if a["贡献"] > 0)
    neg = sum(-a["贡献"] for a in 归因 if a["贡献"] < 0)   # 取正数量级
    是否冲突 = bool(pos >= eps and neg >= eps)
    冲突说明 = ""
    if 是否冲突:
        多 = [a["专家"] for a in 归因 if a["贡献"] > 0]
        空 = [a["专家"] for a in 归因 if a["贡献"] < 0]
        冲突说明 = f"看多{多}(+{round(pos,4)}) vs 看空{空}(-{round(neg,4)})"

    归因.sort(key=lambda a: abs(a["贡献"]), reverse=True)

    口径 = f"预设权重·τ={tau}·仲裁=加权求和(冲突仅标注)·ε={eps}"
    if wo:
        口径 += "·含权重覆盖"

    return {
        "综合方向": 综合方向,
        "综合分": round(S, 4),
        "参与专家": [v.专家 for v in verdicts],
        "归因": 归因,
        "是否冲突": 是否冲突,
        "冲突说明": 冲突说明,
        "口径": 口径,
    }


def convene_default(record: dict, kline=None) -> dict:
    """用 config 默认专家组 + 默认(等权)权重召集合议。供批量落库 record['council']。"""
    return convene(list(_C["默认专家组"]), record, kline)


def build_council_block(record: dict, kline=None) -> dict:
    """产出写入中心记录的 council 块:各专家信封 + 默认组合议结果。

    调用方(serialize,非本模块职责)将其挂到 record['council'];本层只产数据、不落盘。
    """
    names = list(_C["默认专家组"])
    experts_env = [experts.build(n, record, kline).to_dict() for n in names]
    return {
        "experts": experts_env,                 # 各专家信封(供前端勾选重合成 D7)
        "default": convene_default(record, kline),
        "config": {"tau": _C["tau"], "conflict_epsilon": _C["conflict_epsilon"],
                   "默认权重": dict(_C["默认权重"]), "默认专家组": names},
    }
