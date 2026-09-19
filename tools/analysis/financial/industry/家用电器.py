"""家用电器(白电/黑电/小家电/厨电)财报专家(轻量版·P0 数值层)。

⚠️ 非投资建议;阈值/区间/权重均为**工程占位**,注释注明"待标定"。

行业口径:
- 龙头盈利稳健、渠道占款(应付>应收)、现金流强、ROE 中枢高(白电龙头 ROE 常 20%+) → 回报区间上调。
- 毛利率中枢中等(20~40),受大宗原材料(铜/铝/钢/塑料)成本波动挤压。
- 命门:渠道压货(应收/存货随出货膨胀)与原材料成本侵蚀毛利。

只写本文件;不改共享文件。纯函数、无网络、无状态;缺值→None/不命中,不抛异常。
"""
from __future__ import annotations

KEY = "家用电器"
NOTE = ("家电口径:龙头盈利稳健、现金流强、ROE中枢高(回报区间上调);毛利率中枢中等、受大宗原材料挤压;"
        "重点看 渠道压货(应收/存货膨胀)、原材料侵蚀毛利。阈值工程占位待标定。")


def dimension_specs() -> dict:
    """拷通用默认再改目标区间:回报上调(龙头高ROE)、毛利率中枢上移。工程占位待标定。"""
    return {
        "成长": [("营收增速%", "营收增速", -20, 40),
               ("扣非净利增速%", "扣非净利增速", -20, 40)],
        # 质量:毛利率中枢 10~40(区别于低毛利零售、高毛利白酒)【待标定】
        "质量": [("现金含量 CFO/归母净利", "现金含量_CFO比净利", 0, 1.2),
               ("扣非占归母", "扣非占归母", 0, 1.0),
               ("毛利率%", "毛利率", 10, 40)],
        "健康": [("资产负债率%(反向)", "资产负债率", 80, 30),
               ("短债覆盖", "短债覆盖", 0, 2),
               ("商誉占净资产%(反向)", "商誉占净资产", 50, 0)],
        "运营": [("应收周转天数(反向)", "应收周转天数", 180, 30),
               ("存货周转天数(反向)", "存货周转天数", 360, 60)],
        # 回报:白电龙头 ROE 中枢高,100分端上调到 25【待标定】
        "回报": [("ROE%", "ROE", 0, 25)],
    }


def weights() -> dict | None:
    return None


SKIP_FLAGS: list[str] = []


# ── 专属红旗阈值(工程占位,待标定)────────────────────────────
_CHANNEL_GAP_PCT = 20.0   # 应收/存货增速−营收增速 超此(pct)→ 渠道压货(向经销商压库存冲收入)
_GM_DROP_PCT = -3.0       # 毛利率同比降幅达此(百分点)→ 原材料(铜/铝/钢)成本侵蚀毛利


def extra_flags(derived: dict, structured: dict) -> list[dict]:
    """家电专属红旗。仅 命中=True 者并入;缺值不命中不抛异常。阈值工程占位待标定。"""
    derived = derived or {}
    out: list[dict] = []

    def emit(code, sev, val):
        out.append({"code": code, "命中": True, "严重度": sev, "值": val})

    # 1) 渠道压货:应收或存货增速显著超营收增速(向渠道压库存虚增出货)。
    rev = derived.get("营收增速")
    inv = derived.get("存货增速")
    ar = derived.get("应收增速")
    _ar_gap = derived.get("应收营收增速差")
    if _ar_gap is None and ar is not None and rev is not None:
        _ar_gap = ar - rev
    _inv_gap = (inv - rev) if (inv is not None and rev is not None) else None
    命中项 = {}
    if _ar_gap is not None and _ar_gap > _CHANNEL_GAP_PCT:
        命中项["应收营收增速差pct"] = round(_ar_gap, 4)
    if _inv_gap is not None and inv is not None and inv > 0 and _inv_gap > _CHANNEL_GAP_PCT:
        命中项["存货营收增速差pct"] = round(_inv_gap, 4)
    if 命中项:
        命中项["阈值pct"] = _CHANNEL_GAP_PCT
        emit("渠道压货", "中", 命中项)

    # 2) 原材料侵蚀毛利:毛利率同比降幅达阈(大宗成本上行,家电利润承压)。
    gm = derived.get("毛利率同比升")
    if gm is not None and gm <= _GM_DROP_PCT:
        emit("原材料侵蚀毛利", "中", {"毛利率同比升pct": round(gm, 4), "阈值pct": _GM_DROP_PCT})

    return out
