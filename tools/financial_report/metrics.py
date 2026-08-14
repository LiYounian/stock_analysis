"""财报衍生指标(代码计算,不经 LLM)。

输入:collectors.financial 落盘的多报告期三大表(period → {利润表,资产负债表,现金流量表})。
输出:每报告期一组衍生指标(增速/质量/健康/运营/回报)。

口径要点:
  - 季报为**累计口径**(三季报=前三季合计)。增速用**累计同比**(与去年同报告期比),稳健优先。
  - **单季**值由相邻累计相减(Q2=H1−Q1,Q3=前三季−H1,Q4=年报−前三季),供单季环比/同比;缺相邻期→None。
  - ROE 等比率用当期口径直接算(季报 ROE 未年化,做趋势/相对比较,不当年度绝对值);已注明。
  - 一切除法防 0/None → None(缺失,不用 0 冒充)。

纯函数、无网络、无状态。红旗判定在 flags.py,评分在 scoring.py。
"""
from __future__ import annotations


def _f(d: dict, *path):
    """安全取嵌套字段:_f(rec, '利润表', '营业总收入');任一层缺 → None。"""
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur if isinstance(cur, (int, float)) else None


def _div(a, b):
    """a/b;a、b 任一 None 或 b≈0 → None。"""
    if a is None or b is None or abs(b) < 1e-9:
        return None
    return a / b


def _pct(a, b):
    """(a/b)×100;供占比/率类。"""
    r = _div(a, b)
    return None if r is None else round(r * 100, 4)


def _yoy_pct(cur, prev):
    """同比增速(%):(cur−prev)/|prev|×100;prev 为 0/None → None。

    用 |prev| 作分母,使去年为负、今年改善时增速符号方向正确(亏损收窄=正增长)。
    """
    if cur is None or prev is None or abs(prev) < 1e-9:
        return None
    return round((cur - prev) / abs(prev) * 100, 4)


def _prev_year_period(period: str) -> str:
    """同报告期上一年:'2025-09-30' → '2024-09-30'。"""
    y = int(period[:4]) - 1
    return f"{y}{period[4:]}"


# —— 单季拆分:同一会计年度内相邻累计相减 ——
_PREV_CUM = {"06-30": "03-31", "09-30": "06-30", "12-31": "09-30"}


def _single_quarter(periods: dict, period: str, table: str, field: str):
    """当期单季值 = 当期累计 − 同年上一相邻报告期累计;Q1 单季=累计;缺相邻期→None。"""
    cur = _f(periods.get(period, {}), table, field)
    if cur is None:
        return None
    mmdd = period[5:10]
    if mmdd == "03-31":
        return cur
    prev_mmdd = _PREV_CUM.get(mmdd)
    if prev_mmdd is None:
        return None
    prev_period = f"{period[:4]}-{prev_mmdd}"
    prev = _f(periods.get(prev_period, {}), table, field)
    return None if prev is None else round(cur - prev, 4)


def compute_derived(periods: dict) -> dict[str, dict]:
    """对每个报告期算衍生指标,返回 {period: derived_dict}。

    periods: {period(YYYY-MM-DD): 单期三大表记录}。
    """
    out: dict[str, dict] = {}
    for p, rec in periods.items():
        prev_p = _prev_year_period(p)
        prev = periods.get(prev_p, {})

        营收 = _f(rec, "利润表", "营业总收入")
        营业成本 = _f(rec, "利润表", "营业成本")
        归母 = _f(rec, "利润表", "归母净利润")
        扣非 = _f(rec, "利润表", "扣非归母净利润")
        净利润 = _f(rec, "利润表", "净利润")
        研发 = _f(rec, "利润表", "研发费用")

        净资产 = _f(rec, "资产负债表", "股东权益合计")
        归母净资产 = _f(rec, "资产负债表", "归母股东权益")
        总资产 = _f(rec, "资产负债表", "资产总计")
        负债 = _f(rec, "资产负债表", "负债合计")
        商誉 = _f(rec, "资产负债表", "商誉")
        应收 = _f(rec, "资产负债表", "应收账款") or _f(rec, "资产负债表", "应收票据及应收账款")
        存货 = _f(rec, "资产负债表", "存货")
        货币资金 = _f(rec, "资产负债表", "货币资金")
        短期借款 = _f(rec, "资产负债表", "短期借款")
        一年内到期 = _f(rec, "资产负债表", "一年内到期非流动负债")
        长期借款 = _f(rec, "资产负债表", "长期借款")
        应付债券 = _f(rec, "资产负债表", "应付债券")

        CFO = _f(rec, "现金流量表", "经营活动现金流量净额")
        capex = _f(rec, "现金流量表", "购建固定资产无形资产等支付现金")

        # 有息负债 = 短期借款 + 一年内到期 + 长期借款 + 应付债券(缺项按 0 计入,全缺→None)
        有息_parts = [x for x in (短期借款, 一年内到期, 长期借款, 应付债券) if x is not None]
        有息负债 = round(sum(有息_parts), 4) if 有息_parts else None
        # 短期有息负债(短债覆盖用)
        短期有息_parts = [x for x in (短期借款, 一年内到期) if x is not None]
        短期有息负债 = round(sum(短期有息_parts), 4) if 短期有息_parts else None

        d = {
            # 成长
            "营收增速": _yoy_pct(营收, _f(prev, "利润表", "营业总收入")),
            "归母净利增速": _yoy_pct(归母, _f(prev, "利润表", "归母净利润")),
            "扣非净利增速": _yoy_pct(扣非, _f(prev, "利润表", "扣非归母净利润")),
            # 盈利质量
            "毛利率": _pct((营收 - 营业成本) if (营收 is not None and 营业成本 is not None) else None, 营收),
            "净利率": _pct(归母, 营收),
            "扣非占归母": _div(扣非, 归母),
            "现金含量_CFO比净利": _div(CFO, 归母),
            # 财务健康
            "资产负债率": _pct(负债, 总资产),
            "有息负债": 有息负债,
            "短债覆盖": _div(货币资金, 短期有息负债),   # >1 货币资金可覆盖短期有息负债
            "商誉占净资产": _pct(商誉, 归母净资产 or 净资产),
            # 运营(周转天数用累计口径,季报未年化,做趋势/相对比较)
            "应收周转天数": _div((应收 * _period_days(p)) if 应收 is not None else None, 营收),
            "存货周转天数": _div((存货 * _period_days(p)) if 存货 is not None else None, 营业成本),
            "应收增速": _yoy_pct(应收, _f(prev, "资产负债表", "应收账款")
                              or _f(prev, "资产负债表", "应收票据及应收账款")),
            "存货增速": _yoy_pct(存货, _f(prev, "资产负债表", "存货")),
            # 回报
            "ROE": _pct(归母, 归母净资产 or 净资产),   # 当期口径(季报未年化),供趋势/相对
            # 现金流结构(资金来源分析)
            "自由现金流": (round(CFO - capex, 4) if (CFO is not None and capex is not None) else None),
            "研发费用率": _pct(研发, 营收),
            # 单季(供环比/单季同比;缺相邻期→None)
            "单季营收": _single_quarter(periods, p, "利润表", "营业总收入"),
            "单季归母净利": _single_quarter(periods, p, "利润表", "归母净利润"),
        }
        # 单季环比(vs 同年上一相邻报告期的单季)
        d["单季营收环比"] = _single_quarter_qoq(periods, p, "利润表", "营业总收入")
        out[p] = d
    return out


def _period_days(period: str) -> int:
    """报告期累计对应的天数(用于周转天数的当期口径换算)。"""
    return {"03-31": 90, "06-30": 180, "09-30": 270, "12-31": 360}.get(period[5:10], 360)


def _single_quarter_qoq(periods: dict, period: str, table: str, field: str):
    """单季环比(%):当期单季 vs 同年上一相邻报告期单季。"""
    cur_sq = _single_quarter(periods, period, table, field)
    mmdd = period[5:10]
    prev_mmdd = _PREV_CUM.get(mmdd)
    if cur_sq is None or prev_mmdd is None:
        return None
    prev_period = f"{period[:4]}-{prev_mmdd}"
    prev_sq = _single_quarter(periods, prev_period, table, field)
    return _yoy_pct(cur_sq, prev_sq)
