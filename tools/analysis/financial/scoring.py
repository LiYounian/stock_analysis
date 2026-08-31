"""财报质地评分(数值部分,P0)。

五维:成长 / 质量(盈利质量)/ 健康(财务健康)/ 运营(运营效率)/ 回报(回报能力),
各 0~100,由衍生指标按**绝对阈值**打分(方案 Q6:行业相对分位列为 P2 增强)。
综合 quality_score = 五维加权均值 − 红旗扣分,触发高危红旗封顶。

权重/阈值/扣分/评级映射全部读 config/strategy.py THRESHOLDS["财报"]["评分"](占位待标定)。
LLM 的「好坏画像」层(verdict/qualitative)在本轮置 null 留口(见 analyzer)。

打分为**单调映射**(指标越好分越高),阈值为工程占位;⚠️ 非投资建议。
"""
from __future__ import annotations

from tools.config import strategy


def _cfg() -> dict:
    return strategy.THRESHOLDS.get("财报", {}).get("评分", {})


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _score_linear(val, lo, hi):
    """把 val 从 [lo,hi] 线性映射到 [0,100](val<=lo→0, >=hi→100);val None→None。"""
    if val is None:
        return None
    if hi == lo:
        return 50.0
    return round(_clamp((val - lo) / (hi - lo) * 100), 2)


def _avg(vals) -> float | None:
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def dimension_specs() -> dict:
    """五维各子指标标准化区间(0分端→100分端)。**真源=config['财报']['评分']['五维区间']**(M3 可标定);
    缺配置时回退内置默认。每项:(显示名, derived 键名, 0分端, 100分端);反向指标 0分端>100分端。"""
    cfg = _cfg().get("五维区间")
    if cfg:
        return {dim: [tuple(sub) for sub in subs] for dim, subs in cfg.items()}
    return {
        "成长": [("营收增速%", "营收增速", -20, 40), ("扣非净利增速%", "扣非净利增速", -20, 40)],
        "质量": [("现金含量 CFO/归母净利", "现金含量_CFO比净利", 0, 1.2),
               ("扣非占归母", "扣非占归母", 0, 1.0), ("毛利率%", "毛利率", 0, 50)],
        "健康": [("资产负债率%(反向)", "资产负债率", 80, 30), ("短债覆盖", "短债覆盖", 0, 2),
               ("商誉占净资产%(反向)", "商誉占净资产", 50, 0)],
        "运营": [("应收周转天数(反向)", "应收周转天数", 180, 30),
               ("存货周转天数(反向)", "存货周转天数", 360, 60)],
        "回报": [("ROE%", "ROE", 0, 12)],
    }


def five_dims(derived: dict, specs: dict | None = None) -> dict:
    """五维得分(各 0~100,子项缺失该维用可得子项均值;全缺→None)。

    specs:行业专家覆写的五维区间(格式同 dimension_specs);None → 用通用 dimension_specs(config 真源)。"""
    out = {}
    for dim, subs in (specs or dimension_specs()).items():
        out[dim] = _avg([_score_linear(derived.get(key), lo, hi) for (_name, key, lo, hi) in subs])
    return out


def quality_score(derived: dict, flags: list[dict], specs: dict | None = None,
                  weights: dict | None = None) -> dict:
    """综合质地评分。

    specs / weights:行业专家覆写(五维区间 / 五维权重);None → 用通用(config 真源)。
    Returns: {five_dims, quality_score(0~100 or None), 评级, 红旗扣分, 高危封顶}。
    """
    cfg = _cfg()
    dims = five_dims(derived, specs)
    weights = weights if weights is not None else cfg.get("五维权重", {})
    # 加权均值(仅计可得维度,权重同步剔除)
    num = den = 0.0
    for k, v in dims.items():
        if v is None:
            continue
        w = weights.get(k, 1.0)
        num += v * w
        den += w
    base = round(num / den, 2) if den else None

    # 红旗扣分(提示=0:纯信息标注不压分,如成长期未盈利半导体承接项)
    ded_map = cfg.get("红旗扣分", {"高": 15, "中": 8, "低": 3, "提示": 0})
    deduction = sum(ded_map.get(f.get("严重度", "中"), 8) for f in flags)

    high_cap = cfg.get("高危封顶分", 35)
    capped = any(f.get("严重度") == "高" for f in flags)

    score = None
    if base is not None:
        score = _clamp(base - deduction)
        if capped:
            score = min(score, high_cap)
        score = round(score, 2)

    return {
        "five_dims": dims,
        "quality_score": score,
        "评级": _rating(score, cfg),
        "红旗扣分": deduction,
        "高危封顶": capped,
    }


def _rating(score, cfg: dict) -> str | None:
    """分数 → 评级(优/良/中/差/风险);None→None。"""
    if score is None:
        return None
    m = cfg.get("评级映射", {"优": 80, "良": 65, "中": 50, "差": 35})
    if score >= m.get("优", 80):
        return "优"
    if score >= m.get("良", 65):
        return "良"
    if score >= m.get("中", 50):
        return "中"
    if score >= m.get("差", 35):
        return "差"
    return "风险"
