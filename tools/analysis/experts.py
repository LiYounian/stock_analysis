"""专家适配器(F2):把现有策略/分析器输出 → 统一 ExpertVerdict 信封。

设计权威:docs/计划/多策略合议_专家投票架构_与新策略roadmap.md §2.3/§2.4;实现需求 F2。
D4 锁定:**先适配器、不动 registry 地基** —— 本模块只在其上包装,不改 tools/strategy/registry.py。

两条产出路径:
  1) 内置 4+1 专家(record-shaped):技术趋势/超买超卖/拐点/资金流/情绪三层 —— 直接读中心记录字段。
  2) 通用适配:把 registry 三类(评分/选股/信号)包装成 ExpertVerdict(§2.3 规则)。

依赖方向:分析层。依赖 契约(expert)+ config + registry(同层);**不 import web/report/serialize/store**。
缺数据策略:不跳过,给"方向=中性、强度=0、置信度=0、数据充分度=缺失",保留"弃权/降级"的可见性。
"""
from __future__ import annotations

import math

from tools.config.strategy import THRESHOLDS
from tools.contracts.expert import ExpertVerdict

_C = THRESHOLDS["合议"]
_CONF = _C["置信度映射"]
_SUFF = _CONF["数据充分度"]        # {充分:1.0, 部分降级:0.5, 缺失:0.0}


def _w(name: str) -> float:
    """专家默认权重(config 真源,缺省 1.0)。"""
    return float(_C["默认权重"].get(name, 1.0))


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _missing(name: str, 能力类型: str = "方向", 原因: str = "数据缺失") -> ExpertVerdict:
    """缺数据的统一弃权信封:中性 + 强度0 + 置信度0 + 数据充分度=缺失。"""
    return ExpertVerdict(专家=name, 能力类型=能力类型, 方向="中性", 强度=0.0,
                         置信度=0.0, 默认权重=_w(name), 依据=[原因],
                         数据充分度="缺失", 原始={})


# ————————————————————————————————————————————————
# 内置专家(record-shaped):直接读中心记录
# ————————————————————————————————————————————————
def expert_技术趋势(record: dict, kline=None) -> ExpertVerdict:
    """signals.trend.评级 → 方向;得分/100 → 强度(中性时强度归 0 保契约)。"""
    sig = (record or {}).get("signals") or {}
    t = sig.get("trend")
    if not isinstance(t, dict) or t.get("评级") is None:
        return _missing("技术趋势")
    rating = t.get("评级")                       # 偏多/中性/偏空
    score = t.get("得分")
    依据 = list(t.get("依据") or [])
    方向 = {"偏多": "看多", "偏空": "看空"}.get(rating, "中性")
    强度 = 0.0 if 方向 == "中性" else _clamp((score or 0) / 100.0)
    # 符号护栏(评级与得分理论上同号,异常时以方向为准)
    if 方向 == "看多":
        强度 = abs(强度)
    elif 方向 == "看空":
        强度 = -abs(强度)
    return ExpertVerdict(专家="技术趋势", 能力类型="评级", 方向=方向, 强度=强度,
                         置信度=_SUFF["充分"], 默认权重=_w("技术趋势"),
                         依据=依据 or [f"趋势{rating}"], 数据充分度="充分",
                         原始={"评级": rating, "得分": score})


def expert_超买超卖(record: dict, kline=None) -> ExpertVerdict:
    """signals.ob_os.verdict → 方向(超卖→看多、超买→看空);置信度 = 共振数/满档。"""
    sig = (record or {}).get("signals") or {}
    o = sig.get("ob_os")
    if not isinstance(o, dict) or o.get("verdict") is None:
        return _missing("超买超卖")
    verdict = o.get("verdict")                    # 超买/超卖/中性
    reson = int(o.get("resonance") or 0)
    方向 = {"超卖": "看多", "超买": "看空"}.get(verdict, "中性")
    conf = _clamp(reson / float(_CONF["共振满档"]), 0.0, 1.0)
    if 方向 == "中性":
        强度, 置信度, 充分 = 0.0, conf * _SUFF["充分"], "充分"
    else:
        mag = conf                                # 共振越多越强
        强度 = mag if 方向 == "看多" else -mag
        置信度 = conf
        充分 = "充分" if reson >= _CONF["共振满档"] else "部分降级"
    return ExpertVerdict(专家="超买超卖", 能力类型="方向", 方向=方向, 强度=强度,
                         置信度=置信度, 默认权重=_w("超买超卖"),
                         依据=[f"{verdict}·共振{reson}"], 数据充分度=充分,
                         原始={"verdict": verdict, "resonance": reson})


def expert_拐点(record: dict, kline=None) -> ExpertVerdict:
    """signals.reversal.拐点标签 → 方向(反弹启动/超跌待反弹→看多);拐点评分/100 → 强度。"""
    sig = (record or {}).get("signals") or {}
    r = sig.get("reversal")
    if not isinstance(r, dict) or r.get("拐点标签") is None:
        return _missing("拐点")
    label = r.get("拐点标签")                     # 反弹启动/超跌待反弹/无
    score = r.get("拐点评分") or 0
    if label in ("反弹启动", "超跌待反弹"):
        方向 = "看多"
        强度 = abs(_clamp(score / 100.0))
    else:
        方向, 强度 = "中性", 0.0
    return ExpertVerdict(专家="拐点", 能力类型="方向", 方向=方向, 强度=强度,
                         置信度=_SUFF["充分"], 默认权重=_w("拐点"),
                         依据=[f"拐点{label}·评分{score}"], 数据充分度="充分",
                         原始={"拐点标签": label, "拐点评分": score})


def expert_资金流(record: dict, kline=None) -> ExpertVerdict:
    """fundflow.今日主力净流入 → 方向;连续净流入天数抬升强度。缺失→弃权。"""
    f = (record or {}).get("fundflow")
    if not isinstance(f, dict) or f.get("今日主力净流入") is None:
        return _missing("资金流")
    net = f.get("今日主力净流入")
    streak = int(f.get("主力连续净流入天数") or 0)
    if not isinstance(net, (int, float)):
        return _missing("资金流")
    if net > 0:
        方向 = "看多"
        强度 = _clamp(0.5 + (0.5 if streak >= 2 else 0.0))
        依据 = ["主力净流入" + (f"·连续{streak}天" if streak >= 2 else "")]
    elif net < 0:
        方向, 强度, 依据 = "看空", -0.5, ["主力净流出"]
    else:
        方向, 强度, 依据 = "中性", 0.0, ["主力净流入为0"]
    return ExpertVerdict(专家="资金流", 能力类型="方向", 方向=方向, 强度=强度,
                         置信度=_SUFF["充分"], 默认权重=_w("资金流"),
                         依据=依据, 数据充分度="充分",
                         原始={"今日主力净流入": net, "主力连续净流入天数": streak})


def expert_情绪三层(record: dict, kline=None) -> ExpertVerdict:
    """sentiment.净情绪分 → 方向(±0.2 阈值);置信度 = 数据充分度 × 样本数/饱和。样本0→缺失。"""
    s = (record or {}).get("sentiment")
    if not isinstance(s, dict):
        return _missing("情绪三层")
    net = s.get("净情绪分")
    n = int(s.get("样本数") or 0)
    if not isinstance(net, (int, float)) or n <= 0:
        return _missing("情绪三层", 原因="情绪样本数为0")
    conf = _clamp(n / float(_CONF["样本饱和数"]), 0.0, 1.0)
    if net >= 0.2:
        方向, 强度 = "看多", abs(_clamp(net))
    elif net <= -0.2:
        方向, 强度 = "看空", -abs(_clamp(net))
    else:
        方向, 强度 = "中性", 0.0
    充分 = "充分" if n >= _CONF["样本饱和数"] else "部分降级"
    return ExpertVerdict(专家="情绪三层", 能力类型="方向", 方向=方向, 强度=强度,
                         置信度=conf, 默认权重=_w("情绪三层"),
                         依据=[f"净情绪{net}·样本{n}"], 数据充分度=充分,
                         原始={"净情绪分": net, "样本数": n})


# 内置专家名 → 适配器(record-shaped)
BUILTIN = {
    "技术趋势": expert_技术趋势,
    "超买超卖": expert_超买超卖,
    "拐点": expert_拐点,
    "资金流": expert_资金流,
    "情绪三层": expert_情绪三层,
}


# ————————————————————————————————————————————————
# 通用适配:registry 三类 → ExpertVerdict(§2.3)
# ————————————————————————————————————————————————
def from_score(name: str, record: dict, scale: float = 5.0) -> ExpertVerdict:
    """评分策略 fn(record)->{score,依据}:sign(score)→方向;tanh(score/scale)→强度(有界归一,启发式)。"""
    from tools.strategy import registry
    try:
        out = registry.run(name, record) or {}
    except Exception:
        return _missing(name, 能力类型="评级", 原因="评分策略执行失败")
    score = out.get("score")
    if not isinstance(score, (int, float)):
        return _missing(name, 能力类型="评级", 原因="score 缺失")
    mag = _clamp(math.tanh(score / scale), 0.0, 1.0) if score else 0.0
    if score > 0:
        方向, 强度 = "看多", mag
    elif score < 0:
        方向, 强度 = "看空", -mag
    else:
        方向, 强度 = "中性", 0.0
    return ExpertVerdict(专家=name, 能力类型="评级", 方向=方向, 强度=强度,
                         置信度=_SUFF["充分"], 默认权重=_w(name),
                         依据=list(out.get("依据") or []), 数据充分度="充分",
                         原始={"score": score, "归一scale": scale})


def from_screen(name: str, records: dict, code: str) -> ExpertVerdict:
    """选股策略 fn(records)->[codes]:该票入选→看多、未入选→中性(**不反推看空**,§2.3)。"""
    from tools.strategy import registry
    try:
        hits = set(registry.run(name, records) or [])
    except Exception:
        return _missing(name, 能力类型="入选", 原因="选股策略执行失败")
    if code in hits:
        return ExpertVerdict(专家=name, 能力类型="入选", 方向="看多", 强度=0.6,
                             置信度=_SUFF["充分"], 默认权重=_w(name),
                             依据=[f"入选「{name}」"], 数据充分度="充分",
                             原始={"入选": True})
    return ExpertVerdict(专家=name, 能力类型="入选", 方向="中性", 强度=0.0,
                         置信度=_SUFF["充分"], 默认权重=_w(name),
                         依据=[f"未入选「{name}」"], 数据充分度="充分",
                         原始={"入选": False})


def from_signal(name: str, kline) -> ExpertVerdict:
    """信号策略 fn(kline)->[买/卖/持]:取最新一根 → 看多/看空/中性(§2.3)。"""
    from tools.strategy import registry
    if kline is None:
        return _missing(name, 能力类型="信号", 原因="无 K 线")
    try:
        seq = registry.run(name, kline) or []
    except Exception:
        return _missing(name, 能力类型="信号", 原因="信号策略执行失败")
    if not seq:
        return _missing(name, 能力类型="信号", 原因="信号序列为空")
    last = seq[-1]
    方向 = {"买": "看多", "卖": "看空"}.get(last, "中性")
    强度 = {"看多": 0.6, "看空": -0.6}.get(方向, 0.0)
    return ExpertVerdict(专家=name, 能力类型="信号", 方向=方向, 强度=强度,
                         置信度=_SUFF["充分"], 默认权重=_w(name),
                         依据=[f"最新信号「{last}」"], 数据充分度="充分",
                         原始={"最新信号": last, "序列长度": len(seq)})


# ————————————————————————————————————————————————
# 统一入口
# ————————————————————————————————————————————————
def build(name: str, record: dict, kline=None) -> ExpertVerdict:
    """按专家名产 ExpertVerdict:内置优先;否则按 registry kind 分派通用适配。

    未知专家/未注册 → 弃权信封(不抛,保证批量健壮)。产出恒过契约校验。
    """
    if name in BUILTIN:
        v = BUILTIN[name](record, kline)
        v.validate()
        return v
    # registry 通用分派
    try:
        from tools.strategy import registry
        meta = registry.get(name)
    except Exception:
        return _missing(name, 原因=f"未注册专家: {name}")
    if meta.kind == "评分":
        v = from_score(name, record)
    elif meta.kind == "选股":
        code = ((record or {}).get("meta") or {}).get("code")
        v = from_screen(name, {code: record} if code else {}, code)
    elif meta.kind == "信号":
        v = from_signal(name, kline)
    else:
        v = _missing(name, 原因=f"未知 kind: {meta.kind}")
    v.validate()
    return v
