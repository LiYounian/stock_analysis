"""半导体多因子选股(移植自聚宽社区「[半导体板块多因子策略](https://www.joinquant.com/post/63497)」——zyyoyo)。

原脚本核心 = **申万二级半导体池(801081)** + 3 因子(rd/rev、rd/mcap、营收增速)
winsor+zscore 加权 + 每 20 交易日调仓 + 涨跌停限价买卖。持仓管理/调仓/限价单 =
回测器/主观决策的职责(见 8650081 剥离规矩),这里只提炼可分析层复用的选股。

    组合选股(1 个,面向半导体池):
      策略E_半导体多因子:
        1) 限池 = 传入 records ∩ config/semi_universe.json(申万二级 801081 半导体 178 只)
           缺池文件时降级为不限池(与策略C 同款语义)
        2) 因子提取(每票):
             rd_rev  = financial.derived["研发费用率"] / 100
             rd_mcap = (rd_rev × 营收) / (mktcap_yi × 1e8)
             rev_yoy = financial.derived["营收增速"] / 100
             其中 营收 = financial.利润表摘要["营业总收入"] 或 fundamental["营收"]
        3) 每因子 winsorize_med(scale=3,中位数±3×MAD) → 标准化(z-score)
        4) 综合分 = rd_rev_z × 0.6 + rd_mcap_z × 0.2 + rev_yoy_z × 0.2(原脚本权重)
        5) 剥离触涨跌停(|pct_chg| ≥ 9.7)/ 停牌(snapshot 缺失)
        6) 按综合分降序取 top_k

    差异(与原脚本相比,已剥离):
      · 每 20 交易日调仓 / 限价买卖 / 新股 <160 天过滤 —— 回测器/主观决策职责,不搬。
"""
from __future__ import annotations

import json
from statistics import median

from tools.config import settings
from tools.strategy.registry import strategy

# 原脚本 3 因子权重(rd/rev=0.6, rd/mcap=0.2, 营收增速=0.2)
_W_RD_REV = 0.6
_W_RD_MCAP = 0.2
_W_REV_YOY = 0.2

_WINSOR_SCALE = 3.0                  # winsorize_med(scale=3):中位数 ± 3×MAD
_LIMIT_PCT_THRESHOLD = 9.7           # 与策略C/D 同款:|pct_chg|≥9.7% 视为触板

_UNIVERSE_PATH = settings.PROJECT_ROOT / "config" / "semi_universe.json"


def _load_universe() -> set[str]:
    """申万二级 半导体池(801081)178 只;缺文件 → 空 set(降级为不限池)。"""
    try:
        codes = json.loads(_UNIVERSE_PATH.read_text("utf-8"))
        return set(codes) if isinstance(codes, list) else set()
    except FileNotFoundError:
        return set()


def _winsorize_med(values: list[float], scale: float = _WINSOR_SCALE) -> list[float]:
    """中位数 ± scale × MAD 截断(jqfactor.winsorize_med 等价实现)。空/全同值原样返回。"""
    if not values:
        return values
    med = median(values)
    devs = [abs(v - med) for v in values]
    mad = median(devs)
    if mad == 0:
        return list(values)
    lo, hi = med - scale * mad, med + scale * mad
    return [min(max(v, lo), hi) for v in values]


def _zscore(values: list[float]) -> list[float]:
    """z-score 标准化(jqfactor.standardlize 等价:(x - mean) / std)。空/全同值 → 全 0.0。"""
    if not values:
        return values
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    std = var ** 0.5
    if std == 0:
        return [0.0] * len(values)
    return [(v - mean) / std for v in values]


def _extract_factors(rec: dict) -> tuple[float, float, float] | None:
    """从中心记录抽 3 因子:(rd/rev, rd/mcap, rev_yoy);任一缺失 → None(剔)。"""
    fin = rec.get("financial") or {}
    val = rec.get("valuation") or {}
    derived = fin.get("derived") or {}
    profit_summary = fin.get("利润表摘要") or {}
    fundamental = rec.get("fundamental") or {}

    rd_pct = derived.get("研发费用率")                              # % 制
    rev_yoy_pct = derived.get("营收增速")                            # % 制
    营收 = profit_summary.get("营业总收入") or fundamental.get("营收")
    mktcap_yi = val.get("mktcap_yi")

    if not all(isinstance(v, (int, float)) and v is not None
               for v in (rd_pct, rev_yoy_pct, 营收, mktcap_yi)):
        return None
    if rd_pct <= 0 or 营收 <= 0 or mktcap_yi <= 0:
        return None

    rd_rev = rd_pct / 100.0
    rd = rd_rev * float(营收)
    rd_mcap = rd / (float(mktcap_yi) * 1e8)                        # mktcap 单位:亿元
    rev_yoy = float(rev_yoy_pct) / 100.0
    return float(rd_rev), float(rd_mcap), float(rev_yoy)


def _pass_business_filters(rec: dict) -> bool:
    """业务过滤(与策略D 同款):snapshot 存在 + |pct_chg|<9.7。"""
    snap = (rec or {}).get("snapshot")
    if not snap:
        return False
    pct = snap.get("pct_chg")
    if isinstance(pct, (int, float)) and abs(pct) >= _LIMIT_PCT_THRESHOLD:
        return False
    return True


@strategy(
    "策略E_半导体多因子", "选股",
    params_schema={
        "records": "dict[code, 中心记录]",
        "top_k": "目标持仓数(默认 8,原脚本 g.stocknum=8)",
    },
)
def combo_semi_factor_screen(records: dict[str, dict], top_k: int = 8) -> dict:
    """半导体多因子(策略E):申万二级 半导体池 178 只 + 3 因子加权打分排序。

    输出结构:{codes, candidates, top_k, 因子明细, universe_size}。数据缺失/触涨跌停/
    停牌 静默剔除。records ∩ 半导体池 = 空时返回空 + note(诚实降级)。
    """
    universe = _load_universe()

    scoped: list[tuple[str, tuple[float, float, float]]] = []
    for code, rec in (records or {}).items():
        if universe and code not in universe:
            continue
        if not _pass_business_filters(rec):
            continue
        factors = _extract_factors(rec)
        if factors is None:
            continue
        scoped.append((code, factors))

    if len(scoped) < 2:                                             # 少于 2 只无法标准化
        note = ("records ∩ 半导体池 样本 <2,无法做横截面标准化;"
                "本机 records 通常只覆盖自选池,半导体票需远端全A 闭环采集后才有数据")
        return {"codes": [], "candidates": [], "top_k": top_k,
                "因子明细": [], "monthly_pool_size": len(scoped),
                "universe_size": len(universe), "note": note}

    codes = [c for c, _ in scoped]
    rd_rev = [f[0] for _, f in scoped]
    rd_mcap = [f[1] for _, f in scoped]
    rev_yoy = [f[2] for _, f in scoped]

    rd_rev_z = _zscore(_winsorize_med(rd_rev))
    rd_mcap_z = _zscore(_winsorize_med(rd_mcap))
    rev_yoy_z = _zscore(_winsorize_med(rev_yoy))

    scores = [rd_rev_z[i] * _W_RD_REV + rd_mcap_z[i] * _W_RD_MCAP
              + rev_yoy_z[i] * _W_REV_YOY for i in range(len(codes))]

    ranked = sorted(zip(codes, scores, rd_rev, rd_mcap, rev_yoy,
                        rd_rev_z, rd_mcap_z, rev_yoy_z),
                    key=lambda x: x[1], reverse=True)

    detail = [{
        "code": c, "综合分": round(s, 4),
        "rd_rev": round(rr, 4), "rd_mcap": round(rm, 6), "rev_yoy": round(ry, 4),
        "rd_rev_z": round(rrz, 4), "rd_mcap_z": round(rmz, 4), "rev_yoy_z": round(ryz, 4),
    } for c, s, rr, rm, ry, rrz, rmz, ryz in ranked]

    picked = [c for c, *_ in ranked[:top_k]]
    return {
        "codes": picked,
        "candidates": picked,
        "top_k": top_k,
        "monthly_pool_size": len(scoped),
        "universe_size": len(universe),
        "因子明细": detail,
        "权重": {"rd_rev": _W_RD_REV, "rd_mcap": _W_RD_MCAP, "rev_yoy": _W_REV_YOY},
    }
