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
from tools.analysis.financial.industry import get_expert
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


def _is_financial(code: str, industry: str | None = None) -> bool:
    """该票是否金融业(银行/非银)——用于红旗金融业特判。

    行业口径:优先用传入的 industry(如 record.meta.industry),否则回退 board.board_of(code)
    (证监会门类),统一经 industry_map 对齐到申万一级,命中 THRESHOLDS['财报']['金融业申万'] 即金融业。
    任何一步失败 → False(不特判,退回通用红旗;宁可不特判也不误伤非金融)。
    """
    from tools.analysis import industry_map
    fin_set = set(_cfg().get("金融业申万", ["银行", "非银金融"]))
    sw = industry_map.to_sw(industry) if industry else None
    if sw is None:
        try:
            from tools.collectors import board
            sw = industry_map.to_sw(board.board_of(code) or "")
        except Exception:                                   # noqa: BLE001
            sw = None
    return sw in fin_set


_SEMI_UNIVERSE_CACHE: set[str] | None = None


def _in_semi_universe(code: str) -> bool:
    """该票是否在申万二级 801081 半导体池(config/semi_universe.json,178 只)。

    背景:仓库 board_membership(个股→板块)成分数据在本机所有 akshare 接口下均不可用,
    故 board.board_of(code) 对半导体票恒 None,证监会门类又把 fabless 芯片设计公司误判成
    「计算机/软件」(C39/I64)。半导体行业归属**以申万二级 801081 成分为准**(见
    collectors/semi_universe.py),命中即可靠地对齐到申万一级「电子」,不依赖缺失的 board 数据。
    文件缺失/读取失败 → False(不误路由)。结果缓存,避免逐票读盘。
    """
    global _SEMI_UNIVERSE_CACHE
    if _SEMI_UNIVERSE_CACHE is None:
        try:
            from tools.collectors import semi_universe
            _SEMI_UNIVERSE_CACHE = set(semi_universe.load())
        except Exception:                                   # noqa: BLE001
            _SEMI_UNIVERSE_CACHE = set()
    return code in _SEMI_UNIVERSE_CACHE


def _industry_key(code: str, industry: str | None = None) -> str | None:
    """解析该票申万一级行业名(行业财报专家路由用)。

    优先用传入 industry(record.meta.industry),否则回退 board.board_of(code)(证监会门类),
    统一经 industry_map 对齐到申万一级。两者皆缺时,再以申万二级 801081 半导体池成分兜底
    →「电子」(修 board_membership 数据缺导致 fabless 半导体从不命中电子专家的路由 bug)。
    全部失败 → None(退回通用兜底,不误路由)。
    """
    from tools.analysis import industry_map
    sw = industry_map.to_sw(industry) if industry else None
    if sw is None:
        try:
            from tools.collectors import board
            sw = industry_map.to_sw(board.board_of(code) or "")
        except Exception:                                   # noqa: BLE001
            sw = None
    if sw is None and _in_semi_universe(code):              # 半导体池成分兜底 → 电子(申万 801081→电子)
        sw = "电子"
    return sw


def _route_disambig(code: str, as_of: str | None, industry: str | None,
                    sector: str | None) -> dict:
    """行业归属消歧 + 多路由聚合(见 disambiguate.disambiguate)。

    产出:消歧结论 dis + `路由行业`(每个候选申万一级是否命中专家)+ `口径注解`(模糊时的口径说明,
    非模糊 → None,保证单行业行为不变)。**主行业 scoring 仍由 `_industry_key` 决定(向后兼容)**;
    本层只加候选/标注,不改主评分路径。消歧失败(数据缺)整体降级为空标注,不阻断财报分析。
    """
    from tools.analysis.financial import disambiguate as dis_mod
    try:
        dis = dis_mod.disambiguate(code, as_of=as_of, industry=industry, sector=sector)
    except Exception as e:                                  # noqa: BLE001
        logger.warning("行业消歧失败 %s: %s(降级为无标注)", code, e)
        return {"消歧": None, "相关行业": [], "路由行业": [], "口径注解": None}
    routes = [{"行业": c, "命中专家": get_expert(c) is not None} for c in dis["candidates"]]
    注解 = None
    if dis["ambiguous"]:
        seg = [dis["ambiguity_reason"]]
        if dis.get("market_label"):
            seg.append(dis["market_label"])
        seg.append("模糊:候选 " + "/".join(dis["candidates"]) + ",主口径按下方 行业专家 评分,余口径供多视角参考")
        注解 = " | ".join(s for s in seg if s)
    return {"消歧": dis, "相关行业": dis["candidates"], "路由行业": routes, "口径注解": 注解}


def _is_financial_structural(periods_raw: dict) -> bool:
    """结构兜底(行业名解析不到时):有资产负债表数据、但**无'营业成本'且无'存货'** → 银行/保险/证券。
    银行等金融业利润表无营业成本行、资产负债表无存货,是稳定可判信号(不依赖会员/行业数据加载)。"""
    if not periods_raw:
        return False
    latest = periods_raw.get(max(periods_raw)) or {}
    lp = latest.get("利润表") or {}
    bs = latest.get("资产负债表") or {}
    return bool(bs.get("资产总计") is not None
                and lp.get("营业成本") is None and bs.get("存货") is None)


def _cfg() -> dict:
    from tools.config import strategy
    return strategy.THRESHOLDS.get("财报", {})


def _funding_mode(cfo, cfi, cff) -> str | None:
    """现金流三段 → 造血模式(方案 §2.3b:靠经营/靠融资/靠变卖)。"""
    if cfo is None:
        return None
    if cfo > 0:
        s = "经营造血(CFO为正)"
        if isinstance(cfi, (int, float)) and cfi < 0:
            s += " · 投资扩张"
        if isinstance(cff, (int, float)):
            s += " · 净融资" if cff > 0 else " · 净还债/分红"
        return s
    if isinstance(cff, (int, float)) and cff > 0:
        return "靠融资输血(CFO≤0,筹资净流入)"
    if isinstance(cfi, (int, float)) and cfi > 0:
        return "靠变卖资产(CFO≤0,投资净流入)"
    return "造血不足(CFO≤0)"


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


def analyze(code: str, as_of: str | None = None, persist: bool = True,
            industry: str | None = None, sector: str | None = None) -> dict:
    """对单票做财报数值分析,产出按报告期的财报视图。

    Args:
        code: 6 位代码。
        as_of: 可见性锚(仅纳入 disclosure_date <= as_of 的报告期,防未来函数)。None=全部可见。
        persist: True 则把视图写 code_view("financial_report", code)。
        industry: 可选行业名(如 record.meta.industry);用于金融业红旗特判,缺省回退 board.board_of。
        sector: 可选市场大类板块(record.meta.sector);仅用于行业消歧的「市场概念」标注,缺省回退自选池。
    Returns:
        {code, name, as_of, periods:{period: 单期分析}, latest: 最新可见期摘要, provenance}。
    缺 raw 抛 FileNotFoundError(与 store 缺失约定一致)。
    """
    code = str(code).zfill(6)
    raw = store.get_raw("financial_report", code)          # 缺失 → FileNotFoundError
    periods_raw = raw.get("periods", {})
    # 金融业判定:行业名(池/board)优先,结构信号(无营业成本+无存货)兜底,任一命中即金融业
    is_fin = _is_financial(code, industry) or _is_financial_structural(periods_raw)
    # 行业财报专家路由:命中 → 用其五维区间/权重/跳过红旗/专属红旗;无 → 通用兜底
    key = _industry_key(code, industry)
    exp = get_expert(key)
    exp_specs = exp.dimension_specs() if exp else None
    exp_weights = exp.weights() if exp else None
    exp_skip = getattr(exp, "SKIP_FLAGS", None) if exp else None
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
        exp_extra = exp.extra_flags(derived, rec) if exp else None
        # 行业专家「动态跳过」(可选钩子):按当期指标条件豁免通用红旗(如成长期未盈利半导体
        # 豁免"扣非为负/现金含量不足"高危封顶)。与静态 SKIP_FLAGS 并集;缺钩子 → 只用静态。
        skip_p = list(exp_skip) if exp_skip else []
        if exp and hasattr(exp, "dynamic_skip"):
            try:
                skip_p += list(exp.dynamic_skip(derived, rec) or [])
            except Exception:                               # noqa: BLE001
                pass
        flags = flags_mod.evaluate_flags(derived, rec, is_financial=is_fin,
                                         skip=(skip_p or None), extra=exp_extra)
        score = scoring_mod.quality_score(derived, flags, specs=exp_specs, weights=exp_weights)
        out_periods[p] = {
            "report_date": rec.get("report_date", p),
            "disclosure_date": disc,
            "report_type": rec.get("report_type"),
            "is_forecast": rec.get("is_forecast", False),
            "audit_opinion": rec.get("audit_opinion"),     # 采集期落的审计意见(闸门本轮不判)
            "金融业口径": is_fin,                            # True=已按金融业跳过不适用红旗(高负债等)
            "行业专家": key if exp else None,                # 命中的行业专家 KEY(无=通用兜底)
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

    # 行业归属消歧(时变正式行业 / 市场概念标注 / 多行业模糊标记 + 多路由聚合)。
    # 主行业 scoring 仍由上面 key(_industry_key)决定,本层只加候选与标注,单行业行为不变。
    route = _route_disambig(code, as_of, industry, sector)

    latest_p = max(out_periods) if out_periods else None
    result = {
        "code": code, "name": raw.get("name"), "as_of": as_of,
        "periods": out_periods, "latest_period": latest_p,
        "latest": out_periods.get(latest_p) if latest_p else None,
        "行业专家": key if exp else None,
        "口径说明": getattr(exp, "NOTE", None) if exp else None,
        "消歧": route["消歧"],                # 三口径消歧结论(时变正式/市场概念/细分主业 + candidates)
        "相关行业": route["相关行业"],          # 去重候选申万一级(单行业时=[primary])
        "路由行业": route["路由行业"],          # 每个候选是否命中行业专家(多路由用)
        "口径注解": route["口径注解"],          # 模糊时的口径说明;非模糊=None(向后兼容)
        "provenance": {"structured": bool(out_periods), "qualitative": False, "verdict": False},
    }
    if persist and out_periods:
        try:
            store.put_code_view("financial_report", code, result)
        except Exception as e:                              # noqa: BLE001
            logger.warning("财报视图落盘失败 %s: %s", code, e)
    return result


def build_financial_block(code: str, as_of: str | None = None,
                          industry: str | None = None, sector: str | None = None) -> dict | None:
    """构建中心记录顶层 `financial` 轻量块(仅最新已披露报告期摘要)。

    供 panel/screen/web/Agent 直接消费(不塞多期大数组;多期在 code_view)。
    industry:可选行业名(record.meta.industry),用于金融业红旗特判。
    sector:可选市场大类板块(record.meta.sector),用于行业消歧的市场概念标注。无可见报告期 → None。
    """
    try:
        res = analyze(code, as_of=as_of, persist=False, industry=industry, sector=sector)
    except FileNotFoundError:
        return None
    latest = res.get("latest")
    if not latest:
        return None
    block = {
        "报告期": latest["report_date"],
        "报告类型": latest.get("report_type"),
        "披露日": latest.get("disclosure_date"),
        "is_forecast": latest.get("is_forecast", False),
        "quality_score": latest.get("quality_score"),
        "评级": latest.get("评级"),
        "five_dims": latest.get("five_dims"),
        "利润表摘要": latest.get("利润表摘要"),
        "flags": [f["code"] for f in latest.get("flags", [])],   # 轻量:只列命中信号名
        "flags_detail": latest.get("flags", []),                  # 完整红旗:{code,命中,严重度,值}(详情页证据)
        "金融业口径": latest.get("金融业口径", False),
        "行业专家": res.get("行业专家"),          # 命中的行业专家(无=通用兜底)
        "口径说明": res.get("口径说明"),          # 行业专属口径标注(页面展示)
        "消歧": res.get("消歧"),                  # 行业归属消歧(时变正式/市场概念/模糊标记)
        "相关行业": res.get("相关行业"),           # 候选申万一级(单行业=[primary])
        "口径注解": res.get("口径注解"),           # 模糊时口径说明;非模糊=None
        "行业模糊": bool((res.get("消歧") or {}).get("ambiguous")),  # 页面一眼可见的模糊标记
        "derived": latest.get("derived"),
        "verdict": None,   # LLM 层留口
    }
    # 审计意见闸门(闸门2)传导:取最新已披露"年报"的审计意见,即使 latest 是季报也生效。
    # 非标 → 记录级降"风险" + 补红旗(非标年报期间公司整体不可信,不待下一份干净年报不翻身)。
    annual = [p for p in res.get("periods", {}).values() if p.get("audit_opinion")]
    if annual:
        newest = max(annual, key=lambda x: x.get("disclosure_date") or "")
        op = newest.get("audit_opinion")
        pass_ops = set(_cfg().get("审计意见_通过", ["标准无保留意见", "无保留意见"]))
        gate_pass = op in pass_ops
        block["审计意见"] = op
        block["审计意见闸门"] = "通过" if gate_pass else "不通过"
        if not gate_pass:
            if "非标审计意见" not in block["flags"]:
                block["flags"].append("非标审计意见")
            block["评级"] = "风险"
    else:
        block["审计意见"] = None
        block["审计意见闸门"] = None

    # 闸门1(M2):审计机构备案核查——读采集层落的年报文本(披露日 <= as_of 才可见,无未来函数)。
    # 抽到名且不在录 → 高危红旗"审计机构未备案" + 降"风险";名录未知/抽不到名 → 不判(不误杀)。
    try:
        ar_raw = store.get_raw("annual_report_text", code)
    except FileNotFoundError:
        ar_raw = None
    if ar_raw and (as_of is None or (ar_raw.get("disclosure_date") or "") <= as_of):
        from tools.analysis.financial import audit_gate as ag_mod
        g = ag_mod.audit_gate((ar_raw.get("段落") or {}).get("审计报告"))
        block["审计机构"] = g.get("审计机构")
        block["审计机构闸门"] = g.get("闸门1")
        block["审计机构档位"] = g.get("档位")
        if g.get("闸门1") == "不通过":
            if "审计机构未备案" not in block["flags"]:
                block["flags"].append("审计机构未备案")
            block["评级"] = "风险"

    # LLM 文本层(M2):只读 llm_text.run_financial_text 预算的 code_view,**不触发 LLM**
    # (避免对每票 serialize 都烧 token)。未预算 → qualitative/verdict 保持 null。
    block.setdefault("qualitative", None)
    try:
        ft = store.get_code_view("financial_text", code)
        block["qualitative"] = ft.get("qualitative")
        block["verdict"] = ft.get("verdict")
    except FileNotFoundError:
        pass

    # 资金来源(现金流三段→造血模式)+ 资产负债关键科目:从最新可见期 raw 取,写进 block
    # (随记录同步远端;详细页不依赖 raw——upload 只同步 records/views,不含 data/raw)。
    lp = res.get("latest_period")
    try:
        rp = ((store.get_raw("financial_report", code).get("periods")) or {}).get(lp, {})
    except FileNotFoundError:
        rp = {}
    cf = rp.get("现金流量表") or {}
    bs = rp.get("资产负债表") or {}
    if cf or bs:
        cfo, cfi, cff = (cf.get("经营活动现金流量净额"), cf.get("投资活动现金流量净额"),
                         cf.get("筹资活动现金流量净额"))
        block["现金流"] = {"CFO": cfo, "CFI": cfi, "CFF": cff,
                         "自由现金流": (latest.get("derived") or {}).get("自由现金流"),
                         "造血模式": _funding_mode(cfo, cfi, cff)}
        _yx = [bs.get(k) for k in ("短期借款", "一年内到期非流动负债", "长期借款", "应付债券")]
        block["资产负债"] = {
            "货币资金": bs.get("货币资金"),
            "应收账款": bs.get("应收账款") or bs.get("应收票据及应收账款"),
            "存货": bs.get("存货"), "商誉": bs.get("商誉"), "合同负债": bs.get("合同负债"),
            "有息负债": sum(v for v in _yx if isinstance(v, (int, float))) or None,
            "资产总计": bs.get("资产总计"), "负债合计": bs.get("负债合计"),
            "归母净资产": bs.get("归母股东权益") or bs.get("股东权益合计")}
    # 年报节选(证据原文,截断;随 block 同步远端)。仅在年报披露日可见时带。
    if ar_raw and (as_of is None or (ar_raw.get("disclosure_date") or "") <= as_of):
        _secs = ar_raw.get("段落") or {}
        block["年报节选"] = {"年度": ar_raw.get("年度"), "披露日": ar_raw.get("disclosure_date"),
                          "pdf_url": ar_raw.get("pdf_url"),
                          "MD&A": (_secs.get("MD&A") or "")[:800] or None,
                          "风险": (_secs.get("风险") or "")[:600] or None}
    return block


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
