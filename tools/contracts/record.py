"""数据契约:中心记录 data/analysis/{code}.json 的 schema + 校验 + 枚举词表。

这是"层与层之间的 API"单一真源(见 docs/参考/架构评审与规范_v1.md 第七章)。
生产者(serialize)按此产出,消费者(panel/screen/web/report/Agent)按此消费。
轻量校验,不引第三方依赖;对"数据不可用=null"宽容,只在字段存在且非空时校验枚举/类型。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_JSON = _ROOT / "tools" / "contracts" / "record.schema.json"

# ————————————————————————————————————————————————
# 枚举词表(全项目统一词汇,单一真源)
# ————————————————————————————————————————————————
ENUMS = {
    "影响方向": ("利好", "利空", "中性"),        # 新闻/情绪/预测对个股的方向
    "公告方向": ("利好", "利空", "待判"),        # 公告标题规则粗判
    "趋势评级": ("偏多", "中性", "偏空"),        # signals.trend.评级
    "超买超卖": ("超买", "超卖", "中性"),        # signals.ob_os.verdict
    "拐点标签": ("反弹启动", "超跌待反弹", "无"),  # signals.reversal.拐点标签
    "买卖倾向": ("偏买入", "偏卖出", "观望"),      # prediction.买卖倾向.结论
    "情绪三层": ("政策", "公司行为", "舆情"),      # sentiment.events[].层
    "与本股关系": ("直接", "间接", "无关"),        # sentiment.events[].与本股关系
    "财报评级": ("优", "良", "中", "差", "风险"),   # financial.评级(质地评分映射)
    "新鲜度": ("新鲜", "陈旧", "无数据"),           # sentiment(顶层/三层).新鲜度:date-pin 采集新鲜度三态
}
HOLD_PERIODS = ("1日", "5日", "10日")

# ————————————————————————————————————————————————
# 约定(类型/量纲统一)
# ————————————————————————————————————————————————
CONVENTIONS = {
    "代码": "A股6位 / 港股5位字符串(如 '000021' / '00700'),非 int",
    "日期": "YYYY-MM-DD 字符串",
    "金额": "默认单位元;字段名以 _yi / 含'亿'结尾时为亿",
    "百分比": "数值(如 5.88 表示 5.88%),非字符串",
    "缺失": "统一用 null(数据不可用),不用空串/0 冒充",
    "命名不一致(待规范)": "signals.ob_os 用英文键 verdict,而 trend.评级 / 买卖倾向.结论 用中文;"
                          "本契约按现状固化,建议后续阶段统一(改动涉及多模块,单独排期)",
}

# 记录顶层块(12);核心必需 + 可空块
REQUIRED_TOP = ("schema_version", "meta", "events", "timeseries_refs", "provenance")
OPTIONAL_TOP = ("snapshot", "valuation", "fundamental", "signals",
                "prediction", "sentiment", "fundflow", "financial")
TOP_LEVEL_KEYS = REQUIRED_TOP[:2] + OPTIONAL_TOP + REQUIRED_TOP[2:]

# 人读 + 机读 schema(字段 → 说明)。详尽结构见各生产者;此处固化契约要点。
RECORD_SCHEMA = {
    "schema_version": "str,如 '1.0'",
    "meta": {"code": "str(5-6)", "name": "str", "sector": "str|null",
             "industry": "str|null", "market": "str(A|HK)", "as_of": "date(YYYY-MM-DD)"},
    "snapshot": "null | {close, pct_chg, ma{ma5,ma10,ma20,ma60,排列}, "
                "macd{dif,dea,macd,状态}, kdj{k,d,j,状态}, rsi{rsi6,rsi12,rsi24}, "
                "bias20, vol_ratio, vol_state}",
    "valuation": "null | {pe_ttm, pb, mktcap_yi(亿), 报告期, pe_valid:bool, ...}",
    "fundamental": "null | {营收, 净利, 营收增速, 净利增速, ROE, 毛利率, 净利率, 负债率}",
    "financial": "null | {报告期, 报告类型, 披露日, is_forecast, quality_score:0~100, "
                 "评级∈财报评级, five_dims{成长,质量,健康,运营,回报}, "
                 "利润表摘要{营业总收入,归母净利润,扣非归母净利润,营收增速,...}, "
                 "flags[信号名], derived{增速/质量/健康/运营/回报衍生}, verdict:null(LLM层留口)}"
                 "(财报 P0 数值层最新已披露期摘要;多期明细见 code_view financial_report;披露日锚定无未来函数)",
    "signals": "null | {trend{评级∈趋势评级,得分,依据[]}, "
               "reversal{拐点标签∈拐点标签,拐点评分,...}, ob_os{verdict∈超买超卖,resonance,per_indicator}}",
    "prediction": "null | {现价, atr, atr_pct, 近三次放量[], 支撑位[], 压力位[], "
                  "持有期建议{1日/5日/10日:{止损位,最大亏损%,止盈位,目标盈利%,风险收益比}}, "
                  "结构位{支撑[],压力[],距支撑%,距压力%,区间位置%,当日量比,放量,突破,趋势,bias20,"
                  "锚定{情景,止损位,止盈位,盈亏比,依据[]}, 均线支撑[]{名称,价,距今%}}, "  # L3:纯数据结构位+情景锚定;均线支撑=F2b候选止跌锚
                  "情景预测{1日/5日/10日:{上涨概率%,...}}(无条件,对照), "
                  "指标条件化预测{1日/5日/10日:{方向∈看涨/看跌/中性/数据不足,置信度∈高/中/低,上涨概率%,"
                  "方向_修正∈看涨/看跌/中性/数据不足(激进版·含消息面后验倾斜;k=0/无信号/10日/退回时==方向),"
                  "上涨概率%_修正(=clip(上涨概率%+k·根源信号,0,100);仅1/5日且非退回且k≠0时≠上涨概率%),"
                  "是否倾斜:bool,悲观%,中位%,乐观%,期望%,期望标准误%,相似样本数,放宽层级∈精确/放宽1/放宽2/退回,是否退回}}, "  # F3+F4(+激进版倾斜,新键可空、旧记录兼容)
                  "买卖倾向{结论∈买卖倾向,得分,依据[]}, "
                  "消息面提示:str|null(第二步·保守版:纯文本提示看涨/看跌/中性,不改任何预测数字;旧记录无此字段/null 仍合规), "
                  "免责}",
    "sentiment": "null | {净情绪分:-1~1, 利好数, 利空数, 样本数, "
                 "口径:str, "
                 "采集日期:date|null(顶层聚合=三层最旧层日期), "
                 "新鲜度:新鲜/陈旧/无数据|null(顶层聚合=最坏优先:任一层陈旧则陈旧,全无数据则无数据,否则新鲜), "
                 "锁定日期:date|null(本次运行锁定的交易日 active_date,诊断回退用:≠采集日期即回退), "
                 "三层{新闻,舆情,政策}{...原字段, 采集日期:date|null, 新鲜度:新鲜/陈旧/无数据|null}, "
                 "events[]{影响方向∈影响方向,影响强度:1~5,与本股关系∈与本股关系,层∈情绪三层,标题,time}}"
                 "(新鲜度/采集日期/锁定日期为附加可选字段,旧记录无此字段仍合规)",
    "fundflow": "null | {今日主力净流入(元), 今日主力净占比, 近5日主力合计(元), 主力连续净流入天数}",
    "events": "list[{date, type, impact∈公告方向, title}](公告)",
    "timeseries_refs": "{kline, fundflow, announcements}(文件路径指针)",
    "provenance": "{tech:bool, fundamental:bool, announcements:int, fundflow:bool}",
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CODE_RE = re.compile(r"^\d{5,6}$")


def _enum_ok(val, key: str) -> bool:
    """None/非字符串 视为"数据不可用",不校验;仅在有值时查枚举。"""
    return val is None or val in ENUMS[key]


def validate_record(rec: dict) -> list[str]:
    """校验单条记录,返回问题列表(空=合规)。对 null 宽容,只查存在的字段。"""
    errs: list[str] = []
    if not isinstance(rec, dict):
        return ["记录非 dict"]

    for k in REQUIRED_TOP:
        if k not in rec:
            errs.append(f"缺必需顶层块: {k}")

    meta = rec.get("meta") or {}
    if not _CODE_RE.match(str(meta.get("code", ""))):
        errs.append(f"meta.code 非6位字符串: {meta.get('code')!r}")
    if not isinstance(meta.get("name"), str):
        errs.append("meta.name 缺失或非字符串")
    if meta.get("as_of") and not _DATE_RE.match(str(meta.get("as_of"))):
        errs.append(f"meta.as_of 非日期: {meta.get('as_of')!r}")

    sig = rec.get("signals")
    if isinstance(sig, dict):
        if isinstance(sig.get("trend"), dict) and not _enum_ok(sig["trend"].get("评级"), "趋势评级"):
            errs.append(f"signals.trend.评级 非法: {sig['trend'].get('评级')!r}")
        if isinstance(sig.get("ob_os"), dict) and not _enum_ok(sig["ob_os"].get("verdict"), "超买超卖"):
            errs.append(f"signals.ob_os.verdict 非法: {sig['ob_os'].get('verdict')!r}")
        if isinstance(sig.get("reversal"), dict) and not _enum_ok(sig["reversal"].get("拐点标签"), "拐点标签"):
            errs.append(f"signals.reversal.拐点标签 非法: {sig['reversal'].get('拐点标签')!r}")

    pred = rec.get("prediction")
    if isinstance(pred, dict):
        bs = pred.get("买卖倾向") or {}
        if isinstance(bs, dict) and not _enum_ok(bs.get("结论"), "买卖倾向"):
            errs.append(f"prediction.买卖倾向.结论 非法: {bs.get('结论')!r}")
        for hp in (pred.get("持有期建议") or {}):
            if hp not in HOLD_PERIODS:
                errs.append(f"prediction.持有期建议 含非法持有期: {hp!r}")

    sent = rec.get("sentiment")
    if isinstance(sent, dict):
        # 新增(附加可选):新鲜度三态枚举 + 采集/锁定日期格式;null/缺失一律宽容(旧记录兼容)
        if not _enum_ok(sent.get("新鲜度"), "新鲜度"):
            errs.append(f"sentiment.新鲜度 非法: {sent.get('新鲜度')!r}")
        for dk in ("采集日期", "锁定日期"):
            dv = sent.get(dk)
            if dv is not None and not _DATE_RE.match(str(dv)):
                errs.append(f"sentiment.{dk} 非日期: {dv!r}")
        three = sent.get("三层")
        if isinstance(three, dict):
            for lname, lval in three.items():
                if not isinstance(lval, dict):
                    continue
                if not _enum_ok(lval.get("新鲜度"), "新鲜度"):
                    errs.append(f"sentiment.三层.{lname}.新鲜度 非法: {lval.get('新鲜度')!r}")
                av = lval.get("采集日期")
                if av is not None and not _DATE_RE.match(str(av)):
                    errs.append(f"sentiment.三层.{lname}.采集日期 非日期: {av!r}")
        for i, e in enumerate(sent.get("events") or []):
            if not _enum_ok(e.get("影响方向"), "影响方向"):
                errs.append(f"sentiment.events[{i}].影响方向 非法: {e.get('影响方向')!r}")
            if not _enum_ok(e.get("与本股关系"), "与本股关系"):
                errs.append(f"sentiment.events[{i}].与本股关系 非法: {e.get('与本股关系')!r}")
            if e.get("层") is not None and e.get("层") not in ENUMS["情绪三层"]:
                errs.append(f"sentiment.events[{i}].层 非法: {e.get('层')!r}")

    for i, e in enumerate(rec.get("events") or []):
        if e.get("impact") is not None and e.get("impact") not in ENUMS["公告方向"]:
            errs.append(f"events[{i}].impact 非法: {e.get('impact')!r}")

    fin = rec.get("financial")
    if isinstance(fin, dict) and not _enum_ok(fin.get("评级"), "财报评级"):
        errs.append(f"financial.评级 非法: {fin.get('评级')!r}")

    return errs


def is_valid(rec: dict) -> bool:
    return not validate_record(rec)


def dump_schema() -> str:
    """导出机读 schema(枚举+约定+字段)到 record.schema.json,返回路径。"""
    payload = {"enums": {k: list(v) for k, v in ENUMS.items()},
               "hold_periods": list(HOLD_PERIODS),
               "conventions": CONVENTIONS,
               "top_level_keys": list(TOP_LEVEL_KEYS),
               "record_schema": RECORD_SCHEMA}
    SCHEMA_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(SCHEMA_JSON)


if __name__ == "__main__":
    print("导出", dump_schema())
