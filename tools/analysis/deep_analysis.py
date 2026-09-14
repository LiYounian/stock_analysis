"""逐票深度研判生成器(headless)。

设计:docs/计划/2026-09-13_headless逐票研判生成器与双跑框架_P2实现计划.md
      + 架构设计 §3.3(A)。**P2 只建 + 验证,不切生产**。

编排:装配事实(deep_analysis_inputs)→ 检索经验(experience_recall)→ 拼 SOP prompt
(llm/prompts.deep_analysis_*)→ 经 get_client(purpose) / get_client_for(provider)调 LLM
extract → 规范成 write_picks 可吃的 analysis unit(_JUDGE_FIELDS)→ 交 write_picks 组装落盘。

客观字段(名称/价/涨跌/命中策略)不在此填——write_picks 从 record + 策略视图回填。
LLM 只出研判字段;本模块对枚举做**兜底规范**(越界值 → 保守默认 + 记 coercion),
再由 picks_schema.validate_picks 强校验(不合规不落盘)。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools.analysis import deep_analysis_inputs as di
from tools.analysis import experience_recall as er
from tools.analysis import picks_schema as ps
from tools.llm import prompts

PURPOSE = "deep_analysis"

# 越界枚举的保守兜底(宁可保守、待人工复核,不冒进给最强表态)
_SAFE = {
    "type": "检验样本",
    "stance": "观望",
    "dir": "中性",
    "conf": "低",
    "sentiment_quality": "unknown",
}


@dataclass
class JudgeResult:
    code: str
    unit: dict                       # write_picks 可吃的 analysis unit(_JUDGE_FIELDS + code[+ strategies_hint])
    facts_notes: list = field(default_factory=list)
    coercions: list = field(default_factory=list)   # 枚举兜底/一致性修正留痕
    experience_version: str | None = None
    error: str | None = None


def _coerce_enum(val, allowed: set[str], default: str, field_name: str, coercions: list) -> str:
    if val in allowed:
        return val
    coercions.append(f"{field_name}={val!r} 越界 → {default!r}")
    return default


def _coerce_unit(code: str, raw: dict, *, sentiment_quality_hint: str | None) -> tuple[dict, list]:
    """把 LLM 原始输出规范成合规 analysis unit;返回 (unit, coercions)。"""
    coercions: list[str] = []
    raw = raw or {}

    typ = _coerce_enum(raw.get("type"), ps.TYPE, _SAFE["type"], "type", coercions)
    stance = _coerce_enum(raw.get("stance"), ps.STANCE, _SAFE["stance"], "stance", coercions)
    d1 = _coerce_enum(raw.get("dir_1d"), ps.DIRECTION, _SAFE["dir"], "dir_1d", coercions)
    d5 = _coerce_enum(raw.get("dir_5d"), ps.DIRECTION, _SAFE["dir"], "dir_5d", coercions)
    c1 = _coerce_enum(raw.get("dir_1d_conf"), ps.CONF, _SAFE["conf"], "dir_1d_conf", coercions)
    c5 = _coerce_enum(raw.get("dir_5d_conf"), ps.CONF, _SAFE["conf"], "dir_5d_conf", coercions)

    # sentiment_quality:LLM 缺/越界时用原料自评的 hint 兜底
    sq_raw = raw.get("sentiment_quality")
    if sq_raw in ps.SENTIMENT_QUALITY:
        sq = sq_raw
    else:
        sq = sentiment_quality_hint if sentiment_quality_hint in ps.SENTIMENT_QUALITY else _SAFE["sentiment_quality"]
        coercions.append(f"sentiment_quality={sq_raw!r} → {sq!r}(据原料兜底)")

    # 一致性1:不给方向 → 置信必须 '-'
    if d1 == "不给方向" and c1 != "-":
        coercions.append(f"dir_1d=不给方向 强制 dir_1d_conf {c1!r}→'-'")
        c1 = "-"
    if d5 == "不给方向" and c5 != "-":
        coercions.append(f"dir_5d=不给方向 强制 dir_5d_conf {c5!r}→'-'")
        c5 = "-"
    # 反向:给了方向但 conf='-' → 提保守 conf(校验虽不禁,但语义应有置信)
    if d1 != "不给方向" and c1 == "-":
        c1 = _SAFE["conf"]
        coercions.append("dir_1d 有方向但 conf='-' → '低'")
    if d5 != "不给方向" and c5 == "-":
        c5 = _SAFE["conf"]
        coercions.append("dir_5d 有方向但 conf='-' → '低'")

    qualifier = raw.get("stance_qualifier") or ""
    # 一致性2:情绪盲区不得给'买入'/含'首选'
    if sq in ps.SENTIMENT_BLIND:
        if stance == "买入":
            coercions.append("情绪盲区 强制 stance 买入→可参与")
            stance = "可参与"
        if "首选" in qualifier:
            coercions.append("情绪盲区 去除 stance_qualifier '首选'")
            qualifier = qualifier.replace("首选", "").strip("（）() 、,")

    wp = raw.get("watch_points")
    if isinstance(wp, str):
        wp = [wp]
    elif not isinstance(wp, list):
        wp = []

    unit = {
        "code": code,
        "type": typ,
        "stance": stance,
        "stance_qualifier": qualifier,
        "dir_1d": d1, "dir_1d_conf": c1,
        "dir_5d": d5, "dir_5d_conf": c5,
        "sentiment_quality": sq,
        "key_reason": (raw.get("key_reason") or "").strip(),
        "key_risk": (raw.get("key_risk") or "").strip(),
        "alpha_beta": (raw.get("alpha_beta") or "").strip(),
        "watch_points": [str(x).strip() for x in wp if str(x).strip()],
    }
    return unit, coercions


def _sentiment_quality_hint(facts: di.StockFacts) -> str | None:
    """从原料的情绪质量给 sentiment_quality 兜底 hint。"""
    sent = (facts.sentiment or {}).get("sentiment") if isinstance(facts.sentiment, dict) else None
    q = (sent or {}).get("质量") if isinstance(sent, dict) else None
    if q in ps.SENTIMENT_QUALITY:
        return q
    if facts.sentiment is None:
        return "missing"
    return None


def generate_one(
    code: str,
    pick_date: str,
    *,
    client,
    data_root: Path | None = None,
    experience_base: Path | None = None,
    strategy_tags=None,
    strategies_hint=None,
    experience_top_k: int = 8,
    news_time_cutoff: str | None = None,
) -> JudgeResult:
    """对单票生成研判 unit。client 需实现 .extract(text, schema, instruction)(可注入桩)。

    news_time_cutoff(如 '<date> 11:30:00'):午盘 ≤11:30 intraday 防未来,透传 di.assemble。
    """
    facts = di.assemble(code, pick_date, data_root, news_time_cutoff=news_time_cutoff)
    if facts.record is None:
        return JudgeResult(code=code, unit={"code": code}, facts_notes=facts.notes,
                           error="record 缺失,跳过研判")

    qualitative = di.infer_qualitative(facts.record)
    snippets, exp_ver = er.recall_snippets_for(
        pick_date, industry=facts.industry, strategy_tags=strategy_tags,
        qualitative=qualitative, top_k=experience_top_k, base_dir=experience_base)

    text = di.render_facts_text(facts)
    instruction = prompts.deep_analysis_instruction(
        facts.name, code, experience_snippets=snippets)

    try:
        raw = client.extract(text, prompts.DEEP_ANALYSIS_SCHEMA, instruction=instruction)
    except Exception as e:                                # noqa: BLE001 研判失败显式记录,不冒充成功
        return JudgeResult(code=code, unit={"code": code}, facts_notes=facts.notes,
                           experience_version=exp_ver, error=f"LLM 研判失败:{str(e)[:120]}")

    unit, coercions = _coerce_unit(code, raw, sentiment_quality_hint=_sentiment_quality_hint(facts))
    if strategies_hint:
        unit["strategies_hint"] = strategies_hint
    return JudgeResult(code=code, unit=unit, facts_notes=facts.notes,
                       coercions=coercions, experience_version=exp_ver)


def generate(
    pick_date: str,
    codes: list[str],
    *,
    client=None,
    provider_id: str | None = None,
    enable_thinking: bool | None = None,
    data_root: Path | None = None,
    experience_base: Path | None = None,
    strategies_hint_map: dict | None = None,
    experience_top_k: int = 8,
    news_time_cutoff: str | None = None,
) -> list[JudgeResult]:
    """对候选票逐个生成研判。

    client 优先注入(测试用桩);否则按 provider_id(get_client_for,双跑指定臂)
    或 purpose 路由(get_client(deep_analysis))构造。
    news_time_cutoff:午盘 ≤11:30 intraday 防未来,透传每票 assemble。
    """
    if client is None:
        from tools.llm import client as lc
        client = (lc.get_client_for(provider_id, enable_thinking=enable_thinking)
                  if provider_id else lc.get_client(PURPOSE))

    out: list[JudgeResult] = []
    for code in codes:
        hint = (strategies_hint_map or {}).get(code)
        out.append(generate_one(
            code, pick_date, client=client, data_root=data_root,
            experience_base=experience_base, strategies_hint=hint,
            experience_top_k=experience_top_k, news_time_cutoff=news_time_cutoff))
    return out


def units_of(results: list[JudgeResult]) -> list[dict]:
    """取可落盘的 analysis unit 列表(跳过 error 票)。"""
    return [r.unit for r in results if r.error is None]


# ————————————————————————————————————————————————————————————————
# CLI(dry-run 友好:默认只产研判 JSON,不落生产)
# ————————————————————————————————————————————————————————————————
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="headless 逐票深度研判生成器(P2;产研判 JSON,不切生产)")
    ap.add_argument("--date", required=True, help="选股日 YYYY-MM-DD")
    ap.add_argument("--codes", required=True, help="候选票代码,逗号分隔")
    ap.add_argument("--provider", help="provider id(如 deepseek_v4pro / qwen_max);缺省走 deep_analysis 路由主 provider")
    ap.add_argument("--think", choices=["on", "off"], help="think 开关(A/B);缺省沿用注册表默认")
    ap.add_argument("--data-root", help="data/analysis 根(dry-run 指向生产数据只读)")
    ap.add_argument("--experience-base", help="经验沉淀目录(默认项目内)")
    ap.add_argument("--out", required=True, help="研判 JSON 输出路径(analysis list;dry-run 落隔离目录)")
    args = ap.parse_args(argv)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    enable_thinking = {"on": True, "off": False}.get(args.think)
    data_root = Path(args.data_root) if args.data_root else None
    exp_base = Path(args.experience_base) if args.experience_base else None

    results = generate(
        args.date, codes, provider_id=args.provider, enable_thinking=enable_thinking,
        data_root=data_root, experience_base=exp_base)

    units = units_of(results)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(units, ensure_ascii=False, indent=2), encoding="utf-8")

    for r in results:
        tag = "ERR" if r.error else "OK "
        extra = r.error or (f"coercions={len(r.coercions)}" if r.coercions else "")
        print(f"[{tag}] {r.code} exp={r.experience_version} {extra}", file=sys.stderr)
    print(f"已写研判 JSON:{out_path}({len(units)}/{len(results)} 票成功)", file=sys.stderr)
    return 0 if units else 1


if __name__ == "__main__":
    raise SystemExit(main())
