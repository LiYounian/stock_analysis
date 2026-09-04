"""合议 / 仲裁层(F3)—— 收集勾选专家 → 加权合成 → 冲突标注 → 可追溯综合结论。

设计权威:docs/计划/多策略合议_专家投票架构_与新策略roadmap.md §三 + §八(D1 加权求和+冲突仅标注)。
仲裁规则(D1 锁定):**加权求和为准,冲突只标注、不改判**。

合成(§2.2 + 弃权稀释修正):
    contrib_i = 强度_i × 置信度_i × 权重_i
    S = Σ contrib_i / 分母
      分母 = Σ(权重_i × 置信度_i)   # 默认"置信度加权":弃权专家(置信度0)不入分母,不稀释在场专家
           或 Σ 权重_i             # 旧"等权":弃权专家仍占分母 → 稀释(config 合议.分母模式 可切回)
    (分母=0 → S=0);S ≥ +τ → 看多;S ≤ −τ → 看空;其间 → 中性

弃权稀释修正(本次):多因子/事件驱动/板块轮动等在数据不全时弃权(置信度0、贡献0),
旧"Σ权重"分母仍把它们的权重计入 → 把 S 拉向 0、稀释在场专家。改用分子分母口径一致的
"Σ(权重×置信度)":弃权专家分子分母都为 0、自动退出;部分降级(置信度0.5)按比例计入。

冲突(§3.2 第4步):同时存在"看多"专家与"看空"专家,且两方各自 |Σ贡献| ≥ conflict_epsilon。

依赖方向:分析层。依赖 契约(expert)+ experts 适配器 + config;
**不 import web / report / serialize / store**(守依赖方向,展示层只读本层落库产物)。
"""
from __future__ import annotations

from tools.analysis import experts
from tools.config.strategy import THRESHOLDS
from tools.contracts.expert import validate_verdict

_C = THRESHOLDS["合议"]


def _abstain_cfg() -> dict:
    """弃权置信度标注配置(单一真源;缺块给保守默认,老 config 兼容)。"""
    return _C.get("弃权置信度标注", {}) or {}


def _confidence_and_shrink(归因: list[dict], S: float, tau: float) -> dict:
    """由逐专家归因派生「合议级参与度 / 置信度 / 软收缩」(纯函数,不改综合分/方向)。

    口径(见 config「弃权置信度标注」注释):
      · 参与(发声)= 权重×置信度 > 0(实际对分母有贡献);弃权 = 数据充分度=="缺失"(无数据);
        在场无权(如技术趋势权重0、数据充分度非缺失)既不参与也不算弃权。
      · 覆盖口径 = 参与专家所属语义桶(config 口径分组)去重;口径多样性 = 桶数。
      · 合议置信度 = clip(参与权重×min(参与/参与饱和,1) + 口径权重×min(口径多样/口径饱和,1), 0, 1)。
      · 收缩系数 = 1(参与≥收缩门槛 或 收缩关) 否则 clip(参与/收缩门槛, 收缩下限, 1);综合分_收缩 = S×系数。
    副作用:给每条归因补 "参与"/"弃权" 布尔(可追溯)。
    """
    cfg = _abstain_cfg()
    groups = cfg.get("口径分组", {}) or {}
    参与饱和 = float(cfg.get("参与饱和", 3) or 3)
    口径饱和 = float(cfg.get("口径饱和", 3) or 3)
    w_part = float(cfg.get("参与权重", 0.6))
    w_div = float(cfg.get("口径权重", 0.4))
    低阈 = float(cfg.get("低置信阈值", 0.5))
    收缩启用 = bool(cfg.get("收缩启用", False))
    收缩门槛 = float(cfg.get("收缩门槛", 3) or 3)
    收缩下限 = float(cfg.get("收缩下限", 0.4))

    覆盖口径: set = set()
    参与数 = 弃权数 = 0
    for a in 归因:
        弃权 = a.get("数据充分度") == "缺失"
        参与 = (float(a.get("权重", 0.0)) * float(a.get("置信度", 0.0))) > 0
        a["弃权"] = bool(弃权)
        a["参与"] = bool(参与)
        if 弃权:
            弃权数 += 1
        if 参与:
            参与数 += 1
            覆盖口径.add(groups.get(a["专家"], a["专家"]))
    口径多样性 = len(覆盖口径)

    part_ratio = min(参与数 / 参与饱和, 1.0) if 参与饱和 > 0 else 0.0
    div_ratio = min(口径多样性 / 口径饱和, 1.0) if 口径饱和 > 0 else 0.0
    合议置信度 = round(max(0.0, min(1.0, w_part * part_ratio + w_div * div_ratio)), 4)

    if not 收缩启用 or 参与数 >= 收缩门槛:
        收缩系数 = 1.0
    else:
        收缩系数 = max(收缩下限, min(1.0, 参与数 / 收缩门槛)) if 收缩门槛 > 0 else 1.0
    综合分_收缩 = round(S * 收缩系数, 4)
    方向_收缩 = "看多" if 综合分_收缩 >= tau else ("看空" if 综合分_收缩 <= -tau else "中性")

    return {
        "参与专家数": 参与数,
        "弃权专家数": 弃权数,
        "专家总数": len(归因),
        "覆盖口径": sorted(覆盖口径),
        "口径多样性": 口径多样性,
        "合议置信度": 合议置信度,
        "低合议置信度": bool(合议置信度 < 低阈),
        "收缩系数": round(收缩系数, 4),
        "综合分_收缩": 综合分_收缩,
        "综合方向_收缩": 方向_收缩,
    }


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
    conf_weighted = _C.get("分母模式", "置信度加权") == "置信度加权"
    wo = weight_override or {}

    verdicts = []
    for name in expert_names:
        v = experts.build(name, record, kline)
        if validate_verdict(v):                     # 守门:非法信封不入合成
            continue
        verdicts.append(v)

    归因 = []
    denom = 0.0
    sum_contrib = 0.0
    for v in verdicts:
        w = float(wo.get(v.专家, v.默认权重))
        conf = float(v.置信度)
        contrib = float(v.强度) * conf * w
        # 分母:置信度加权(弃权专家 conf=0 → 不入分母,不稀释)或 等权(旧口径)
        denom += (w * conf) if conf_weighted else w
        sum_contrib += contrib
        归因.append({"专家": v.专家, "方向": v.方向, "强度": round(float(v.强度), 4),
                     "置信度": round(conf, 4), "权重": w,
                     "贡献": round(contrib, 4), "依据": list(v.依据),
                     "数据充分度": v.数据充分度})

    S = (sum_contrib / denom) if denom > 0 else 0.0
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

    分母口径 = "Σ(权重×置信度)" if conf_weighted else "Σ权重"
    口径 = f"预设权重·τ={tau}·分母={分母口径}·仲裁=加权求和(冲突仅标注)·ε={eps}"
    if wo:
        口径 += "·含权重覆盖"

    out = {
        "综合方向": 综合方向,
        "综合分": round(S, 4),
        "参与专家": [v.专家 for v in verdicts],
        "归因": 归因,
        "是否冲突": 是否冲突,
        "冲突说明": 冲突说明,
        "口径": 口径,
    }

    # 弃权置信度标注(总 kill-switch=标注启用):加合议级参与度/置信度 + 软收缩(纯附加字段,
    # 不改「综合方向/综合分」——前端重合成与既有排序默认口径不漂移;收缩另由「收缩启用」二级开关门控)。
    if bool(_abstain_cfg().get("标注启用", False)):
        out.update(_confidence_and_shrink(归因, S, tau))
    return out


# ————————————————————————————————————————————————
# 「买卖倾向(默认组)」等价迁移预设(F4 · D6=A)
#
# 把 predict.bias_recommendation 的 5 因子(超买超卖/拐点/趋势/资金流/情绪)表达成 5 个专家,
# 用**整数点数**求和 + ±阈值定论,**与旧函数逐票 100% 等价**(exhaustive grid 回归锁死)。
#
# 等价映射证明(整数点数 = 旧函数硬编码):
#   点数真源 THRESHOLDS['合议']['买卖倾向权重'](镜像旧硬编码);情绪阈值沿用 THRESHOLDS['预测'](同源不漂移)。
#   综合分 S 的通用归一公式(§2.2)用于展示层的连续投票;本预设走整数点数以精确复现旧 ±2 判定,
#   二者不冲突:一个面向"通用可勾选投票",一个面向"旧买卖倾向的确定性迁移"。
# ————————————————————————————————————————————————
def _bias_experts(tech: dict, fundflow: dict | None, sentiment: dict | None) -> list[dict]:
    """把买卖倾向 5 因子拆成专家条目 [{专家,方向,点数,依据}]。逐项与旧 bias_recommendation 对齐。"""
    W = _C["买卖倾向权重"]
    P = THRESHOLDS["预测"]                       # 情绪阈值(与旧 bias 同源)
    items: list[dict] = []

    def _add(专家, 点数, 依据):
        if 点数 == 0:
            return
        items.append({"专家": 专家, "方向": "看多" if 点数 > 0 else "看空",
                      "点数": 点数, "依据": 依据})

    ob = (tech.get("ob_os") or {}).get("结论")
    if ob == "超卖":
        _add("超买超卖", W["超卖"], "超卖+2")
    elif ob == "超买":
        _add("超买超卖", W["超买"], "超买-2")

    rev = (tech.get("reversal") or {}).get("拐点标签")
    if rev == "反弹启动":
        _add("拐点", W["反弹启动"], "拐点反弹启动+2")
    elif rev == "超跌待反弹":
        _add("拐点", W["超跌待反弹"], "超跌待反弹+1")

    rating = (tech.get("signal") or {}).get("评级")
    if rating == "偏多":
        _add("技术趋势", W["趋势偏多"], "趋势偏多+1")
    elif rating == "偏空":
        _add("技术趋势", W["趋势偏空"], "趋势偏空-1")

    if fundflow:
        zhu = fundflow.get("今日主力净流入")
        streak = fundflow.get("主力连续净流入天数") or 0
        if isinstance(zhu, (int, float)):
            if zhu > 0:
                _add("资金流", W["主力净流入"], "主力净流入+1")
                if streak >= 2:
                    _add("资金流", W["主力连续净流入"], f"主力连续{streak}天流入+1")
            elif zhu < 0:
                _add("资金流", W["主力净流出"], "主力净流出-1")

    # 情绪三态门控(与 predict.bias_recommendation 逐字对齐,F4 等价红线):
    # 质量=unknown(打分失败)/missing(无输入)→ 情绪专家**干净弃权**,绝不把失败的 0.0/null 当中性票
    # 或按 net>0 判(那正是本次要修的病)。旧记录无「质量」字段 → 回退原 net/样本数 判据(向后兼容)。
    if sentiment and sentiment.get("质量") not in ("unknown", "missing"):
        net = sentiment.get("净情绪分")
        n = sentiment.get("样本数") or 0
        if isinstance(net, (int, float)) and n > 0:
            if net >= P["情绪偏多阈值"]:
                _add("情绪三层", W["情绪偏多"], f"情绪偏多+{W['情绪偏多']}")
            elif net <= P["情绪偏空阈值"]:
                _add("情绪三层", W["情绪偏空"], f"情绪偏空-{abs(W['情绪偏空'])}")
    return items


def bias_council(tech: dict, fundflow: dict | None = None,
                 sentiment: dict | None = None) -> dict:
    """「买卖倾向(默认组)」合议预设:整数点数求和 → 偏买入/偏卖出/观望。

    与 predict.bias_recommendation 逐票 100% 等价(F4 红线)。返回 {结论, 得分, 依据}。
    """
    W = _C["买卖倾向权重"]
    items = _bias_experts(tech, fundflow, sentiment)
    score = sum(it["点数"] for it in items)
    reasons = [it["依据"] for it in items]
    结论 = ("偏买入" if score >= W["偏买入阈值"]
            else ("偏卖出" if score <= W["偏卖出阈值"] else "观望"))
    return {"结论": 结论, "得分": score, "依据": reasons}


_倾向_到_方向 = {"偏买入": "看多", "偏卖出": "看空", "观望": "中性"}


def reconcile_direction(综合方向, 买卖倾向结论) -> dict:
    """同系统对账:策略0合议「综合方向」vs per-stock「买卖倾向」结论 → 内部分歧标记(纯函数)。

    动机(诊断源同名文档 §3.4):同一票合议看多、per-stock 观望(读到主力净流出等 per-stock 独有数据)
    是"内部分歧",分析师应立刻查因。此处只标注、不改判(与合议 D1"冲突仅标注"同精神)。
    程度:相反(看多 vs 看空)> 偏离(一方明确、一方中性/观望)> 一致(同向或都中性)。
    数据不足(任一为空/未知)→ 分歧=False、程度="数据不足"(不误标)。⚠️ 非投资建议。
    """
    b = _倾向_到_方向.get(买卖倾向结论)
    if 综合方向 not in ("看多", "看空", "中性") or b is None:
        return {"分歧": False, "程度": "数据不足",
                "council方向": 综合方向, "per_stock倾向": 买卖倾向结论,
                "说明": "合议方向或买卖倾向缺失,不对账"}
    if 综合方向 == b:
        return {"分歧": False, "程度": "一致",
                "council方向": 综合方向, "per_stock倾向": 买卖倾向结论,
                "说明": f"合议{综合方向}与买卖倾向{买卖倾向结论}一致"}
    程度 = "相反" if {综合方向, b} == {"看多", "看空"} else "偏离"
    return {"分歧": True, "程度": 程度,
            "council方向": 综合方向, "per_stock倾向": 买卖倾向结论,
            "说明": f"内部{程度}:全A合议{综合方向} vs per-stock买卖倾向{买卖倾向结论}"}


# 财报高危红旗 → 选股排序接入(降权/否决)的纯函数移至 config 层(tools.config.strategy),
# 以便**展示层(web)不 import 分析器**(§9.3 依赖方向,test_chart 守门)。此处按需再导出,
# 便于分析/离线侧从合议模块直接取用(单一真源仍在 config)。
from tools.config.strategy import redflag_adjust, redflag_penalty  # noqa: E402,F401


def convene_default(record: dict, kline=None) -> dict:
    """用 config 默认专家组 + 默认(等权)权重召集合议。供批量落库 record['council']。"""
    return convene(list(_C["默认专家组"]), record, kline)


def build_council_block(record: dict, kline=None) -> dict:
    """产出写入中心记录的 council 块:各专家信封 + 默认组合议结果。

    调用方(serialize,非本模块职责)将其挂到 record['council'];本层只产数据、不落盘。
    """
    names = list(_C["默认专家组"])
    experts_env = [experts.build(n, record, kline).to_dict() for n in names]
    # 情绪质量三态可见(B3):让下游/选股 agent 看到「本票综合分是否在情绪 unknown 下得出」。
    # 取自 B1 写入的 sentiment.质量;unknown=情绪打分失败(情绪专家已弃权、未污染综合分,但仍应
    # 打置信度折价/表态封顶——硬封顶由 workflow 纪律层做,此处只保证「标记可见 + 不污染」)。
    sent = (record or {}).get("sentiment")
    情绪质量 = sent.get("质量") if isinstance(sent, dict) else None
    return {
        "experts": experts_env,                 # 各专家信封(供前端勾选重合成 D7)
        "default": convene_default(record, kline),   # 复用 convene → 与合议口径同一真源(含新分母)
        "情绪质量": 情绪质量,                    # ok/partial/unknown/missing|None(旧记录无情绪块)
        # config 是前端重合成的口径真源:必须带「分母模式」,否则前端无从判分母 → 前后端漂移
        "config": {"tau": _C["tau"], "conflict_epsilon": _C["conflict_epsilon"],
                   "分母模式": _C.get("分母模式", "置信度加权"),
                   # 前端重合成合议置信度/收缩的口径真源(缺则前端无从复算 → 前后端漂移)
                   "弃权置信度标注": dict(_abstain_cfg()),
                   "默认权重": dict(_C["默认权重"]), "默认专家组": names},
    }
