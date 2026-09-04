"""扣非质量主筛(#31)—— 纯扣非质量横截面排序选股(方案 A,已拍板)。

设计文档:docs/计划/2026-09-04_扣非质量主筛_设计.md。⚠️ 非投资建议,研究模拟。

立场(为什么这样做):现有在产策略主筛口径全是位置/量价/形态,**没有一个以利润质量为召回口径**——
扣非/质量只出现在否决/降权层(把坏票往下压),从不用来把好票选出来 → 高质量、非强势的票系统性隐身。
本策略在**全A横截面上按利润质量召回**,让"质量硬、位置弱"的票进候选并集被下游合议看到;
**刻意不与位置耦合**(耦合会抹掉"质量⊥位置"的正交性,重新引入要覆盖的偏差)。位置/择时已有多个策略覆盖。

分层(与 semi_factor / reversal_turnover 一致,守 docs/开发规范.md §5.1):
  · 纯因子函数(本文件上半):输入单票 derived → 各维原始因子值;可脱离 IO 独测。
  · 选股策略(本文件下半 `combo_deduct_quality_screen`):输入已带 financial.derived 的中心记录 →
    横截面 winsorize+zscore → **缺维重归一**等权复合 → 可交易性过滤 → 排序取 top_k。
  · 薄管线(tools/pipeline/screen_deduct_quality.py):补采财报、建最小 record、算跨期质量领先度、落 view。

因子构成(全读 record['financial']['derived'],方向均"越大越好"):
  扣非增速     = derived['扣非净利增速']        扣非同比(累计 YoY)
  扣非占归母   = derived['扣非占归母']          扣非/归母(= 1 − 非经常损益占比),利润真实性
  现金含量     = derived['现金含量_CFO比净利']   经营现金流/归母净利,利润含金量
  毛利率       = derived['毛利率']              (营收−营业成本)/营收,盈利能力
  质量领先度   = 扣非增速 − 归母增速            "扣非快于归母"(协创数据那种质量领先);
                管线可注入 record['扣非质量']['质量领先度'] 覆盖为**跨期 N 期均值**(更稳、防未来函数锚披露日);
                未注入 → 由 derived 现算单期值兜底(扣非净利增速 − 归母净利增速)。

缺失纪律(与本项目"缺失不塌缩成 0"的既定纪律一致):
  · 某维横截面有效样本 <2 → 该维不参与(无法标准化,不给全体记 0);
  · 单票缺某维 → 该维不计入其复合,权重在**可用维上重归一**(不把缺维当中性 0 稀释);
  · 单票全维缺失 → 不参与召回(不是给 0 分)。

防未来函数:核心 4 维基于**已披露**报告期 derived(analyzer 控 disclosure_date ≤ as_of);
  跨期质量领先度由管线读多期 raw、只取披露日 ≤ as_of 的报告期。纯函数只吃入参、同入同出。
"""
from __future__ import annotations

import math
from typing import Optional

from tools.config.strategy import THRESHOLDS
from tools.strategy import reversal_veto
from tools.strategy._factor_util import winsorize_med, zscore
from tools.strategy.registry import strategy

# —— 默认参数(单一真源在 THRESHOLDS["扣非质量"];此处取值,缺键兜底)——
_CFG = THRESHOLDS.get("扣非质量", {})
_TOP_K = int(_CFG.get("top_k", 30))
_WINSOR = float(_CFG.get("winsor_scale", 3.0))
_N_PERIODS = int(_CFG.get("多期N", 5))
_W = dict(_CFG.get("权重", {}))
_LIQ = dict(_CFG.get("流动性", {}))
_LIQ_ST = bool(_LIQ.get("剔除ST", True))
_MIN_AMOUNT_WAN = float(_LIQ.get("最小成交额_万元", 5000))
_MIN_MKTCAP_YI = float(_LIQ.get("最小流通市值_亿", 20))

_FIELD = "扣非质量"                       # record 里(可选)存放注入因子的命名空间键

# 五维定义:(维度名, derived 源字段, 权重键)。derived 源为 None 表示该维由现算而非直接取。
_CORE_DIMS = (
    ("扣非增速", "扣非净利增速"),
    ("扣非占归母", "扣非占归母"),
    ("现金含量", "现金含量_CFO比净利"),
    ("毛利率", "毛利率"),
)
_ALL_DIM_NAMES = tuple(name for name, _ in _CORE_DIMS) + ("质量领先度",)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# ————————————————————————————————————————————————————————————————
# 一、纯因子函数(输入单票 derived,输出各维原始因子值;None = 缺失)
# ————————————————————————————————————————————————————————————————
def core_factors(derived: Optional[dict]) -> dict[str, Optional[float]]:
    """从 financial.derived 抽 4 个核心维(越大越好);缺维 → None(不塌缩成 0)。"""
    d = derived or {}
    out: dict[str, Optional[float]] = {}
    for name, src in _CORE_DIMS:
        v = d.get(src)
        out[name] = float(v) if _finite(v) else None
    return out


def single_period_lead(derived: Optional[dict]) -> Optional[float]:
    """单期质量领先度 = 扣非净利增速 − 归母净利增速(均在 derived,无 IO)。任一缺 → None。

    正且大 → 扣非增速快于归母(利润质量在改善,非经常损益不是增长主力),即"扣非快于归母"。
    """
    d = derived or {}
    kf, gm = d.get("扣非净利增速"), d.get("归母净利增速")
    if _finite(kf) and _finite(gm):
        return float(kf) - float(gm)
    return None


def factors_of(rec: Optional[dict]) -> dict[str, Optional[float]]:
    """单票五维原始因子:4 核心维(读 derived)+ 质量领先度(优先取管线注入的跨期均值,
    否则由 derived 现算单期值兜底)。全维可能为 None,交由横截面层判缺维/重归一。
    """
    rec = rec or {}
    derived = (rec.get("financial") or {}).get("derived") or {}
    out = core_factors(derived)
    injected = (rec.get(_FIELD) or {}).get("质量领先度")
    out["质量领先度"] = (float(injected) if _finite(injected)
                        else single_period_lead(derived))
    return out


# ————————————————————————————————————————————————————————————————
# 一·补 横截面标准化(某维有效样本 <2 → 该维不参与,视为全体缺维)
# ————————————————————————————————————————————————————————————————
def _cross_z(pairs: list[tuple[str, Optional[float]]], scale: float) -> dict[str, float]:
    """对一维的 (code, 原始值) 做 winsorize+zscore,返回 {code: z}(仅含有效值票)。

    有效样本 <2 无法标准化 → 返回空(该维退出所有票的复合,由重归一自然摊到其余维)。
    """
    present = [(c, float(v)) for c, v in pairs if _finite(v)]
    if len(present) < 2:
        return {}
    codes = [c for c, _ in present]
    zs = zscore(winsorize_med([v for _, v in present], scale))
    return {codes[i]: zs[i] for i in range(len(codes))}


# ————————————————————————————————————————————————————————————————
# 一·补 可交易性下限(附加②:剔 ST/停牌 + 成交额/流通市值下限)——纯函数,可独测
# ————————————————————————————————————————————————————————————————
def _liquidity_skip_reason(code: str, rec: dict, *, min_amount_wan: float,
                           min_mktcap_yi: float, exclude_st: bool) -> Optional[str]:
    """可交易性门:返回跳过原因字符串(None=通过)。这是"可交易性"门,不是"位置/强势"门——
    只排除无法执行的标的(ST/停牌/成交枯竭/微盘),不引入位置偏差。次新过滤在管线侧(需 kline 根数)。
    """
    if exclude_st and reversal_veto.is_st_name(code, ((rec.get("meta") or {}).get("name"))):
        return "ST"
    snap = rec.get("snapshot")
    if not snap:
        return "停牌或无快照"
    val = rec.get("valuation") or {}
    amount_yuan = snap.get("amount")
    amount_wan = amount_yuan / 1e4 if _finite(amount_yuan) else None
    # 流通市值优先,缺则用总市值兜底(可交易性近似)
    mktcap = val.get("circ_mktcap_yi")
    if not _finite(mktcap):
        mktcap = val.get("mktcap_yi")
    has_amt, has_cap = _finite(amount_wan), _finite(mktcap)
    if not has_amt and not has_cap:
        return "无流动性数据"
    if has_amt and amount_wan < min_amount_wan:
        return "低成交额"
    if has_cap and mktcap < min_mktcap_yi:
        return "低流通市值"
    return None


# ————————————————————————————————————————————————————————————————
# 二、选股策略(横截面标准化 + 缺维重归一等权复合 + 可交易性过滤 + 排序)
# ————————————————————————————————————————————————————————————————
_SCHEMA = {
    "records": "dict[code, 中心记录];每条读 record['financial']['derived'](4核心维)+ "
               "可选 record['扣非质量']['质量领先度'](管线注入的跨期均值)+ snapshot/valuation(可交易性)",
    "top_k": f"输出候选数(默认 {_TOP_K})",
    "weights": "五维权重 dict(默认等权,单一真源 THRESHOLDS['扣非质量']['权重'])",
    "winsor_scale": f"去极值 MAD 倍数(默认 {_WINSOR})",
    "apply_liquidity": "是否施加可交易性门(默认 True;回测隔离因子 IC 时可传 False)",
}


@strategy("扣非质量", "选股", params_schema=_SCHEMA)
def combo_deduct_quality_screen(
    records: dict[str, dict],
    top_k: int = _TOP_K,
    weights: Optional[dict] = None,
    winsor_scale: float = _WINSOR,
    apply_liquidity: bool = True,
    min_amount_wan: float = _MIN_AMOUNT_WAN,
    min_mktcap_yi: float = _MIN_MKTCAP_YI,
    exclude_st: bool = _LIQ_ST,
) -> dict:
    """扣非质量横截面排序主筛。

    每票读 5 维原始因子(4 核心维 read derived + 质量领先度),可交易性过滤后对每维 winsorize+zscore,
    **缺维重归一**等权复合 score = Σ(w·z)/Σ(w over 可用维),降序取 top_k。
    跳过原因(ST/停牌/低成交额/低市值/全维缺失)分类计数,诚实降级。
    某维横截面有效样本 <2 → 该维退出;有效票 <2 无法标准化 → 空 + note。⚠️ 非投资建议。
    """
    weights = dict(weights) if weights else dict(_W)
    skip: dict[str, int] = {}

    def _skip(reason: str):
        skip[reason] = skip.get(reason, 0) + 1

    # —— 一遍:可交易性过滤 + 抽五维原始因子 ——
    raw: dict[str, dict[str, Optional[float]]] = {}   # code → {维: 原始值}
    for code, rec in (records or {}).items():
        rec = rec or {}
        if apply_liquidity:
            reason = _liquidity_skip_reason(
                code, rec, min_amount_wan=min_amount_wan,
                min_mktcap_yi=min_mktcap_yi, exclude_st=exclude_st)
            if reason:
                _skip(reason)
                continue
        f = factors_of(rec)
        if all(f.get(n) is None for n in _ALL_DIM_NAMES):
            _skip("全维缺失")
            continue
        raw[code] = f

    if len(raw) < 2:
        return {"codes": [], "candidates": [], "top_k": top_k,
                "有效样本": len(raw), "跳过": skip, "因子明细": [],
                "权重": weights,
                "note": "有效样本 <2,无法做横截面标准化(全A 闭环采集后才有足量样本)"}

    # —— 二遍:逐维横截面 winsor+z ——
    zmaps: dict[str, dict[str, float]] = {}
    for dim in _ALL_DIM_NAMES:
        zmaps[dim] = _cross_z([(c, raw[c].get(dim)) for c in raw], winsor_scale)

    # —— 三遍:缺维重归一等权复合 ——
    scored: list[tuple[str, float, dict, dict]] = []   # (code, score, 原始值, z值)
    for code in raw:
        num = 0.0
        den = 0.0
        zused: dict[str, float] = {}
        for dim in _ALL_DIM_NAMES:
            z = zmaps[dim].get(code)
            if z is None:
                continue
            w = float(weights.get(dim, 1.0))
            if w == 0:
                continue
            num += w * z
            den += w
            zused[dim] = z
        if den == 0:                       # 该票所有可用维要么缺、要么权重0 → 不参与
            _skip("全维缺失")
            continue
        scored.append((code, num / den, raw[code], zused))

    if len(scored) < 2:
        return {"codes": [], "candidates": [], "top_k": top_k,
                "有效样本": len(scored), "跳过": skip, "因子明细": [],
                "权重": weights,
                "note": "可参与复合的有效票 <2(各维横截面样本不足),诚实降级"}

    ranked = sorted(scored, key=lambda x: x[1], reverse=True)
    detail = []
    for code, score, rawvals, zused in ranked:
        row = {"code": code, "综合分": round(score, 4),
               "参与维数": len(zused)}
        for dim in _ALL_DIM_NAMES:
            rv = rawvals.get(dim)
            row[dim] = round(rv, 4) if _finite(rv) else None
            zv = zused.get(dim)
            row[f"{dim}_z"] = round(zv, 4) if zv is not None else None
        detail.append(row)

    picked = [c for c, *_ in ranked][:top_k]
    return {
        "codes": picked,
        "candidates": picked,
        "top_k": top_k,
        "有效样本": len(scored),
        "跳过": skip,
        "因子明细": detail,
        "权重": weights,
        "参数": {"winsor_scale": winsor_scale, "多期N": _N_PERIODS,
                 "min_amount_wan": min_amount_wan, "min_mktcap_yi": min_mktcap_yi,
                 "剔除ST": exclude_st, "apply_liquidity": apply_liquidity},
    }
