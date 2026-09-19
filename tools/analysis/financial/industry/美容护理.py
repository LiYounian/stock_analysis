"""美容护理(化妆品/个护/医美)财报专家(轻量版·P0 数值层)。

⚠️ 非投资建议;阈值/区间/权重均为**工程占位**,注释注明"待标定"。

行业口径:
- 高毛利、品牌溢价:化妆品/医美毛利率常 60%+ → 毛利率区间上调,ROE 中枢高。
- 重营销轻资产:销售费用率极高(线上投流/达人营销),营销吞噬利润是本行业最典型隐患。
- 命门:销售费用率畸高(增收不增利的根源)、渠道压货(存货/应收随大促膨胀)。

只写本文件;不改共享文件。缺通用指标(销售费用率已在 metrics,亦可用 structured 自算)。
纯函数、无网络、无状态;缺值→None/不命中,不抛异常。
"""
from __future__ import annotations

KEY = "美容护理"
NOTE = ("化妆品/个护/医美口径:高毛利品牌溢价(毛利率区间上调、ROE中枢高)、重营销轻资产;"
        "重点看 销售费用率畸高(营销吞噬利润)、渠道压货(存货/应收随大促膨胀)。阈值工程占位待标定。")


def dimension_specs() -> dict:
    """拷通用默认再改目标区间:毛利率上调(高毛利)、ROE上调(品牌高回报)。工程占位待标定。"""
    return {
        "成长": [("营收增速%", "营收增速", -20, 45),
               ("扣非净利增速%", "扣非净利增速", -20, 50)],
        # 质量:化妆品/医美高毛利,毛利率区间上调(下界 20、100分端 70)【待标定】
        "质量": [("现金含量 CFO/归母净利", "现金含量_CFO比净利", 0, 1.2),
               ("扣非占归母", "扣非占归母", 0, 1.0),
               ("毛利率%", "毛利率", 20, 70)],
        "健康": [("资产负债率%(反向)", "资产负债率", 80, 30),
               ("短债覆盖", "短债覆盖", 0, 2),
               ("商誉占净资产%(反向)", "商誉占净资产", 50, 0)],
        "运营": [("应收周转天数(反向)", "应收周转天数", 150, 20),
               ("存货周转天数(反向)", "存货周转天数", 360, 60)],
        # 回报:品牌高回报,ROE 100分端上调到 25【待标定】
        "回报": [("ROE%", "ROE", 0, 25)],
    }


def weights() -> dict | None:
    return None


SKIP_FLAGS: list[str] = []


# ── 专属红旗阈值(工程占位,待标定)────────────────────────────
_SALES_EXP_HI_PCT = 45.0   # 销售费用/营业总收入 超此(%)→ 营销吞噬利润(重投流,增收不增利根源)
_INV_GAP_PCT = 25.0        # 存货增速−营收增速 超此(pct)且存货增长→ 大促后渠道压货/滞销


def _num(rec, *path):
    cur = rec
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, (int, float)) else None


def extra_flags(derived: dict, structured: dict) -> list[dict]:
    """美容护理专属红旗。仅 命中=True 者并入;缺值不命中不抛异常。阈值工程占位待标定。"""
    derived = derived or {}
    structured = structured if isinstance(structured, dict) else {}
    out: list[dict] = []

    def emit(code, sev, val):
        out.append({"code": code, "命中": True, "严重度": sev, "值": val})

    # 1) 销售费用率畸高(营销吞噬利润):优先用派生研发/销售率不可得,则用 structured 自算 销售费用/营收。
    销售费用 = _num(structured, "利润表", "销售费用")
    营收 = _num(structured, "利润表", "营业总收入") or _num(structured, "利润表", "营业收入")
    if 销售费用 is not None and 营收 is not None and 营收 > 0:
        rate = 销售费用 / 营收 * 100
        if rate > _SALES_EXP_HI_PCT:
            emit("销售费用率畸高", "中", {"销售费用率%": round(rate, 2), "阈值pct": _SALES_EXP_HI_PCT,
                                  "NOTE": "重投流/达人营销吞噬利润,增收不增利根源"})

    # 2) 渠道压货:存货增速显著超营收增速(大促后向渠道压库存或动销不及预期)。
    inv, rev = derived.get("存货增速"), derived.get("营收增速")
    if inv is not None and rev is not None and inv > 0 and (inv - rev) > _INV_GAP_PCT:
        emit("渠道压货", "中", {"存货增速": inv, "营收增速": rev,
                            "差值pct": round(inv - rev, 4), "阈值pct": _INV_GAP_PCT})

    return out
