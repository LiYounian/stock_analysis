"""环保(水处理/固废/大气治理/环保设备)财报专家(轻量版·P0 数值层)。

⚠️ 非投资建议;阈值/区间/权重均为**工程占位**,注释注明"待标定"。

行业口径:
- 重资产 PPP/BOT 模式:项目投资大、靠融资 → 资产负债率高;通用『高负债』系统性误杀 → SKIP,健康维放宽。
- 应收命门:政府/公用客户回款慢、国补/运营费拖欠 → 应收与合同资产巨大、回款周期长、现金流偏紧。
- 命门:应收随营收膨胀(回款恶化)+ 经营现金流为负=PPP 模式资金链风险(环保企业暴雷典型)。

只写本文件;不改共享文件。纯函数、无网络、无状态;缺值→None/不命中,不抛异常。
"""
from __future__ import annotations

KEY = "环保"
NOTE = ("水处理/固废/环保工程口径:重资产PPP/BOT高杠杆(『高负债』已 SKIP、资产负债率反向区间放宽)、"
        "政府客户回款慢(应收周转天数大幅放宽);命门=应收膨胀(回款恶化)+经营现金流为负。阈值工程占位待标定。")


def dimension_specs() -> dict:
    """拷通用默认再改目标区间:资产负债率放宽(高杠杆)、应收周转大幅放宽(政府回款慢)、现金含量放宽。工程占位待标定。"""
    return {
        "成长": [("营收增速%", "营收增速", -20, 40),
               ("扣非净利增速%", "扣非净利增速", -20, 40)],
        # 质量:PPP回款慢扰动 CFO,现金含量下界放宽到负值【待标定】
        "质量": [("现金含量 CFO/归母净利", "现金含量_CFO比净利", -0.5, 1.0),
               ("扣非占归母", "扣非占归母", 0, 1.0),
               ("毛利率%", "毛利率", 0, 45)],
        # 健康:PPP/BOT 高杠杆,资产负债率反向区间放宽【待标定】
        "健康": [("资产负债率%(反向)", "资产负债率", 82, 45),
               ("短债覆盖", "短债覆盖", 0, 1.5),
               ("商誉占净资产%(反向)", "商誉占净资产", 50, 0)],
        # 运营:政府/公用客户回款慢,应收周转天数大幅放宽【待标定】
        "运营": [("应收周转天数(反向)", "应收周转天数", 300, 60),
               ("存货周转天数(反向)", "存货周转天数", 360, 60)],
        "回报": [("ROE%", "ROE", 0, 15)],
    }


def weights() -> dict | None:
    return None


# 高杠杆属环保 PPP/BOT 行业特性(项目投资靠融资),通用『高负债』系统性误杀 → SKIP。
SKIP_FLAGS = ["高负债"]


# ── 专属红旗阈值(工程占位,待标定)────────────────────────────
_AR_GAP_PCT = 20.0     # 应收增速−营收增速 超此(pct)→ 政府/国补回款恶化(应收/合同资产滚大)


def _num(rec, *path):
    cur = rec
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, (int, float)) else None


def extra_flags(derived: dict, structured: dict) -> list[dict]:
    """环保专属红旗。仅 命中=True 者并入;缺值不命中不抛异常。阈值工程占位待标定。"""
    derived = derived or {}
    out: list[dict] = []

    def emit(code, sev, val):
        out.append({"code": code, "命中": True, "严重度": sev, "值": val})

    # 1) 应收膨胀(政府/国补回款恶化):应收增速远超营收增速。
    _ar = derived.get("应收营收增速差")
    if _ar is None:
        a, r = derived.get("应收增速"), derived.get("营收增速")
        _ar = (a - r) if (a is not None and r is not None) else None
    if _ar is not None and _ar > _AR_GAP_PCT:
        emit("应收膨胀", "中", {"应收营收增速差pct": round(_ar, 4), "阈值pct": _AR_GAP_PCT,
                            "NOTE": "政府/公用客户回款慢,应收异常膨胀,回款恶化"})

    # 2) 经营现金流为负(PPP 资金链风险):账面盈利却经营现金净流出。
    归母净利 = _num(structured, "利润表", "归母净利润")
    CFO = _num(structured, "现金流量表", "经营活动现金流量净额")
    if 归母净利 is not None and 归母净利 > 0 and CFO is not None and CFO < 0:
        emit("经营现金流为负", "中", {"归母净利润": 归母净利, "经营活动现金流量净额": CFO,
                              "NOTE": "账面盈利却经营现金净流出,叠加高杠杆需警惕 PPP 资金链"})

    return out
