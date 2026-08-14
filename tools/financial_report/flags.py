"""财报数值红旗规则引擎(P0)。

定位(方案 §0.5 B):**投资侧红旗过滤器**——升起疑点即降权/规避,不做审计定罪。
阈值集中在 config/strategy.py 的 THRESHOLDS["财报"]["红旗"](单一真源,占位待标定)。

每条红旗输出:{code(信号名), 命中(bool), 严重度(高/中/低), 值(命中依据的数值)}。
判定用当期衍生指标(metrics.compute_derived 的单期结果)+ 少量原始科目(如扣非绝对值)。
一切阈值缺数据 → 该条不命中(命中=False),不误杀;不抛异常。

⚠️ 非投资建议。深度红旗(八大造假反查 / 同行业相对 / 文本类)本轮不实现,见 analyzer TODO。
"""
from __future__ import annotations

from tools.config import strategy


def _cfg() -> dict:
    return strategy.THRESHOLDS.get("财报", {})


def _thr() -> dict:
    return _cfg().get("红旗", {})


def _sev(name: str) -> str:
    return _cfg().get("严重度", {}).get(name, "中")


def evaluate_flags(derived: dict, structured: dict | None = None) -> list[dict]:
    """对单期衍生指标算红旗清单。

    Args:
        derived: metrics.compute_derived 的单期结果。
        structured: 单期三大表记录(取扣非绝对值等原始科目;可 None)。
    Returns:
        list[flag];仅返回**命中**的红旗(未命中不列)。
    """
    thr = _thr()
    flags: list[dict] = []

    def hit(name: str, val: dict):
        flags.append({"code": name, "命中": True, "严重度": _sev(name), "值": val})

    营收增速 = derived.get("营收增速")
    归母增速 = derived.get("归母净利增速")
    # 增收不增利:营收增而归母降
    if 营收增速 is not None and 归母增速 is not None and 营收增速 > 0 > 归母增速:
        hit("增收不增利", {"营收增速": 营收增速, "归母净利增速": 归母增速})

    # 现金含量不足:CFO/归母净利 < 下限(且净利为正才有意义)
    cfo_ratio = derived.get("现金含量_CFO比净利")
    lo = thr.get("现金含量_CFO比净利_下限")
    if cfo_ratio is not None and lo is not None and cfo_ratio < lo:
        hit("现金含量不足", {"现金含量_CFO比净利": round(cfo_ratio, 4), "下限": lo})

    # 应收/存货激增:任一增速 − 营收增速 > 阈值
    gap = thr.get("应收存货增速超营收_pct")
    if gap is not None and 营收增速 is not None:
        for k in ("应收增速", "存货增速"):
            g = derived.get(k)
            if g is not None and (g - 营收增速) > gap:
                hit("应收存货激增", {k: g, "营收增速": 营收增速, "差值pct": round(g - 营收增速, 4)})
                break

    # 商誉高企:商誉/净资产 > 上限
    gw = derived.get("商誉占净资产")
    gw_hi = thr.get("商誉占净资产_上限_pct")
    if gw is not None and gw_hi is not None and gw > gw_hi:
        hit("商誉高企", {"商誉占净资产": gw, "上限pct": gw_hi})

    # 高负债:资产负债率 > 上限
    dar = derived.get("资产负债率")
    dar_hi = thr.get("资产负债率_上限_pct")
    if dar is not None and dar_hi is not None and dar > dar_hi:
        hit("高负债", {"资产负债率": dar, "上限pct": dar_hi})

    # 扣非占比低:扣非/归母 < 下限(归母为正时)
    kf_ratio = derived.get("扣非占归母")
    kf_lo = thr.get("扣非占归母_下限")
    if kf_ratio is not None and kf_lo is not None and kf_ratio < kf_lo:
        hit("扣非占比低", {"扣非占归母": round(kf_ratio, 4), "下限": kf_lo})

    # 短债覆盖不足:货币资金/短期有息负债 < 下限
    cov = derived.get("短债覆盖")
    cov_lo = thr.get("短债覆盖_下限")
    if cov is not None and cov_lo is not None and cov < cov_lo:
        hit("短债覆盖不足", {"短债覆盖": round(cov, 4), "下限": cov_lo})

    # 扣非为负:主业不赚钱
    if structured is not None:
        kf = structured.get("利润表", {}).get("扣非归母净利润")
        if isinstance(kf, (int, float)) and kf < 0:
            hit("扣非为负", {"扣非归母净利润": kf})

    # 毛利率异常跳升:同比绝对百分点跳升 > 阈值(需上一期毛利率,由 analyzer 注入 derived['毛利率同比升'])
    jump = derived.get("毛利率同比升")
    jump_thr = thr.get("毛利率跳升_pct")
    if jump is not None and jump_thr is not None and jump > jump_thr:
        hit("毛利率异常跳升", {"毛利率同比升pct": round(jump, 4), "阈值pct": jump_thr})

    return flags


def has_high_severity(flags: list[dict]) -> bool:
    """是否含「高」严重度红旗(评分封顶用)。"""
    return any(f.get("严重度") == "高" for f in flags)
