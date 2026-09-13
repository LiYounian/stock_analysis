"""历史日 dry-run 验证:headless 生成器 vs 当时 Claude 窗口研判。

设计:docs/计划/2026-09-13_headless逐票研判生成器与双跑框架_P2实现计划.md §4
      + 架构设计 §3.4(P1:与历史 Claude 日报人工比对校准)。

用途:挑已有 Claude 产出的选股日,用生成器跑同一候选集(= Claude md 的 PICKS 锚点),
      与当时 `选股/<date>.md` 的「买入建议排序」表对比,量化"程序化 vs Claude"的差距:
        - 覆盖度:生成器是否对所有候选出了研判;
        - type / stance 一致性(把 Claude 自由文本表态归一到 picks_schema 4 枚举后精确比);
        - **方向姿态**一致性(是否"敢给可交易方向":偏多/偏空 = 承诺,不给方向/低波待动/中性 = 不承诺)——
          锁 SOP 纪律"无触发信号/游资情绪票不硬给方向";
        - **退化**判定:Claude 给买入/可参与但生成器给规避/观望(过保守),或反向(过冒进)。

**dry-run 不落生产、不覆盖已有 md/json**。方向对比是**姿态级粗比**(Claude 自由文本 vs 枚举,
无法逐字精确),报告里显式标注,不夸大程序化能力。
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from tools.analysis import picks_schema as ps

# 提取 md 第一行 PICKS 锚点
_PICKS_RE = re.compile(r"<!--\s*PICKS:\s*([^>]*?)\s*-->")


@dataclass
class ClaudeVerdict:
    code: str
    name: str
    type_raw: str
    stance_raw: str
    dir1_raw: str
    dir5_raw: str


# ————————————————————————————————————————————————————————————————
# 解析 Claude 选股 md
# ————————————————————————————————————————————————————————————————
def parse_picks_anchor(md_text: str) -> list[str]:
    m = _PICKS_RE.search(md_text)
    if not m:
        return []
    inner = m.group(1).strip()
    if not inner or inner == "none":
        return []
    return [c.strip() for c in inner.split(",") if c.strip()]


def parse_ranking_table(md_text: str) -> list[ClaudeVerdict]:
    """解析「买入建议排序」表(列:排序|代码|名称|类型|表态|方向（1日/5日）|...)。"""
    verdicts: list[ClaudeVerdict] = []
    for line in md_text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        code = cells[1]
        if not re.fullmatch(r"\d{6}", code):    # 跳过表头/分隔行
            continue
        dir_cell = cells[5]
        d1, d5 = (dir_cell.split("/", 1) + [""])[:2] if "/" in dir_cell else (dir_cell, "")
        verdicts.append(ClaudeVerdict(
            code=code, name=cells[2], type_raw=cells[3],
            stance_raw=_strip_bold(cells[4]), dir1_raw=d1.strip(), dir5_raw=d5.strip()))
    return verdicts


def _strip_bold(s: str) -> str:
    return s.replace("**", "").strip()


# ————————————————————————————————————————————————————————————————
# 归一化(Claude 自由文本 → picks_schema 口径)
# ————————————————————————————————————————————————————————————————
def norm_type(raw: str) -> str:
    if "买入候选" in raw:
        return "买入候选"
    if "规避" in raw and "样本" not in raw:
        return "规避"
    return "检验样本"


def norm_stance(raw: str) -> str:
    """Claude 表态自由文本 → 4 枚举(买入/可参与/观望/规避)。优先级:买入>规避>可参与>观望。"""
    if "买入" in raw and "可参与" not in raw and "条件" not in raw:
        return "买入"
    if "规避" in raw:
        return "规避"
    if "可参与" in raw or ("买入" in raw and "条件" in raw):
        return "可参与"
    return "观望"


# 方向姿态:是否承诺一个可交易方向
_COMMIT_WORDS = ("偏多", "偏空")
_NONCOMMIT_WORDS = ("不给方向", "低波", "待动", "待触发", "中性", "弱")


def commits_direction(raw: str) -> bool:
    """Claude 自由文本方向是否'承诺可交易方向'。'待触发/待动/弱/不给方向/中性' = 不承诺。"""
    r = raw.strip()
    if any(w in r for w in _NONCOMMIT_WORDS):
        return False
    return any(w in r for w in _COMMIT_WORDS)


def gen_commits_direction(dir_enum: str) -> bool:
    return dir_enum in ("偏多", "偏空")


# ————————————————————————————————————————————————————————————————
# 对比
# ————————————————————————————————————————————————————————————————
# stance 保守度序(越大越谨慎),用于退化判定
_STANCE_ORDER = {"买入": 0, "可参与": 1, "观望": 2, "规避": 3}


@dataclass
class DryRunReport:
    pick_date: str
    codes: list
    provider: str
    coverage: dict = field(default_factory=dict)
    per_code: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def compare(pick_date: str, claude_verdicts: list[ClaudeVerdict], gen_units: list[dict],
            *, provider: str = "deepseek_v4pro") -> DryRunReport:
    gen_by = {u.get("code"): u for u in gen_units}
    cl_by = {v.code: v for v in claude_verdicts}
    codes = [v.code for v in claude_verdicts]

    covered = [c for c in codes if c in gen_by]
    missing = [c for c in codes if c not in gen_by]

    rows = []
    type_hit = stance_hit = posture_hit = 0
    downgrades = []      # Claude 更积极、生成器更保守
    upgrades = []        # 生成器更积极(冒进)
    for c in covered:
        v, u = cl_by[c], gen_by[c]
        c_type, c_stance = norm_type(v.type_raw), norm_stance(v.stance_raw)
        g_type, g_stance = u.get("type"), u.get("stance")
        c_commit5 = commits_direction(v.dir5_raw)
        g_commit5 = gen_commits_direction(u.get("dir_5d", ""))

        t_ok = c_type == g_type
        s_ok = c_stance == g_stance
        p_ok = c_commit5 == g_commit5
        type_hit += t_ok
        stance_hit += s_ok
        posture_hit += p_ok

        # 退化:stance 保守度差异
        d = _STANCE_ORDER.get(g_stance, 2) - _STANCE_ORDER.get(c_stance, 2)
        if d >= 2:
            downgrades.append(c)
        elif d <= -2:
            upgrades.append(c)

        rows.append({
            "code": c, "name": v.name,
            "type": {"claude": c_type, "gen": g_type, "same": t_ok},
            "stance": {"claude": c_stance, "gen": g_stance, "same": s_ok,
                       "claude_raw": v.stance_raw},
            "dir5_posture": {"claude_commits": c_commit5, "gen_commits": g_commit5,
                             "same": p_ok, "claude_raw": v.dir5_raw, "gen": u.get("dir_5d")},
        })

    n = len(covered)
    rep = DryRunReport(pick_date=pick_date, codes=codes, provider=provider)
    rep.coverage = {"claude_codes": len(codes), "generated": n, "missing": missing}
    rep.per_code = rows
    rep.summary = {
        "type_agreement": round(type_hit / n, 3) if n else None,
        "stance_agreement": round(stance_hit / n, 3) if n else None,
        "dir5_posture_agreement": round(posture_hit / n, 3) if n else None,
        "downgrades": downgrades, "upgrades": upgrades,
    }
    if missing:
        rep.notes.append(f"生成器未覆盖(缺研判):{missing}")
    rep.notes.append("方向姿态为粗比(Claude 自由文本 vs 枚举);表态已归一到 4 枚举。")
    return rep


def render_md(rep: DryRunReport) -> str:
    s = rep.summary
    L = [f"# 历史日 dry-run 对比:headless 生成器 vs Claude · {rep.pick_date}",
         "",
         "> ⚠️ 测试环境研究模拟,非投资建议。dry-run 不落生产、不覆盖已有 md/json。",
         f"> 生成器 provider:{rep.provider}",
         "",
         f"- 候选(Claude PICKS):{rep.coverage['claude_codes']} 只　生成器覆盖:{rep.coverage['generated']} 只",
         f"- **type 一致率:{_pct(s['type_agreement'])}**",
         f"- **stance 一致率(归一 4 枚举):{_pct(s['stance_agreement'])}**",
         f"- **方向姿态一致率(是否承诺方向·粗比):{_pct(s['dir5_posture_agreement'])}**",
         f"- 明显退化(生成器过保守):{s['downgrades'] or '无'}　过冒进:{s['upgrades'] or '无'}",
         ""]
    for note in rep.notes:
        L.append(f"> {note}")
    L += ["", "## 逐票对比", "",
          "| 代码 | 名称 | type(C/生) | stance(C/生) | 方向姿态5日(C承诺?/生承诺?) |",
          "|---|---|---|---|---|"]
    for r in rep.per_code:
        t = f"{r['type']['claude']}/{r['type']['gen']}{'✓' if r['type']['same'] else '✗'}"
        st = f"{r['stance']['claude']}/{r['stance']['gen']}{'✓' if r['stance']['same'] else '✗'}"
        p = (f"{'是' if r['dir5_posture']['claude_commits'] else '否'}"
             f"/{'是' if r['dir5_posture']['gen_commits'] else '否'}"
             f"{'✓' if r['dir5_posture']['same'] else '✗'}"
             f" (C:{r['dir5_posture']['claude_raw']} 生:{r['dir5_posture']['gen']})")
        L.append(f"| {r['code']} | {r['name']} | {t} | {st} | {p} |")
    return "\n".join(L)


def _pct(v) -> str:
    return "—" if v is None else f"{v:.1%}"


def report_to_dict(rep: DryRunReport) -> dict:
    return {"pick_date": rep.pick_date, "provider": rep.provider,
            "coverage": rep.coverage, "summary": rep.summary,
            "per_code": rep.per_code, "notes": rep.notes}


# ————————————————————————————————————————————————————————————————
# CLI
# ————————————————————————————————————————————————————————————————
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="历史日 dry-run:生成器 vs Claude(不落生产)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--claude-md", required=True, help="当时 Claude 选股 md 路径")
    ap.add_argument("--gen-units", required=True, help="生成器研判 JSON(analysis list)路径")
    ap.add_argument("--provider", default="deepseek_v4pro")
    ap.add_argument("--out-dir", required=True, help="报告输出目录(隔离)")
    args = ap.parse_args(argv)

    md_text = Path(args.claude_md).read_text(encoding="utf-8")
    verdicts = parse_ranking_table(md_text)
    gen_units = json.loads(Path(args.gen_units).read_text(encoding="utf-8"))

    rep = compare(args.date, verdicts, gen_units, provider=args.provider)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"dryrun_vs_claude_{args.date}.json").write_text(
        json.dumps(report_to_dict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_md(rep)
    (out_dir / f"dryrun_vs_claude_{args.date}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
