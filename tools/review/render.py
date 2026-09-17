"""渲染层：逐票行 → 汇总 CSV（冻结 schema·幂等 upsert）+ 每日复盘 MD + 跨线汇总统计。

主榜 = r_exit（统筹拍板·含 D+2 了结）；胜率 = r_d1（close_positive）。未成交/pending 单列，不混胜率。
CSV 落 data/analysis/backtest/（gitignored 派生）；MD 落 docs/预测复盘/。
"""
from __future__ import annotations

import logging
from pathlib import Path

from tools.review.types import Attribution, ModelALabels, Pick

logger = logging.getLogger("review.render")

# —— 冻结列（下游/web 逐字引用；勿改列名/顺序，新增走末尾）——
GRAND_COLUMNS = [
    "date", "code", "name", "version_tag", "来源", "角色", "board",
    "filled", "untriggered", "status",
    "limit", "entry_price", "r_d1", "r_d2", "r_exit", "exit_horizon",
    "close_positive", "hold_to_d2", "stop_flag", "alpha_d1", "alpha_exit",
    "correct_bucket", "wrong_bucket",
    "native_sector_r_d1", "native_nextday",
]


def to_row(pick: Pick, lab: ModelALabels, attr: Attribution, native: dict) -> dict:
    """逐票 → 冻结 schema 一行。"""
    ns = native.get("native_sector") or {}
    return {
        "date": pick.date, "code": pick.code, "name": pick.name,
        "version_tag": pick.version_tag, "来源": pick.来源, "角色": pick.角色, "board": pick.board,
        "filled": lab.filled, "untriggered": lab.untriggered, "status": lab.status,
        "limit": lab.limit, "entry_price": lab.entry_price,
        "r_d1": lab.r_d1, "r_d2": lab.r_d2, "r_exit": lab.r_exit, "exit_horizon": lab.exit_horizon,
        "close_positive": lab.close_positive, "hold_to_d2": lab.hold_to_d2,
        "stop_flag": lab.stop_flag, "alpha_d1": lab.alpha_d1, "alpha_exit": lab.alpha_exit,
        "correct_bucket": attr.correct_bucket, "wrong_bucket": attr.wrong_bucket,
        "native_sector_r_d1": ns.get("r_d1"), "native_nextday": native.get("native_nextday"),
    }


def default_csv_path() -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "grand_review_scorecard.csv"


def write_grand_csv(rows: list[dict], csv_path=None) -> str:
    """幂等 upsert：剔除 rows 涉及的所有 date 的旧行 → append 新行 → 按 date,code 排序写回。
    同 date 重跑=覆盖当日全部票（绝不重复累加）。"""
    import pandas as pd
    path = Path(csv_path) if csv_path is not None else default_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([{c: r.get(c) for c in GRAND_COLUMNS} for r in rows], columns=GRAND_COLUMNS)
    dates = {str(r["date"]) for r in rows}

    if path.is_file() and path.stat().st_size > 0:
        old = pd.read_csv(path, dtype={"date": str, "code": str})
        for c in GRAND_COLUMNS:
            if c not in old.columns:
                old[c] = None
        old = old[GRAND_COLUMNS]
        old = old[~old["date"].astype(str).isin(dates)]     # 剔涉及 date → 幂等覆盖
        out = pd.concat([old, new], ignore_index=True)
    else:
        out = new
    out = out.sort_values(["date", "code"], kind="stable").reset_index(drop=True)
    out.to_csv(path, index=False)
    logger.info("汇总大复盘已写 %s（date=%s·%d 行）", path, ",".join(sorted(dates)), len(rows))
    return str(path)


# ── 跨线汇总统计（Model-A 口径）───────────────────────────────────────────
def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows: list[dict]) -> dict:
    """跨版/跨线聚合（收益榜用 r_exit·胜率用 close_positive·未成交/pending 单列）。"""
    total = len(rows)
    filled = [r for r in rows if r["filled"] is True]
    triggered = [r for r in rows if r["close_positive"] is not None]   # 进胜率分母
    wins = [r for r in triggered if r["close_positive"] is True]
    untrig = [r for r in rows if r["untriggered"] is True]
    settled_exit = [r for r in rows if r["r_exit"] is not None]

    from collections import Counter
    return {
        "n_total": total,
        "n_filled": len(filled),
        "n_untriggered": len(untrig),
        "未成交率": round(len(untrig) / total, 4) if total else None,
        "n_pending": sum(1 for r in rows if r["status"] == "pending"),
        "胜率_r_d1": round(len(wins) / len(triggered), 4) if triggered else None,
        "n_胜率分母": len(triggered),
        "平均r_exit_收益榜": _mean(r["r_exit"] for r in settled_exit),
        "平均alpha_exit": _mean(r["alpha_exit"] for r in settled_exit),
        "选对桶分布": dict(Counter(r["correct_bucket"] for r in rows if r["correct_bucket"])),
        "选错桶分布": dict(Counter(r["wrong_bucket"] for r in rows if r["wrong_bucket"])),
        "按版本_平均r_exit": {v: _mean(r["r_exit"] for r in settled_exit if r["version_tag"] == v)
                          for v in sorted({r["version_tag"] for r in rows})},
        "按来源_平均r_exit": {s: _mean(r["r_exit"] for r in settled_exit if r["来源"] == s)
                          for s in sorted({r["来源"] for r in rows if r["来源"]})},
    }


def _fmt(v, pct=False):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, (int, float)) and pct:
        return f"{v:+.2f}%"
    return str(v)


def daily_md(date: str, rows: list[dict]) -> str:
    """每日复盘 MD（逐票表 + 当日胜率牌）。"""
    s = summarize(rows)
    lines = [
        f"# 统一大复盘 · {date}",
        "",
        "> ⚠️ 测试环境研究模拟，非投资建议。口径：Model-A（限价=D收盘·成交价=min(限价,次开)·"
        "未触发不进分母）；**主榜=r_exit（含D+2了结）·胜率=r_d1**；防未来严格 as-of。",
        "",
        "## 当日胜率牌（Model-A）",
        f"- 总票 {s['n_total']}｜成交 {s['n_filled']}｜未成交 {s['n_untriggered']}"
        f"（未成交率 {_fmt(s['未成交率'])}）｜pending {s['n_pending']}",
        f"- **胜率(r_d1) {_fmt(s['胜率_r_d1'])}**（分母 {s['n_胜率分母']}）｜"
        f"**平均 r_exit(收益榜) {_fmt(s['平均r_exit_收益榜'], pct=True)}**｜平均 α_exit {_fmt(s['平均alpha_exit'], pct=True)}",
        f"- 选对桶 {s['选对桶分布'] or '—'}｜选错桶 {s['选错桶分布'] or '—'}",
        f"- 按版本平均 r_exit：{ {k: _fmt(v, pct=True) for k, v in s['按版本_平均r_exit'].items()} }",
        "",
        "## 逐票",
        "| 版本 | 来源 | 角色 | 代码 | 名称 | 成交价 | r_d1 | r_exit | 收盘为正 | 选对 | 选错 | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["version_tag"], str(x["code"]))):
        lines.append(
            f"| {r['version_tag']} | {_fmt(r['来源'])} | {_fmt(r['角色'])} | {r['code']} | {r['name']} "
            f"| {_fmt(r['entry_price'])} | {_fmt(r['r_d1'], pct=True)} | {_fmt(r['r_exit'], pct=True)} "
            f"| {_fmt(r['close_positive'])} | {_fmt(r['correct_bucket'])} | {_fmt(r['wrong_bucket'])} "
            f"| {r['status']} |")
    return "\n".join(lines) + "\n"


def default_md_dir() -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT / "docs" / "预测复盘"


def write_daily_md(date: str, rows: list[dict], md_dir=None) -> str:
    d = Path(md_dir) if md_dir is not None else default_md_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"大复盘_{date}.md"
    path.write_text(daily_md(date, rows), encoding="utf-8")
    logger.info("每日复盘 MD 已写 %s", path)
    return str(path)
