#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""乙案效果验证 · 归因 driver（单 provider 分离 W3-A vs W3-B 驱动）。

研究模拟 · 非投资建议 · 不真交易。

在严格 A/B(见 ab_driver.py)出现 B≠A 实质差异时，可选地在单个 provider 上补两格：
  Donly(只开 digest)     = digest 开 + 旧 SYSTEM_PROMPT   → 隔离 W3-A(digest) 的驱动
  Xonly(只开辩证段)      = digest 关 + 加厚 SYSTEM_PROMPT → 隔离 W3-B(辩证段) 的驱动
与 A/B 四格并读即可判断名单变化由 digest 有无(文本扰动) / digest 内容 / 辩证段 哪个驱动。

真跑 LLM 走 zsh -ic：
    zsh -ic 'cd <repo> && PYTHONPATH=. ~/.conda/envs/stock_analysis/bin/python \
        -m tools.experimental.ab_verify.attr_driver \
        --provider deepseek_v4pro --label DeepSeek \
        --data-root <生产data父目录> --out-dir <非repo产物目录>'
产物 raw_*/attr_summary.json 落 --out-dir，**不入库**。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile

from tools.pyramid import d2_package as P
from tools.pyramid import registry
import tools.pyramid.tools  # noqa: F401
from tools.experimental import d2_pyramid_llm_select as S

CELLS = [
    ("Donly", dict(no_digest=False, baseline_prompt=True)),   # 只开 digest
    ("Xonly", dict(no_digest=True, baseline_prompt=False)),   # 只开辩证段
]


def main():
    ap = argparse.ArgumentParser(description="乙案归因 driver（研究模拟·非投资建议）")
    ap.add_argument("--provider", default="deepseek_v4pro",
                    choices=["deepseek_v4pro", "qwen_max"])
    ap.add_argument("--label", default="DeepSeek")
    ap.add_argument("--as-of", default="2026-09-17")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--top-n", type=int, default=15)
    ap.add_argument("--out-dir", default=None, help="产物目录（默认系统临时目录，绝不写入 repo）")
    a = ap.parse_args()

    out = a.out_dir or os.path.join(tempfile.gettempdir(), f"ab_verify_attr_{a.as_of}")
    os.makedirs(out, exist_ok=True)

    pkg = P.build_package(a.as_of, root=a.data_root, top_n=a.top_n)
    top_codes = [c["code"] for c in pkg["候选卡片"]]
    text_B = P.render_package(pkg)
    pkg_A = copy.deepcopy(pkg)
    for c in pkg_A["候选卡片"]:
        c["新闻digest_lines"] = []
    text_A = P.render_package(pkg_A)

    res = {}
    for tag, cfg in CELLS:
        text = text_A if cfg["no_digest"] else text_B
        system = S.SYSTEM_PROMPT_BASELINE if cfg["baseline_prompt"] else S.SYSTEM_PROMPT
        user = S.build_user_prompt(text, top_codes)
        print(f"[call] {a.label}_{tag} no_digest={cfg['no_digest']} "
              f"baseline_prompt={cfg['baseline_prompt']}", file=sys.stderr)
        raw, usage, mid = S.call_llm(a.provider, system, user)
        with open(os.path.join(out, f"raw_{a.label}_{tag}.txt"), "w", encoding="utf-8") as f:
            f.write(raw)
        r = S.sanitize(S.parse_llm_json(raw), top_codes)
        res[tag] = {"buy": [b["code"] for b in r["buy"]],
                    "avoid": [x["code"] for x in r["avoid"]],
                    "avoid_full": r["avoid"], "adjust_log": r["adjust_log"], "usage": usage}
        print(f"[ok] {a.label}_{tag} buy={res[tag]['buy']} avoid={res[tag]['avoid']}",
              file=sys.stderr)

    with open(os.path.join(out, "attr_summary.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n== 归因({a.label}) ==")
    print(f"  Donly(只digest) buy={res['Donly']['buy']} avoid={res['Donly']['avoid']}")
    print(f"  Xonly(只辩证)   buy={res['Xonly']['buy']} avoid={res['Xonly']['avoid']}")
    print(f"[out] {os.path.join(out, 'attr_summary.json')}")


if __name__ == "__main__":
    main()
