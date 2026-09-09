"""每日选股结构化产物（`每日选股` view）落盘 Tool —— 方案 P1 实现。

配套设计：docs/计划/2026-09-09_选股产物结构化与程序化_方案.md

职责边界（方案 §4.1）：本 Tool 只固化**可程序化**的三件事——
  1) 客观字段回填：名称/行业/收盘价/涨跌幅/命中策略分值，一律代码从 record + 策略视图 join，
     Agent 不手打（根治"远端只有代码没名称""手打错价"）；
  2) 结构化落盘：按 canonical schema 原子写 `data/analysis/<pick_date>/每日选股.json`
     （非 6 位文件名 → `import_to_db.collect_date` 自动归入 `views["每日选股"]`
      → `upload.py` 自动切分片 `__view__:每日选股`，**传输层零改动**）；
  3) 校验：枚举成员 / 必填非空 / code 格式 / 情绪质量闸门 / PICKS 一致性 / 防未来。
**一切研判仍是 Agent 的活**（表态/方向/理由/风险/盯点），由输入 analysis 提供。

受控枚举与纯结构校验骨架复用 `tools.analysis.picks_schema`（P0 骨架，单一真源）。
本模块在其上补：客观回填、策略 join、PICKS 锚点一致性、record 防未来交叉核对、原子写、md 渲染。

CLI：
    python -m tools.analysis.write_picks --date 2026-09-09 --analysis /tmp/picks_input.json
    python -m tools.analysis.write_picks --date 2026-09-09 --status skipped \
        --skip-reason "闭环未在门控窗口内完成"
校验不过 → 打印 errors + 非零退出（不带错落盘）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from tools.analysis import picks_schema as ps

# —— 命中策略视图里"榜单"可能的键名（各策略结构不统一，按序探测）——
_LIST_KEYS = ("入选清单", "top", "入选", "list", "candidates", "选中")
# —— 单票分值可能的键名（顶层或嵌在 特征/明细/council 里）——
_SCORE_KEYS = ("综合分", "score", "动量分", "得分", "分值", "强度")
_NESTED_SCORE_PARENTS = ("特征", "明细", "council")

# 研判字段：Agent 提供、代码原样透传（客观字段由代码回填，不在此列）
_JUDGE_FIELDS = (
    "type", "buy_rank", "stance", "stance_qualifier",
    "dir_1d", "dir_1d_conf", "dir_5d", "dir_5d_conf",
    "sentiment_quality", "key_reason", "key_risk", "alpha_beta",
    "watch_points", "detail_anchor",
)


# ————————————————————————————————————————————————————————————————
# 客观字段回填
# ————————————————————————————————————————————————————————————————
def _default_record_loader(code: str, pick_date: str):
    """默认 record 加载：读 `data/analysis/<pick_date>/<code>.json`。缺则 None（不触网）。

    选股在 serialize 之后跑，record 通常已落地；缺失时回填字段留空、由校验报错，
    不静默落"只有代码"。（测试可注入桩 loader 免磁盘依赖。）
    """
    try:
        from tools.store import repo as store
        return store.get_record(code, date=pick_date)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _load_code_industry() -> dict:
    """全A 代码→申万一级行业映射（record.meta.industry 缺时的回退源）。缺文件 → 空。"""
    try:
        from tools.config import settings
        p = settings.PROJECT_ROOT / "config" / "code_industry.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_strategy_views(analysis_dir: Path, pick_date: str) -> dict:
    """读当日全部策略 view（顶层非 6 位 json）为 {view名: obj}，供命中策略 join。缺目录 → 空。"""
    day = Path(analysis_dir) / pick_date
    out: dict = {}
    if not day.is_dir():
        return out
    for p in sorted(day.glob("*.json")):
        stem = p.stem
        if len(stem) == 6 and stem.isdigit():
            continue  # 6 位=record，非策略 view
        try:
            out[stem] = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return out


def _extract_ranklist(view: dict) -> list:
    """从策略 view 里取榜单 list（探测常见键名）。取不到 → 空 list。"""
    if not isinstance(view, dict):
        return []
    for k in _LIST_KEYS:
        v = view.get(k)
        if isinstance(v, list):
            return v
    return []


def _extract_score(item: dict):
    """从榜单单元里取一个代表分值（顶层键 → 嵌套 特征/明细/council 键）。取不到 → None。"""
    if not isinstance(item, dict):
        return None
    for k in _SCORE_KEYS:
        if isinstance(item.get(k), (int, float)):
            return item[k]
    for parent in _NESTED_SCORE_PARENTS:
        sub = item.get(parent)
        if isinstance(sub, dict):
            for k in _SCORE_KEYS:
                if isinstance(sub.get(k), (int, float)):
                    return sub[k]
    return None


def _join_strategies(code: str, hints, strategy_views: dict) -> list:
    """据 Agent 的 strategies_hint（name[,rank]）到对应策略 view join rank/score。

    - rank：优先用 view 榜单里 code 的 1-based 位次；code 不在榜（或无该 view）→ 用 hint 的 rank。
    - score：从榜单单元回填（探测常见分值键）；取不到 → None（schema 允许 null）。
    Agent 只提示"命中了哪些策略"，分值一律代码回填、不手打。
    """
    out: list = []
    for h in (hints or []):
        if isinstance(h, str):
            h = {"name": h}
        if not isinstance(h, dict):
            continue
        name = h.get("name")
        if not name:
            continue
        rank = h.get("rank")
        score = None
        view = strategy_views.get(name)
        ranklist = _extract_ranklist(view) if view else []
        for idx, item in enumerate(ranklist):
            if isinstance(item, dict) and item.get("code") == code:
                rank = idx + 1  # 榜单位次权威，覆盖 hint
                score = _extract_score(item)
                break
        out.append({"name": name, "rank": rank, "score": score})
    return out


def _backfill_pick(unit: dict, pick_date: str, record_loader, code_industry: dict,
                   strategy_views: dict) -> tuple[dict, list[str]]:
    """把 Agent 单票研判 unit 回填客观字段，产出 canonical pick + 该票的软告警（record 防未来）。"""
    warns: list[str] = []
    code = str(unit.get("code", "")).strip()
    out: dict = {"code": code}

    rec = record_loader(code, pick_date) if code else None
    meta = (rec or {}).get("meta") or {}
    snap = (rec or {}).get("snapshot") or {}

    # 防未来交叉核对：回填的 record.as_of 不得晚于 pick_date（抓到更晚数据 → 告警）
    rec_asof = meta.get("as_of")
    if rec_asof and rec_asof > pick_date:
        warns.append(f"{code} record.as_of({rec_asof}) 晚于 pick_date({pick_date})（疑似未来数据）")

    out["name"] = meta.get("name")
    out["industry"] = meta.get("industry") or code_industry.get(code)
    out["close"] = snap.get("close")
    out["pct_chg"] = snap.get("pct_chg")
    out["strategies"] = _join_strategies(code, unit.get("strategies_hint"), strategy_views)

    # 研判字段：Agent 提供，原样透传（缺省给安全默认，交由校验按枚举判非法）
    for f in _JUDGE_FIELDS:
        if f in unit:
            out[f] = unit[f]
    out.setdefault("stance_qualifier", "")
    out.setdefault("alpha_beta", "")
    out.setdefault("watch_points", [])
    return out, warns


# ————————————————————————————————————————————————————————————————
# 组装 + 校验
# ————————————————————————————————————————————————————————————————
def build_picks_json(
    pick_date: str,
    analysis: list,
    *,
    status: str = "ok",
    skip_reason: str | None = None,
    market_regime: str = "",
    generator: str = "daily-stock-selection",
    as_of: str | None = None,
    predict_for: str | None = None,
    source_md: str | None = None,
    market_forecast_ref=None,
    drift_note: str | None = None,
    analysis_dir: Path | None = None,
    record_loader=None,
    now: _dt.datetime | None = None,
) -> tuple[dict, list[str]]:
    """回填客观字段 + 组装 canonical doc + 校验。返回 (doc, errors)。errors 非空即未过。

    - 客观回填：每个 code 取 record 的 name/industry/close/pct_chg；strategies 从策略 view join。
    - 纯组装 + 校验，不做研判、不联网（record_loader 可注入以脱离磁盘）。
    """
    as_of = as_of or pick_date
    record_loader = record_loader or _default_record_loader
    if analysis_dir is None:
        from tools.config import settings
        analysis_dir = settings.PROJECT_ROOT / "data" / "analysis"
    now = now or _dt.datetime.now().astimezone()

    if predict_for is None:
        from tools.collectors import calendar as _cal
        predict_for = _cal.next_trading_day(pick_date, allow_fetch=False)

    code_industry = _load_code_industry()
    strategy_views = _load_strategy_views(analysis_dir, pick_date)

    picks: list = []
    warns: list[str] = []
    if status != "skipped":
        for unit in (analysis or []):
            pick, w = _backfill_pick(unit, pick_date, record_loader, code_industry, strategy_views)
            picks.append(pick)
            warns.extend(w)

    meta = {
        "pick_date": pick_date,
        "predict_for": predict_for,
        "as_of": as_of,
        "generated_at": now.isoformat(timespec="seconds"),
        "generator": generator,
        "status": status,
        "skip_reason": skip_reason,
        "market_regime": market_regime,
        "market_forecast_ref": market_forecast_ref,
        "picks_count": len(picks),
        "source_md": source_md or f"docs/每日分析/选股/{pick_date}.md",
        "drift_note": drift_note,
    }
    doc = {"schema_version": ps.SCHEMA_VERSION, "meta": meta, "picks": picks, "review": None}
    if warns:
        # 软告警随 doc 留痕（不阻断），供人排查"回填时抓到更晚数据"等
        meta["_backfill_warnings"] = warns

    errors = validate_picks(doc)
    return doc, errors


def validate_picks(doc: dict, *, picks_anchor=None) -> list[str]:
    """结构/枚举/一致性校验（复用 picks_schema 骨架）+ PICKS 锚点一致性。返回错误列表（空=通过）。

    picks_anchor：若给（可迭代 code 集合 / 逗号串），断言其与 picks[].code 全集一致。
    """
    errors = list(ps.validate_picks(doc))

    if picks_anchor is not None:
        if isinstance(picks_anchor, str):
            anchor = {c.strip() for c in picks_anchor.split(",") if c.strip() and c.strip() != "none"}
        else:
            anchor = {str(c).strip() for c in picks_anchor}
        codes = {str(p.get("code")) for p in (doc.get("picks") or [])}
        if anchor != codes:
            errors.append(f"PICKS 锚点与 picks 不一致：锚点={sorted(anchor)} picks={sorted(codes)}")

    return errors


# ————————————————————————————————————————————————————————————————
# 原子落盘 + md 渲染
# ————————————————————————————————————————————————————————————————
def _picks_path(analysis_dir: Path, pick_date: str) -> Path:
    return Path(analysis_dir) / pick_date / "每日选股.json"


def write_picks(doc: dict, analysis_dir: Path, pick_date: str) -> str:
    """原子写 `data/analysis/<pick_date>/每日选股.json`（tmp + os.replace）。返回路径字符串。"""
    p = _picks_path(analysis_dir, pick_date)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return str(p)


def picks_anchor_line(doc: dict) -> str:
    """据 doc 生成 md 机读锚点行（与 JSON 同源）。空/skipped → `PICKS: none`。"""
    codes = [str(p.get("code")) for p in (doc.get("picks") or []) if p.get("code")]
    inner = ",".join(codes) if codes else "none"
    return f"<!-- PICKS: {inner} -->"


def render_picks_md_tables(doc: dict) -> str:
    """据 doc 渲染 md 的 PICKS 锚点 + 选股结果/买入排序表，供 SKILL 贴入 md（保证与 JSON 同源）。"""
    lines = [picks_anchor_line(doc)]
    meta = doc.get("meta") or {}
    if meta.get("status") == "skipped":
        lines.append("")
        lines.append(f"> 今日选股跳过：{meta.get('skip_reason') or '（未注明原因）'}")
        return "\n".join(lines)

    picks = sorted((doc.get("picks") or []),
                   key=lambda p: (p.get("buy_rank") is None, p.get("buy_rank") or 0))
    lines.append("")
    lines.append("| 买入排序 | 代码 | 名称 | 行业 | 收盘价 | 涨跌% | 命中策略（分值） | 表态 | 1日 | 5日 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for p in picks:
        strat = "；".join(
            f"{s.get('name')}#{s.get('rank')}"
            + (f"({s['score']})" if s.get("score") is not None else "")
            for s in (p.get("strategies") or [])
        ) or "—"
        stance = p.get("stance", "")
        if p.get("stance_qualifier"):
            stance = f"{stance}（{p['stance_qualifier']}）"
        d1 = f"{p.get('dir_1d','')}/{p.get('dir_1d_conf','')}"
        d5 = f"{p.get('dir_5d','')}/{p.get('dir_5d_conf','')}"
        lines.append(
            f"| {p.get('buy_rank','')} | {p.get('code','')} | {p.get('name','')} | "
            f"{p.get('industry','') or ''} | {p.get('close','')} | {p.get('pct_chg','')} | "
            f"{strat} | {stance} | {d1} | {d5} |"
        )
    return "\n".join(lines)


# ————————————————————————————————————————————————————————————————
# CLI
# ————————————————————————————————————————————————————————————————
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="把 Agent 逐票研判组装成 canonical 每日选股.json 并校验落盘（客观字段代码回填）")
    ap.add_argument("--date", required=True, help="选股执行日 D，YYYY-MM-DD")
    ap.add_argument("--analysis", help="Agent 逐票研判 JSON 文件（list[dict]）；skipped 态可省")
    ap.add_argument("--status", default="ok", choices=sorted(ps.STATUS))
    ap.add_argument("--skip-reason", help="status=skipped 时必填")
    ap.add_argument("--market-regime", default="", help="市场环境定性（展示用自由文本）")
    ap.add_argument("--generator", default="daily-stock-selection")
    ap.add_argument("--as-of", help="防未来锚点，默认=--date")
    ap.add_argument("--predict-for", help="预测目标日，默认=交易日历推 D+1")
    ap.add_argument("--source-md", help="对应 md 路径")
    ap.add_argument("--picks-anchor", help="md PICKS 锚点 code 串，给则校验与 picks 一致")
    ap.add_argument("--analysis-dir", help="data/analysis 根，默认项目内")
    ap.add_argument("--print-md", action="store_true", help="额外打印 render_picks_md_tables 结果")
    ap.add_argument("--dry-run", action="store_true", help="只校验、不落盘")
    args = ap.parse_args(argv)

    analysis = []
    if args.analysis:
        analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
        if isinstance(analysis, dict):  # 容错：单票也接受
            analysis = [analysis]

    analysis_dir = Path(args.analysis_dir) if args.analysis_dir else None
    doc, errors = build_picks_json(
        args.date, analysis,
        status=args.status, skip_reason=args.skip_reason,
        market_regime=args.market_regime, generator=args.generator,
        as_of=args.as_of, predict_for=args.predict_for, source_md=args.source_md,
        analysis_dir=analysis_dir,
    )
    if args.picks_anchor:
        errors = validate_picks(doc, picks_anchor=args.picks_anchor)

    if errors:
        print("校验未通过（不落盘）：", file=sys.stderr)
        for e in errors:
            print("  -", e, file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        if analysis_dir is None:
            from tools.config import settings
            analysis_dir = settings.PROJECT_ROOT / "data" / "analysis"
        path = write_picks(doc, analysis_dir, args.date)
        print(f"已写 {path}（status={doc['meta']['status']} picks={doc['meta']['picks_count']}）")
    if args.print_md:
        print(render_picks_md_tables(doc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
