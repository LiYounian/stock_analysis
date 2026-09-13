"""双跑对比框架(迁移前闸门工具)。

设计:docs/计划/2026-09-13_headless逐票研判生成器与双跑框架_P2实现计划.md §3
      + 架构设计 §3.3(B)。**P2 只出对比证据 + 建议,不自动裁决切主**。

同一票、同一输入,用两个配置(arm)各跑一遍研判:
  - arm = {provider_id, enable_thinking};典型:DeepSeek vs 千问,或同 provider think 开/关。
两路研判 → (1) 受控枚举字段**逐字段一致率** + 分歧清单;(2) 叙事字段词重叠相似度(辅助);
(3) 能算则**前瞻收益**对比(据 record snapshot.close 跨交易日算 T+h fwd_ret,验哪一路方向更对)。

裁决:字段一致 → 可采纳;分歧 → 标"待人工复核"。P2 不自动切,只给报告 + 建议。
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tools.analysis import deep_analysis as da
from tools.analysis import deep_analysis_inputs as di

# 逐字段一致率的受控枚举字段(可精确比)
ENUM_FIELDS = ("type", "stance", "dir_1d", "dir_1d_conf", "dir_5d", "dir_5d_conf",
               "sentiment_quality")
# 叙事字段(算词重叠相似度作辅助信号,不精确 diff)
NARRATIVE_FIELDS = ("key_reason", "key_risk", "alpha_beta")


@dataclass
class Arm:
    """一个对比臂 = 一套配置。"""
    label: str
    provider_id: str | None = None
    enable_thinking: bool | None = None
    client=None                       # 可注入桩(测试/复用)


@dataclass
class DualReport:
    pick_date: str
    codes: list
    arm_a: str
    arm_b: str
    per_code: list = field(default_factory=list)      # 每票 diff
    field_agreement: dict = field(default_factory=dict)  # 每字段一致率
    overall_agreement: float = 0.0
    forward: dict = field(default_factory=dict)        # 前瞻收益对比(能算则)
    notes: list = field(default_factory=list)


# ————————————————————————————————————————————————————————————————
# 字段 diff
# ————————————————————————————————————————————————————————————————
def _tokens(s: str) -> set:
    """中文按 2-gram + 英文/数字按词,做词重叠相似度用(轻量、无依赖)。"""
    s = (s or "").strip()
    if not s:
        return set()
    import re
    toks = set(re.findall(r"[A-Za-z0-9]+", s.lower()))
    zh = re.sub(r"[^一-鿿]", "", s)
    toks |= {zh[i:i + 2] for i in range(len(zh) - 1)}
    return toks


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def diff_units(unit_a: dict, unit_b: dict) -> dict:
    """比两路对同一票的研判 unit:枚举逐字段一致 + 叙事相似度。"""
    fields = {}
    disagreements = []
    for f in ENUM_FIELDS:
        va, vb = unit_a.get(f), unit_b.get(f)
        same = va == vb
        fields[f] = {"a": va, "b": vb, "same": same}
        if not same:
            disagreements.append(f)
    narrative = {f: round(_jaccard(unit_a.get(f, ""), unit_b.get(f, "")), 3)
                 for f in NARRATIVE_FIELDS}
    n_same = sum(1 for f in ENUM_FIELDS if fields[f]["same"])
    return {
        "code": unit_a.get("code") or unit_b.get("code"),
        "fields": fields,
        "disagreements": disagreements,
        "enum_agreement": round(n_same / len(ENUM_FIELDS), 3),
        "narrative_similarity": narrative,
    }


# ————————————————————————————————————————————————————————————————
# 前瞻收益(据 record snapshot.close 跨交易日;best-effort,数据不足标 unavailable)
# ————————————————————————————————————————————————————————————————
def _close_on(code: str, date: str, data_root: Path | None) -> float | None:
    rec = di.load_record(code, date, data_root)
    close = ((rec or {}).get("snapshot") or {}).get("close")
    return float(close) if isinstance(close, (int, float)) else None


def forward_return(code: str, pick_date: str, horizon: int, data_root: Path | None) -> float | None:
    """T+h 前瞻收益 = close[T+h]/close[T]-1;任一 close 缺则 None(数据不足)。"""
    from tools.collectors import calendar as cal
    c0 = _close_on(code, pick_date, data_root)
    if c0 is None or c0 == 0:
        return None
    d = pick_date
    for _ in range(horizon):
        try:
            d = cal.next_trading_day(d, allow_fetch=False)
        except Exception:
            return None
        if not d:
            return None
    ch = _close_on(code, d, data_root)
    if ch is None:
        return None
    return ch / c0 - 1.0


_DIR_SIGN = {"偏多": 1, "偏空": -1, "中性": 0, "低波待动": 0, "不给方向": None}


def _dir_hit(direction: str, fwd: float | None) -> bool | None:
    """方向是否兑现:偏多且 fwd>0 / 偏空且 fwd<0 记命中;不给方向/中性 或数据缺 → None(不计)。"""
    if fwd is None:
        return None
    sign = _DIR_SIGN.get(direction)
    if not sign:            # None(不给方向)或 0(中性/低波待动)不计入命中统计
        return None
    return (sign > 0 and fwd > 0) or (sign < 0 and fwd < 0)


def forward_compare(units_a: list, units_b: list, pick_date: str,
                    data_root: Path | None, *, horizons=(1, 5)) -> dict:
    """比两路"给方向的票事后是否更对"。返回每 horizon 的命中率(能算的票)+ 数据可得性。"""
    a_by = {u["code"]: u for u in units_a if u.get("code")}
    b_by = {u["code"]: u for u in units_b if u.get("code")}
    codes = [c for c in a_by if c in b_by]

    out: dict = {"horizons": {}, "note": ""}
    fwd_cache: dict = {}
    for h in horizons:
        rows = []
        a_hit = a_tot = b_hit = b_tot = 0
        avail = 0
        for c in codes:
            fwd = fwd_cache.get((c, h))
            if fwd is None and (c, h) not in fwd_cache:
                fwd = forward_return(c, pick_date, h, data_root)
                fwd_cache[(c, h)] = fwd
            if fwd is not None:
                avail += 1
            ha = _dir_hit(a_by[c].get("dir_1d" if h == 1 else "dir_5d"), fwd)
            hb = _dir_hit(b_by[c].get("dir_1d" if h == 1 else "dir_5d"), fwd)
            if ha is not None:
                a_tot += 1
                a_hit += int(ha)
            if hb is not None:
                b_tot += 1
                b_hit += int(hb)
            rows.append({"code": c, "fwd_ret": None if fwd is None else round(fwd, 4),
                         "a_dir": a_by[c].get("dir_1d" if h == 1 else "dir_5d"),
                         "b_dir": b_by[c].get("dir_1d" if h == 1 else "dir_5d"),
                         "a_hit": ha, "b_hit": hb})
        out["horizons"][f"T+{h}"] = {
            "fwd_available": avail, "codes_total": len(codes),
            "arm_a_hit_rate": round(a_hit / a_tot, 3) if a_tot else None,
            "arm_b_hit_rate": round(b_hit / b_tot, 3) if b_tot else None,
            "arm_a_scored": a_tot, "arm_b_scored": b_tot,
            "rows": rows,
        }
    if all(v["fwd_available"] == 0 for v in out["horizons"].values()):
        out["note"] = "前瞻收益数据不足(未来交易日 record 尚未落地),本次仅出字段一致率。"
    return out


# ————————————————————————————————————————————————————————————————
# 编排
# ————————————————————————————————————————————————————————————————
def run_arm(arm: Arm, pick_date: str, codes: list, *, data_root=None,
            experience_base=None, strategies_hint_map=None) -> list:
    """跑一个臂,返回 analysis unit 列表(含 error 票的占位 unit 也保留 code 便于对齐)。"""
    results = da.generate(
        pick_date, codes, client=arm.client, provider_id=arm.provider_id,
        enable_thinking=arm.enable_thinking, data_root=data_root,
        experience_base=experience_base, strategies_hint_map=strategies_hint_map)
    return [r.unit for r in results]


def compare(
    pick_date: str,
    codes: list,
    arm_a: Arm,
    arm_b: Arm,
    *,
    data_root=None,
    experience_base=None,
    strategies_hint_map=None,
    horizons=(1, 5),
    units_a=None,
    units_b=None,
) -> DualReport:
    """双跑对比主入口。units_a/units_b 可直接注入(测试/复用已跑结果)。"""
    if units_a is None:
        units_a = run_arm(arm_a, pick_date, codes, data_root=data_root,
                          experience_base=experience_base, strategies_hint_map=strategies_hint_map)
    if units_b is None:
        units_b = run_arm(arm_b, pick_date, codes, data_root=data_root,
                          experience_base=experience_base, strategies_hint_map=strategies_hint_map)

    a_by = {u.get("code"): u for u in units_a}
    b_by = {u.get("code"): u for u in units_b}
    common = [c for c in codes if c in a_by and c in b_by]

    per_code = [diff_units(a_by[c], b_by[c]) for c in common]

    # 每字段一致率 + 总一致率
    field_agree: dict = {}
    for f in ENUM_FIELDS:
        same = sum(1 for d in per_code if d["fields"][f]["same"])
        field_agree[f] = round(same / len(per_code), 3) if per_code else None
    total_cells = len(per_code) * len(ENUM_FIELDS)
    total_same = sum(sum(1 for f in ENUM_FIELDS if d["fields"][f]["same"]) for d in per_code)
    overall = round(total_same / total_cells, 3) if total_cells else 0.0

    rep = DualReport(
        pick_date=pick_date, codes=codes, arm_a=arm_a.label, arm_b=arm_b.label,
        per_code=per_code, field_agreement=field_agree, overall_agreement=overall)
    rep.forward = forward_compare(units_a, units_b, pick_date, data_root, horizons=horizons)
    missing = [c for c in codes if c not in common]
    if missing:
        rep.notes.append(f"未能双跑对齐的票(某臂缺研判):{missing}")
    return rep


def render_report_md(rep: DualReport) -> str:
    """渲染对比报告 md。"""
    L = [f"# 双跑对比报告 · {rep.pick_date}",
         "",
         f"- 臂 A:{rep.arm_a}　臂 B:{rep.arm_b}",
         f"- 候选票:{len(rep.codes)}　成功对齐:{len(rep.per_code)}",
         f"- **总字段一致率:{rep.overall_agreement:.1%}**",
         ""]
    for n in rep.notes:
        L.append(f"> {n}")
    L += ["", "## 逐字段一致率", "", "| 字段 | 一致率 |", "|---|---|"]
    for f, v in rep.field_agreement.items():
        L.append(f"| {f} | {'—' if v is None else f'{v:.1%}'} |")

    L += ["", "## 逐票分歧", "", "| 代码 | 枚举一致率 | 分歧字段 |", "|---|---|---|"]
    for d in rep.per_code:
        dis = "、".join(d["disagreements"]) or "(全一致)"
        L.append(f"| {d['code']} | {d['enum_agreement']:.1%} | {dis} |")

    L += ["", "## 前瞻收益对比(方向命中率)", ""]
    if rep.forward.get("note"):
        L.append(f"> {rep.forward['note']}")
    L += ["", "| Horizon | 数据可得 | 臂A命中率(计分数) | 臂B命中率(计分数) |",
          "|---|---|---|---|"]
    for h, v in rep.forward.get("horizons", {}).items():
        a = "—" if v["arm_a_hit_rate"] is None else f"{v['arm_a_hit_rate']:.1%}({v['arm_a_scored']})"
        b = "—" if v["arm_b_hit_rate"] is None else f"{v['arm_b_hit_rate']:.1%}({v['arm_b_scored']})"
        L.append(f"| {h} | {v['fwd_available']}/{v['codes_total']} | {a} | {b} |")
    return "\n".join(L)


def report_to_dict(rep: DualReport) -> dict:
    return {
        "pick_date": rep.pick_date, "arm_a": rep.arm_a, "arm_b": rep.arm_b,
        "codes": rep.codes, "overall_agreement": rep.overall_agreement,
        "field_agreement": rep.field_agreement, "per_code": rep.per_code,
        "forward": rep.forward, "notes": rep.notes,
    }


# ————————————————————————————————————————————————————————————————
# CLI(dry-run:落隔离目录,不碰生产)
# ————————————————————————————————————————————————————————————————
def _parse_arm(spec: str, label_default: str) -> Arm:
    """arm spec 形如 'deepseek_v4pro' / 'deepseek_v4pro:think_on' / 'deepseek_v4pro:think_off'。"""
    parts = spec.split(":")
    pid = parts[0] or None
    et = None
    if len(parts) > 1:
        et = {"think_on": True, "think_off": False}.get(parts[1])
    label = spec
    return Arm(label=label or label_default, provider_id=pid, enable_thinking=et)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="双跑对比框架(P2;出对比报告,不切生产)")
    ap.add_argument("--date", required=True)
    ap.add_argument("--codes", required=True, help="候选票,逗号分隔")
    ap.add_argument("--arm-a", required=True, help="如 deepseek_v4pro:think_off")
    ap.add_argument("--arm-b", required=True, help="如 deepseek_v4pro:think_on / qwen_max")
    ap.add_argument("--data-root", help="data/analysis 根(dry-run 指向生产数据只读)")
    ap.add_argument("--experience-base")
    ap.add_argument("--out-dir", required=True, help="报告输出目录(隔离,不碰生产)")
    args = ap.parse_args(argv)

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    data_root = Path(args.data_root) if args.data_root else None
    exp_base = Path(args.experience_base) if args.experience_base else None
    arm_a = _parse_arm(args.arm_a, "A")
    arm_b = _parse_arm(args.arm_b, "B")

    rep = compare(args.date, codes, arm_a, arm_b, data_root=data_root, experience_base=exp_base)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"dualrun_{args.date}.json").write_text(
        json.dumps(report_to_dict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    md = render_report_md(rep)
    (out_dir / f"dualrun_{args.date}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n已写:{out_dir}/dualrun_{args.date}.(json|md)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
