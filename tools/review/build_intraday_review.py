"""午盘/日内选股复盘编排 CLI（隔夜口径：D 收盘买 → D+1 11:30 快照卖 → 绝对收益 + α + 归因）。

  loaders(读产物·复用) → intraday_cadence(隔夜撮合收益+基准) → attribution.classify(规则归因·复用)
  → 本线渲染（MD·docs/每日分析/复盘/午盘隔夜复盘_<date>.md + CSV·data/analysis/backtest/
  intraday_overnight_scorecard.csv·幂等 upsert）

与尾盘大复盘（build_grand_review.py）**产物单独不共表**：本 CSV 冻结列独立、命名区分既有"下午α口径"
（intraday_review 的 午盘_<date>.md / 午盘Q线复盘_*.md）。搭路径本轮：脚本+测试跑通即可，
全量 N 日运行等用户发令。防未来：只用 ≤当前时点数据；次日快照未出 → pending 留 None（再跑=回填幂等）。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from tools.review.attribution import classify
from tools.review.intraday_cadence import OvernightLabels, compute_all, to_attr_shim
from tools.review.loaders import load_market_context, load_picks
from tools.review.types import Pick

logger = logging.getLogger("review.build_intraday_review")

# —— 冻结列（下游/web 逐字引用；勿改列名/顺序，新增走末尾）——
OVERNIGHT_COLUMNS = [
    "date", "code", "name", "version_tag", "来源", "角色", "board",
    "buy_price", "sell_price", "sell_src", "degraded",
    "r_overnight", "market_overnight", "alpha_overnight", "win", "status",
    "correct_bucket", "wrong_bucket",
]


def to_row(pick: Pick, lab: OvernightLabels, correct: str | None, wrong: str | None) -> dict:
    """逐票 → 冻结 schema 一行。"""
    return {
        "date": pick.date, "code": pick.code, "name": pick.name,
        "version_tag": pick.version_tag, "来源": pick.来源, "角色": pick.角色, "board": pick.board,
        "buy_price": lab.buy_price, "sell_price": lab.sell_price, "sell_src": lab.sell_src,
        "degraded": lab.degraded, "r_overnight": lab.r_overnight,
        "market_overnight": lab.market_overnight, "alpha_overnight": lab.alpha_overnight,
        "win": lab.win, "status": lab.status,
        "correct_bucket": correct, "wrong_bucket": wrong,
    }


def review_date(date: str, data_root: str | None = None, include_curated: bool = True) -> list[dict]:
    """单日 → 逐票行（隔夜收益 + 规则归因）。"""
    picks = load_picks(date, data_root=data_root, include_curated=include_curated)
    if not picks:
        logger.warning("date=%s 无 picks（跳过）", date)
        return []
    market_ctx = load_market_context(date, data_root=data_root)
    labeled = compute_all(picks, data_root=data_root)
    rows: list[dict] = []
    for pick, lab in labeled:
        attr = classify(pick, to_attr_shim(lab), market_ctx=market_ctx)
        rows.append(to_row(pick, lab, attr.correct_bucket, attr.wrong_bucket))
    return rows


# ───────────────────── 汇总统计 ─────────────────────
def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows: list[dict]) -> dict:
    """隔夜口径聚合：收益榜=r_overnight（settled+degraded）·胜率=win·pending/无数据单列。"""
    total = len(rows)
    settled = [r for r in rows if r["r_overnight"] is not None]
    wins = [r for r in settled if r["win"] is True]
    pending = [r for r in rows if r["status"] == "pending"]
    degraded = [r for r in rows if r["degraded"] is True]
    return {
        "n_total": total,
        "n_settled": len(settled),
        "n_pending": len(pending),
        "n_degraded": len(degraded),
        "胜率": round(len(wins) / len(settled), 4) if settled else None,
        "n_胜率分母": len(settled),
        "平均r_overnight": _mean(r["r_overnight"] for r in settled),
        "平均alpha_overnight": _mean(r["alpha_overnight"] for r in settled),
        "选对桶分布": dict(Counter(r["correct_bucket"] for r in rows if r["correct_bucket"])),
        "选错桶分布": dict(Counter(r["wrong_bucket"] for r in rows if r["wrong_bucket"])),
        "按版本_平均r_overnight": {
            v: _mean(r["r_overnight"] for r in settled if r["version_tag"] == v)
            for v in sorted({r["version_tag"] for r in rows})},
    }


# ───────────────────── 渲染：CSV（幂等 upsert）+ MD ─────────────────────
def default_csv_path(data_root: str | None = None) -> Path:
    from tools.review.loaders import _resolve_root
    return _resolve_root(data_root) / "analysis" / "backtest" / "intraday_overnight_scorecard.csv"


def write_csv(rows: list[dict], csv_path=None, data_root: str | None = None) -> str:
    """幂等 upsert：剔 rows 涉及的所有 date 旧行 → append → 按 date,code 排序写回（同日重跑=覆盖）。"""
    import pandas as pd
    path = Path(csv_path) if csv_path is not None else default_csv_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([{c: r.get(c) for c in OVERNIGHT_COLUMNS} for r in rows],
                       columns=OVERNIGHT_COLUMNS)
    dates = {str(r["date"]) for r in rows}
    if path.is_file() and path.stat().st_size > 0:
        old = pd.read_csv(path, dtype={"date": str, "code": str})
        for c in OVERNIGHT_COLUMNS:
            if c not in old.columns:
                old[c] = None
        old = old[OVERNIGHT_COLUMNS]
        old = old[~old["date"].astype(str).isin(dates)]
        out = pd.concat([old, new], ignore_index=True)
    else:
        out = new
    out = out.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    out.to_csv(path, index=False)
    logger.info("午盘隔夜复盘 CSV 已写 %s（date=%s·%d 行）", path, ",".join(sorted(dates)), len(rows))
    return str(path)


def _fmt(v, pct=False):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)) and pct:
        return f"{v:+.2f}%"
    return str(v)


def daily_md(date: str, rows: list[dict]) -> str:
    """每日午盘隔夜复盘 MD（逐票表 + 胜率牌）。"""
    s = summarize(rows)
    lines = [
        f"# 午盘隔夜复盘 · {date}",
        "",
        "> ⚠️ 测试环境研究模拟，非投资建议。**口径：当天收盘买入（D 收盘价）→ 次日 11:30 上午收盘"
        "快照卖出（代理『次日午盘前~11:00』）→ 隔夜绝对收益 r_overnight；α = r − 全A等权同期隔夜**。"
        "防未来严格 as-of（买只取 D 当日行、卖只取 D+1 已落盘快照）。与尾盘/下午α口径解耦。",
        "",
        "## 当日胜率牌（隔夜口径）",
        f"- 总票 {s['n_total']}｜已了结 {s['n_settled']}（含降级 {s['n_degraded']}）｜"
        f"pending {s['n_pending']}",
        f"- **胜率 {_fmt(s['胜率'])}**（分母 {s['n_胜率分母']}）｜"
        f"**平均 r_overnight {_fmt(s['平均r_overnight'], pct=True)}**｜"
        f"平均 α {_fmt(s['平均alpha_overnight'], pct=True)}",
        f"- 选对桶 {s['选对桶分布'] or '—'}｜选错桶 {s['选错桶分布'] or '—'}",
        f"- 按版本平均 r_overnight：{ {k: _fmt(v, pct=True) for k, v in s['按版本_平均r_overnight'].items()} }",
        "",
        "## 逐票",
        "| 版本 | 来源 | 角色 | 代码 | 名称 | 买入价 | 卖出价 | 卖源 | r_overnight | α | 选对 | 选错 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["version_tag"], str(x["code"]))):
        lines.append(
            f"| {r['version_tag']} | {_fmt(r['来源'])} | {_fmt(r['角色'])} | {r['code']} | {r['name']} "
            f"| {_fmt(r['buy_price'])} | {_fmt(r['sell_price'])} | {_fmt(r['sell_src'])} "
            f"| {_fmt(r['r_overnight'], pct=True)} | {_fmt(r['alpha_overnight'], pct=True)} "
            f"| {_fmt(r['correct_bucket'])} | {_fmt(r['wrong_bucket'])} | {r['status']} |")
    lines.append("")
    lines.append("> ⚠️ 单日样本不足以判定；滚动多日看买入组均值 r_overnight/α（隔夜风险纳入评价）。"
                 "『次日午盘前』锚点=11:30 上午收盘快照，略晚于口语 ~11:00，诚实标注。")
    return "\n".join(lines) + "\n"


def default_md_dir(data_root: str | None = None) -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT / "docs" / "每日分析" / "复盘"


def write_daily_md(date: str, rows: list[dict], md_dir=None) -> str:
    d = Path(md_dir) if md_dir is not None else default_md_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"午盘隔夜复盘_{date}.md"
    path.write_text(daily_md(date, rows), encoding="utf-8")
    logger.info("午盘隔夜复盘 MD 已写 %s", path)
    return str(path)


# ───────────────────── 多日编排 / CLI ─────────────────────
def run(dates: list[str], data_root: str | None = None, include_curated: bool = True,
        csv_path: str | None = None, md_dir: str | None = None, write: bool = True) -> dict:
    """多日编排。write=False → 只算不落盘（dry-run）。返回 {rows, per_day_summary}。"""
    all_rows: list[dict] = []
    per_day: dict[str, dict] = {}
    for date in dates:
        rows = review_date(date, data_root=data_root, include_curated=include_curated)
        if not rows:
            continue
        per_day[date] = summarize(rows)
        all_rows.extend(rows)
        if write:
            write_daily_md(date, rows, md_dir=md_dir)
    if write and all_rows:
        write_csv(all_rows, csv_path=csv_path, data_root=data_root)
    return {"rows": all_rows, "per_day_summary": per_day}


def _parse_dates(args) -> list[str]:
    if args.dates:
        return [d.strip() for d in args.dates.split(",") if d.strip()]
    return [args.date]


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="午盘/日内选股复盘（隔夜口径：D收盘买→D+1 11:30卖）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--date", help="单个复盘日 YYYY-MM-DD")
    g.add_argument("--dates", help="逗号分隔多日")
    ap.add_argument("--data-root", help="data/ 目录（worktree 指主仓 data/）")
    ap.add_argument("--out", help="CSV 路径，缺省 data/analysis/backtest/intraday_overnight_scorecard.csv")
    ap.add_argument("--md-dir", help="每日复盘 MD 目录，缺省 docs/每日分析/复盘/")
    ap.add_argument("--no-curated", action="store_true", help="不含统筹精选 v2/v3 对照版")
    ap.add_argument("--dry-run", action="store_true", help="只算不落盘（打印每日汇总）")
    args = ap.parse_args(argv)

    out = run(_parse_dates(args), data_root=args.data_root,
              include_curated=not args.no_curated, csv_path=args.out,
              md_dir=args.md_dir, write=not args.dry_run)
    print(json.dumps({"n_rows": len(out["rows"]), "per_day_summary": out["per_day_summary"]},
                     ensure_ascii=False, indent=2))
    return 0 if out["rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
