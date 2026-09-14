"""维度4:抽 3 只边界票,捕获开思考 reasoning_content 全文 + 关/开 unit 关键字段 + 实际收益。"""
from __future__ import annotations
import json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from thinking_client import ThinkingClient
from tools.analysis import deep_analysis_inputs as di
from tools.analysis import experience_recall as er
from tools.analysis import shadow_score as ss
from tools.llm import prompts

ART = Path(__file__).resolve().parent
OFF = ART.parent / "2026-09-14_候选池v2_AB_artifacts"
DR = Path("/Users/yqg/Documents/projects/stock_analysis/data/analysis")
EXP = Path("/Users/yqg/Documents/projects/stock_analysis/docs/每日分析/经验沉淀")
CARD = ss.load_scorecard("/Users/yqg/Documents/projects/stock_analysis/data/analysis/backtest/forward_scorecard.csv")
CASES = [("2026-08-14", "300515"), ("2026-09-09", "301345"), ("2026-09-10", "605098")]

cli = ThinkingClient(os.environ["QWEN_BASE_URL"], os.environ["QWEN_API_KEY"])


def unit_of(path, code):
    for u in (json.load(open(path, encoding="utf-8")) if Path(path).exists() else []):
        if u.get("code") == code:
            return u
    return None


out = []
for date, code in CASES:
    facts = di.assemble(code, date, DR)
    qual = di.infer_qualitative(facts.record)
    snips, _ = er.recall_snippets_for(date, industry=facts.industry, qualitative=qual,
                                      top_k=8, base_dir=EXP)
    text = di.render_facts_text(facts)
    instr = prompts.deep_analysis_instruction(facts.name, code, experience_snippets=snips)
    sysmsg = (f"{instr}\n只输出一个 JSON,不要任何多余文字/解释。JSON 字段与含义:"
              f"{json.dumps(prompts.DEEP_ANALYSIS_SCHEMA, ensure_ascii=False)}")
    # 直接调底层拿 reasoning:复用 cli.chat 已剥离,故这里单独取 message
    r = cli._cli.chat.completions.create(
        model=cli.model, messages=[{"role": "system", "content": sysmsg},
                                   {"role": "user", "content": text}],
        temperature=0.0, max_tokens=4096, extra_body={"enable_thinking": True})
    msg = r.choices[0].message
    reasoning = getattr(msg, "reasoning_content", "") or ""
    off_u = unit_of(OFF / f"units_deepseek_v4pro_v2_{date}.json", code)
    on_u = unit_of(ART / f"units_deepseek_v4pro_v2think_{date}.json", code)
    ret = CARD.get(date, {}).get(code, {})
    out.append({
        "date": date, "code": code, "name": facts.name,
        "off_stance": off_u and off_u.get("stance"), "on_stance": on_u and on_u.get("stance"),
        "off_key_reason": off_u and off_u.get("key_reason"),
        "on_key_reason": on_u and on_u.get("key_reason"),
        "r_1": ret.get("r_1"), "r_5": ret.get("r_5"),
        "reasoning_content": reasoning,
    })
    print(f"=== {date} {code} {facts.name} | 关:{out[-1]['off_stance']} 开:{out[-1]['on_stance']} "
          f"| r_1={ret.get('r_1')} r_5={ret.get('r_5')} | reasoning {len(reasoning)}字 ===")

(ART / "reasoning_cases.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
print("wrote reasoning_cases.json")
