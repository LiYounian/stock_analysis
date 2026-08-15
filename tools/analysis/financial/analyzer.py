"""财报分析编排(P0 数值层):load raw 三大表 → 衍生指标 → 数值红旗 → 质地评分。

管线(方案 §附/落地顺序 P0~P1):
    collectors.financial 落 raw(多报告期三大表, 带披露日)
        → metrics.compute_derived(增速/质量/健康/运营/回报衍生)
        → flags.evaluate_flags(数值红旗)
        → scoring.quality_score(五维 → 0~100 + 评级)
        → analyze() 组装按 (code, 报告期) 的财报视图 + 浅度利润表摘要 + 最新期 financial 块

**无未来函数红线**:analyze/query 强制按 `disclosure_date <= as_of` 过滤可见性;
报告期结束日 ≠ 披露日,披露日之前该期不可见(方案 §2.2)。

**降级留口**:LLM 文本定性(qualitative / schema_A)与综合归纳(verdict / schema_B)本轮置 null。

—— 本轮挂起(缺 PDF/正文源,代码留 TODO)——
  * 审计闸门1(审计机构∈证监会备案名录)/ 闸门2(审计意见=标准无保留):
    OPINION_TYPE 已在采集期随年报落到 raw(rec['audit_opinion']),但**名录比对 + 拦截逻辑未实现**;
    参考 docs/参考/财报_审计机构名单与八大造假清单.md。
  * LLM 文本定性层(schema_A / MD&A 抽取)+ 综合归纳(schema_B):需财报正文源(方案 Q4)。
  * 三大表深度勾稽 + 八大造假反查 + 同行业相对分位:P2/P3(方案 §四/§5)。

⚠️ 非投资建议。所有阈值/评分为工程占位,待策略提供者标定。
"""
from __future__ import annotations

import logging

from tools.analysis.financial import flags as flags_mod
from tools.analysis.financial import metrics as metrics_mod
from tools.analysis.financial import scoring as scoring_mod
from tools.store import repo as store

logger = logging.getLogger("analysis.financial.analyzer")

# 浅度输出 & structured 摘要选取的关键科目(方案 §2.2 structured 块)
_STRUCT_PROFIT = ("营业总收入", "营业成本", "营业利润", "利润总额",
                  "净利润", "归母净利润", "扣非归母净利润", "研发费用", "基本每股收益")
_STRUCT_BALANCE = ("货币资金", "应收账款", "存货", "商誉", "资产总计", "负债合计",
                   "股东权益合计", "归母股东权益", "短期借款", "长期借款")
_STRUCT_CASHFLOW = ("经营活动现金流量净额", "投资活动现金流量净额", "筹资活动现金流量净额",
                    "销售商品提供劳务收到的现金", "购建固定资产无形资产等支付现金")


def need_llm_fields() -> list[str]:
    """计划用 LLM 从财报原文抽取的字段(schema_A,本轮未采集正文源,置 null)。"""
    return ["增长来源", "量价拆分", "在手订单", "产能与募投", "经营指引",
            "风险提示", "一次性事项", "管理层语气", "文本红旗"]


def _prev_year_period(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}"


def _struct_summary(rec: dict) -> dict:
    """从单期三大表记录抽 structured 关键科目摘要。"""
    prof = rec.get("利润表", {})
    bal = rec.get("资产负债表", {})
    cf = rec.get("现金流量表", {})
    out = {}
    out.update({k: prof.get(k) for k in _STRUCT_PROFIT})
    out.update({k: bal.get(k) for k in _STRUCT_BALANCE})
    out.update({k: cf.get(k) for k in _STRUCT_CASHFLOW})
    return out


def _profit_digest(rec: dict, derived: dict) -> dict:
    """浅度输出:利润表摘要(服务情绪/短期,方案 §0.5 C 浅度层)。"""
    prof = rec.get("利润表", {})
    return {
        "营业总收入": prof.get("营业总收入"),
        "归母净利润": prof.get("归母净利润"),
        "扣非归母净利润": prof.get("扣非归母净利润"),
        "营收增速": derived.get("营收增速"),
        "归母净利增速": derived.get("归母净利增速"),
        "扣非净利增速": derived.get("扣非净利增速"),
        "毛利率": derived.get("毛利率"),
        "净利率": derived.get("净利率"),
    }


def analyze(code: str, as_of: str | None = None, persist: bool = True) -> dict:
    """对单票做财报数值分析,产出按报告期的财报视图。

    Args:
        code: 6 位代码。
        as_of: 可见性锚(仅纳入 disclosure_date <= as_of 的报告期,防未来函数)。None=全部可见。
        persist: True 则把视图写 code_view("financial_report", code)。
    Returns:
        {code, name, as_of, periods:{period: 单期分析}, latest: 最新可见期摘要, provenance}。
    缺 raw 抛 FileNotFoundError(与 store 缺失约定一致)。
    """
    code = str(code).zfill(6)
    raw = store.get_raw("financial_report", code)          # 缺失 → FileNotFoundError
    periods_raw = raw.get("periods", {})
    derived_all = metrics_mod.compute_derived(periods_raw)

    # 注入「毛利率同比升」(供毛利率异常跳升红旗)
    for p, d in derived_all.items():
        cur_gm = d.get("毛利率")
        prev_gm = (derived_all.get(_prev_year_period(p), {}) or {}).get("毛利率")
        d["毛利率同比升"] = (round(cur_gm - prev_gm, 4)
                          if (cur_gm is not None and prev_gm is not None) else None)

    out_periods: dict[str, dict] = {}
    for p, rec in periods_raw.items():
        disc = rec.get("disclosure_date")
        if as_of is not None and disc is not None and disc > as_of:
            continue                                        # 未来函数红线:未披露不可见
        derived = derived_all.get(p, {})
        struct = _struct_summary(rec)
        flags = flags_mod.evaluate_flags(derived, rec)
        score = scoring_mod.quality_score(derived, flags)
        out_periods[p] = {
            "report_date": rec.get("report_date", p),
            "disclosure_date": disc,
            "report_type": rec.get("report_type"),
            "is_forecast": rec.get("is_forecast", False),
            "audit_opinion": rec.get("audit_opinion"),     # 采集期落的审计意见(闸门本轮不判)
            "structured": struct,
            "derived": {k: v for k, v in derived.items() if k != "毛利率同比升"},
            "flags": flags,
            "quality_score": score["quality_score"],
            "评级": score["评级"],
            "five_dims": score["five_dims"],
            "利润表摘要": _profit_digest(rec, derived),      # 浅度输出(显眼字段)
            "qualitative": None,                            # LLM 文本定性(schema_A)留口
            "verdict": None,                                # LLM 综合归纳(schema_B)留口
            "provenance": {"structured": True, "qualitative": False,
                           "source": raw.get("meta_source", "sina+em")},
        }

    latest_p = max(out_periods) if out_periods else None
    result = {
        "code": code, "name": raw.get("name"), "as_of": as_of,
        "periods": out_periods, "latest_period": latest_p,
        "latest": out_periods.get(latest_p) if latest_p else None,
        "provenance": {"structured": bool(out_periods), "qualitative": False, "verdict": False},
    }
    if persist and out_periods:
        try:
            store.put_code_view("financial_report", code, result)
        except Exception as e:                              # noqa: BLE001
            logger.warning("财报视图落盘失败 %s: %s", code, e)
    return result


def build_financial_block(code: str, as_of: str | None = None) -> dict | None:
    """构建中心记录顶层 `financial` 轻量块(仅最新已披露报告期摘要)。

    供 panel/screen/web/Agent 直接消费(不塞多期大数组;多期在 code_view)。
    无可见报告期 → None。
    """
    try:
        res = analyze(code, as_of=as_of, persist=False)
    except FileNotFoundError:
        return None
    latest = res.get("latest")
    if not latest:
        return None
    return {
        "报告期": latest["report_date"],
        "报告类型": latest.get("report_type"),
        "披露日": latest.get("disclosure_date"),
        "is_forecast": latest.get("is_forecast", False),
        "quality_score": latest.get("quality_score"),
        "评级": latest.get("评级"),
        "five_dims": latest.get("five_dims"),
        "利润表摘要": latest.get("利润表摘要"),
        "flags": [f["code"] for f in latest.get("flags", [])],   # 轻量:只列命中信号名
        "derived": latest.get("derived"),
        "verdict": None,   # LLM 层留口
    }


# —— 声明式查询接口(方案 §2.3a):按披露日锚定、条件筛票 / 字段取值 ——
_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _field_value(latest: dict, field: str):
    """从最新期分析里取字段值(derived 优先,再 structured/利润表摘要/顶层)。"""
    for bucket in ("derived", "structured", "利润表摘要"):
        d = latest.get(bucket) or {}
        if field in d:
            return d[field]
    return latest.get(field)


def query(codes: list[str], as_of: str, where: dict | None = None,
          select: list[str] | None = None) -> dict[str, dict]:
    """按报告期财报字段筛选 / 提取(强制披露日锚定,防未来函数)。

    Args:
        codes: 候选票。
        as_of: **必传**;只看 disclosure_date <= as_of 的最新已披露报告期。
        where: 条件,如 {"扣非净利增速": (">", 30), "现金含量_CFO比净利": (">", 0.8)};
               字段缺值的票**不命中**(宁缺勿错)。
        select: 要取的字段列表;None=返回该票最新期完整分析。
    Returns:
        {命中 code: {字段: 值}(select 指定)或 完整最新期分析}。
    """
    if not as_of:
        raise ValueError("query 必须传 as_of(防未来函数),不可为空")
    out: dict[str, dict] = {}
    for code in codes:
        code = str(code).zfill(6)
        try:
            res = analyze(code, as_of=as_of, persist=False)
        except FileNotFoundError:
            continue
        latest = res.get("latest")
        if not latest:
            continue
        # 条件过滤
        ok = True
        for field, cond in (where or {}).items():
            op, target = cond
            val = _field_value(latest, field)
            if val is None or op not in _OPS or not _OPS[op](val, target):
                ok = False
                break
        if not ok:
            continue
        if select is None:
            out[code] = latest
        else:
            out[code] = {f: _field_value(latest, f) for f in select}
    return out
