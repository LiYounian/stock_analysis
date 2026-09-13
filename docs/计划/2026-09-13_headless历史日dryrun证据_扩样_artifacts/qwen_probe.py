"""千问被动度体检:max-push 变体(强制表态 prompt + think-on + 提温)。

复用 deep_analysis 的输入装配/枚举兜底,仅替换 instruction(追加"禁止全部观望、须给明确
表态与方向")并对 qwen_max 提温 + 开思考。看千问能否被逼出方向、方向对不对。
对照:上轮已证 baseline 千问(温0/思考关/默认prompt)近乎恒 0 买入(直接引用,不重跑)。
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

from tools.analysis import deep_analysis as da
from tools.analysis import deep_analysis_inputs as di
from tools.analysis import experience_recall as er
from tools.analysis import shadow_pool as sp
from tools.llm import prompts, client as lc

FORCE = (
    "\n\n【本次额外硬性要求·仅评测】你必须对该票给出明确 stance 与方向,"
    "严禁一律输出'观望/中性/低波待动'来回避判断:"
    "在证据支持时应敢于给'买入'或'可参与'并给出 dir_1d/dir_5d 的偏多或偏空方向;"
    "只有当证据确凿指向下行或风险时才用'规避'。'观望'只允许在证据确实两可时少量使用。"
)

def probe_day(date, data_root, exp_base, temperature=0.5):
    pool, _ = sp.build_pool(data_root, date)
    client = lc.get_client_for("qwen_max", enable_thinking=False)
    units = []
    for code in pool:
        facts = di.assemble(code, date, Path(data_root))
        if facts.record is None:
            continue
        qual = di.infer_qualitative(facts.record)
        snips, _ = er.recall_snippets_for(date, industry=facts.industry,
                                          qualitative=qual, top_k=8,
                                          base_dir=Path(exp_base) if exp_base else None)
        text = di.render_facts_text(facts)
        instr = prompts.deep_analysis_instruction(facts.name, code, experience_snippets=snips) + FORCE
        try:
            raw = client.extract(text, prompts.DEEP_ANALYSIS_SCHEMA, instruction=instr,
                                 temperature=temperature)
        except Exception as e:  # noqa: BLE001
            print(f"  [{code}] ERR {str(e)[:80]}", file=sys.stderr); continue
        unit, _ = da._coerce_unit(code, raw, sentiment_quality_hint=da._sentiment_quality_hint(facts))
        units.append(unit)
    return pool, units

if __name__ == "__main__":
    DR = "/Users/yqg/Documents/projects/stock_analysis/data/analysis"
    EB = "/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀"
    ART = Path("/Users/yqg/Documents/projects/stock_analysis/.claude/worktrees/keen-payne-1de045/docs/计划/2026-09-13_headless历史日dryrun证据_扩样_artifacts")
    days = sys.argv[1].split(",") if len(sys.argv) > 1 else ["2026-08-20","2026-09-03","2026-09-08"]
    for d in days:
        t0=time.time()
        pool, units = probe_day(d, DR, EB)
        buy=[u["code"] for u in units if u.get("stance") in ("买入","可参与")]
        (ART/f"units_qwen_maxpush_{d}.json").write_text(json.dumps(units,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"[{d}] n={len(units)} buy={len(buy)}({','.join(buy)}) "
              f"stances={ {u['code']:u.get('stance') for u in units} } {time.time()-t0:.0f}s", file=sys.stderr)
