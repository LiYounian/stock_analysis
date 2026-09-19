#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""乙案效果验证 · A/B driver（金字塔合成层 W3 加厚是否真改变选股行为）。

研究模拟 · 非投资建议 · 不真交易。

严格 A/B：`build_package` 只建一次 → 派生 digest 开/关两版决策包文本 →
2 provider(DeepSeek/千问) × 2 臂 各跑一次：
  A臂(before) = 关 digest + 旧 SYSTEM_PROMPT(pre-W3-B)
  B臂(现状)   = 开 digest + 加厚 SYSTEM_PROMPT(含辩证核对段)
用同一套生产函数（d2_package.build_package/render_package + d2_pyramid_llm_select 的
call_llm/sanitize/build_machine_json），忠实复刻 CLI 两开关语义，只建一次包省调用。

真跑 LLM 必须走 zsh -ic（非交互 bash 无内部网关 env）：
    zsh -ic 'cd <repo> && PYTHONPATH=. ~/.conda/envs/stock_analysis/bin/python \
        -m tools.experimental.ab_verify.ab_driver \
        --data-root <生产data父目录> --out-dir <非repo产物目录>'
产物（raw_*/out_*/ab_summary.json）落 --out-dir，**不入库**。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import datetime

from tools.pyramid import d2_package as P
from tools.pyramid import registry
import tools.pyramid.tools  # noqa: F401
from tools.experimental import d2_pyramid_llm_select as S

PROVIDERS = [("deepseek_v4pro", "DeepSeek"), ("qwen_max", "千问")]
# 严格 A/B 两臂；归因格(digest-only/辩证-only)见 attr_driver.py
ARMS = [
    ("A", dict(no_digest=True, baseline_prompt=True)),    # before: 关digest + 旧prompt
    ("B", dict(no_digest=False, baseline_prompt=False)),  # 现状:  digest开 + 加厚prompt
]


def build_facts(pkg, as_of, root):
    facts = {}
    for card in pkg["候选卡片"]:
        code = card["code"]
        facts[code] = {
            "骨架分": card["骨架分"], "子分": card["子分"], "来源标签": card["来源标签"],
            "entry_price": registry.get("entry_price").run(as_of, code, root=root).fields,
            "price_volume": registry.get("price_volume").run(as_of, code, root=root).fields,
            "sector_context": registry.get("sector_context").run(as_of, code, root=root).fields,
        }
    return facts


def digest_richness(pkg):
    """统计 digest 信息量：多少票带真实利好/利空计数(而非 0条/无覆盖)。"""
    rich, detail = 0, []
    for c in pkg["候选卡片"]:
        lines = c.get("新闻digest_lines") or []
        head = lines[0] if lines else ""
        if "利好" in head and "近" in head:
            rich += 1
        detail.append((c["code"], head[:60], len(lines)))
    return rich, detail


def main():
    ap = argparse.ArgumentParser(description="乙案 A/B 验证 driver（研究模拟·非投资建议）")
    ap.add_argument("--as-of", default="2026-09-17")
    ap.add_argument("--next-day", default="2026-09-18")
    ap.add_argument("--data-root", default=None, help="生产 data 父目录（不给则用 repo 内 data）")
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--out-dir", default=None, help="产物目录（默认系统临时目录，绝不写入 repo）")
    a = ap.parse_args()

    out = a.out_dir or os.path.join(tempfile.gettempdir(), f"ab_verify_{a.as_of}")
    os.makedirs(out, exist_ok=True)

    print(f"[build] build_package once as_of={a.as_of} top{a.top_n} ...", file=sys.stderr)
    pkg = P.build_package(a.as_of, root=a.data_root, top_n=a.top_n)
    facts = build_facts(pkg, a.as_of, a.data_root)
    top_codes = [c["code"] for c in pkg["候选卡片"]]
    names = S.load_code_names()

    rich, detail = digest_richness(pkg)
    print(f"[digest] 富信息(带利好利空计数)票数 {rich}/{len(top_codes)}", file=sys.stderr)

    # B=digest开(原pkg) / A=digest关(清空 digest 行后 render)
    text_B = P.render_package(pkg)
    pkg_A = copy.deepcopy(pkg)
    for c in pkg_A["候选卡片"]:
        c["新闻digest_lines"] = []
    text_A = P.render_package(pkg_A)
    dh = P._DIGEST头
    assert dh in text_B and dh not in text_A, "digest 开关未生效"
    print(f"[assert] digest 头仅现于 B 臂 ✓ (B含/A不含: {dh in text_B}/{dh not in text_A})",
          file=sys.stderr)

    results = {}
    for pid, label in PROVIDERS:
        for arm, cfg in ARMS:
            text = text_A if cfg["no_digest"] else text_B
            system = S.SYSTEM_PROMPT_BASELINE if cfg["baseline_prompt"] else S.SYSTEM_PROMPT
            user = S.build_user_prompt(text, top_codes)
            tag = f"{label}_{arm}"
            print(f"[call] {tag} no_digest={cfg['no_digest']} "
                  f"baseline_prompt={cfg['baseline_prompt']} ...", file=sys.stderr)
            try:
                raw, usage, model_id = S.call_llm(pid, system, user)
            except Exception as e:
                print(f"[ERR] {tag} call failed: {e}", file=sys.stderr)
                results[tag] = {"error": str(e)}
                continue
            with open(os.path.join(out, f"raw_{tag}.txt"), "w", encoding="utf-8") as f:
                f.write(raw)
            parsed = S.parse_llm_json(raw)
            if not parsed:
                results[tag] = {"error": "unparseable", "model": model_id, "usage": usage}
                print(f"[ERR] {tag} 无可解析 JSON", file=sys.stderr)
                continue
            res = S.sanitize(parsed, top_codes)
            mj = S.build_machine_json(label, model_id, a.as_of, a.next_day,
                                      res, facts, names, usage)
            with open(os.path.join(out, f"out_{tag}.json"), "w", encoding="utf-8") as f:
                json.dump(mj, f, ensure_ascii=False, indent=2)
            results[tag] = {
                "model": model_id, "usage": usage,
                "buy": [b["code"] for b in res["buy"]],
                "avoid": [x["code"] for x in res["avoid"]],
                "buy_full": res["buy"], "avoid_full": res["avoid"],
                "adjust_log": res["adjust_log"], "market_tone": res["market_tone"],
                "warns": res["warns"],
            }
            print(f"[ok] {tag} buy={results[tag]['buy']} avoid={results[tag]['avoid']} "
                  f"tok={usage['prompt']+usage['completion']}", file=sys.stderr)

    summary = {
        "as_of": a.as_of, "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "口径": "temperature=0 机制验证口径·研究模拟·非投资建议",
        "top_codes": top_codes, "digest富信息票数": rich, "digest明细": detail,
        "cells": results,
    }
    with open(os.path.join(out, "ab_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("\n===== A/B 名单对比 =====")
    for _, label in PROVIDERS:
        a_, b_ = results.get(f"{label}_A", {}), results.get(f"{label}_B", {})
        print(f"[{label}]")
        print(f"  A(旧) buy={a_.get('buy')} avoid={a_.get('avoid')}")
        print(f"  B(新) buy={b_.get('buy')} avoid={b_.get('avoid')}")
        if a_.get("buy") is not None and b_.get("buy") is not None:
            print(f"  买入变化: 新增{set(b_['buy'])-set(a_['buy'])} 移除{set(a_['buy'])-set(b_['buy'])}")
            print(f"  规避变化: 新增{set(b_['avoid'])-set(a_['avoid'])} "
                  f"移除{set(a_['avoid'])-set(b_['avoid'])}")
    print(f"\n[out] {os.path.join(out, 'ab_summary.json')}")


if __name__ == "__main__":
    main()
