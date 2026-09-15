"""次日实盘口径·累积胜率牌（2026-09-15 整改设计 §4，阶段2A 配套代码）。

复盘任务（daily-stock-eod-review，D+1 15:05 复 D 选出的票）每日把当日逐票记分聚合成
**一行胜率牌** append 到 `data/analysis/backtest/nextday_scorecard.csv`：绝对胜率、平均绝对
收益、平均 α、β 拖累致亏占比、卖出线达成率、未触发数。这是这套策略对用户的**核心 KPI**，
也是复盘存在的意义——如实统计"按次日入场点买、到 D+1 收盘赚没赚"。

口径要点（整改设计 §4）：
  · **绝对收益 = D+1收盘 / 入场价 − 1**（主指标）；**是否收盘为正 = 绝对收益 > 0**（胜率计数）。
  · **"未触发"不计入胜率分母**（诚实，不拿没买的票充数）；单列统计未触发数。
  · α/β 只作归因、不升格为否决：β 拖累致亏占比回答"这批亏损单里多少主要怪大盘"。

诚实/防未来（硬红线）：
  · 本模块是**纯事后聚合**，不取数、不触网；只用复盘已定稿(≤D+1)的逐票记分。
  · 胜率牌 **append-only + 幂等**：同 date 重跑=覆盖当日行（绝不重复累加），绝不回改历史、绝不挑日。
  · 绝对收益只用 D+1 的入场价与当日收盘两价算，不掺任何 >D+1 信息。

CSV 落 `data/analysis/backtest/`（派生数据，随 forward_scorecard.csv 惯例不入库，见 .gitignore）。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("analysis.nextday_scorecard")

# —— 冻结 CSV schema（换线的 eod-review SKILL 逐字引用；勿改列名/顺序）——
COLUMNS = [
    "date",                  # 复盘日 D+1（被记分的交易日）
    "n_buy",                 # 计入胜率分母的买入数（= 已触发买入，不含"未触发"）
    "abs_win_rate",          # 绝对胜率 = 收盘为正数 / n_buy
    "avg_abs_return",        # 平均绝对收益%（入场→收盘）
    "avg_alpha",             # 平均 α（vs 全A等权）
    "beta_drag_loss_ratio",  # β 拖累致亏占比 = β拖累致亏单 / 亏损单
    "sell_line_hit_rate",    # D+2 卖出线达成率
    "n_untriggered",         # 未触发数（不计入分母）
]


def _num(v) -> bool:
    """v 是可算的数字（排除 bool / None / 字符串）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def abs_return_pct(entry_price, close_price) -> float | None:
    """绝对收益% = (D+1收盘 / 入场价 − 1)×100。入场价/收盘缺或入场价≤0 → None（有声缺失）。

    **防未来**：只用 D+1 的入场价与当日收盘两价，不掺任何 >D+1 信息。"""
    if not (_num(entry_price) and _num(close_price)):
        return None
    if entry_price <= 0:
        return None
    return round((close_price / entry_price - 1.0) * 100.0, 4)


def is_close_positive(ret_pct) -> bool | None:
    """是否收盘为正：绝对收益% **严格 > 0** → True；≤0 → False；None（未触发/无价）→ None。"""
    if ret_pct is None:
        return None
    return ret_pct > 0


def _mean(vals) -> float | None:
    xs = [v for v in vals if _num(v)]
    return round(sum(xs) / len(xs), 4) if xs else None


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def compute_daily_row(date: str, picks: list[dict]) -> dict:
    """把某复盘日 D+1 的逐票记分聚合成胜率牌一行（纯函数、可单测）。

    picks 每项字段（全部可选，缺=None/False；向后兼容）：
      · abs_return_pct : 入场→收盘 绝对收益%（float|None）
      · close_positive : 是否收盘为正（True/False/None）；**None = 未触发（不计入分母）**
      · alpha          : α vs 全A等权（float|None）
      · beta_drag      : 该（亏损）单是否主要由 β 拖累（bool，默认 False）
      · sell_line_hit  : D+2 卖出线是否达成（True/False/None）；None = 未跟踪/无数据

    "未触发"判定 = (close_positive is None)——只有已明确"买到"（是/否）的票才进胜率分母。
    """
    triggered = [p for p in picks if p.get("close_positive") is not None]
    n_buy = len(triggered)
    n_untriggered = len(picks) - n_buy

    wins = sum(1 for p in triggered if p.get("close_positive") is True)
    losses = [p for p in triggered if p.get("close_positive") is False]
    beta_drag_losses = sum(1 for p in losses if bool(p.get("beta_drag")))

    sell_tracked = [p for p in triggered if p.get("sell_line_hit") is not None]
    sell_hits = sum(1 for p in sell_tracked if p.get("sell_line_hit") is True)

    return {
        "date": date,
        "n_buy": n_buy,
        "abs_win_rate": _rate(wins, n_buy),
        "avg_abs_return": _mean(p.get("abs_return_pct") for p in triggered),
        "avg_alpha": _mean(p.get("alpha") for p in triggered),
        "beta_drag_loss_ratio": _rate(beta_drag_losses, len(losses)),
        "sell_line_hit_rate": _rate(sell_hits, len(sell_tracked)),
        "n_untriggered": n_untriggered,
    }


def default_csv_path() -> Path:
    from tools.config import settings
    return settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "nextday_scorecard.csv"


def append_daily_row(row: dict, csv_path=None) -> str:
    """幂等 upsert 一行胜率牌到 csv。**append-only 语义**：只增日；**同 date 重跑=覆盖当日行**
    （绝不重复累加），按 date 升序写回。缺文件则新建。返回路径字符串。

    幂等实现：读旧档 → 剔除同 date 行 → concat 新行 → 排序写回。旧档缺新列时对齐补空（向后兼容）。
    """
    path = Path(csv_path) if csv_path is not None else default_csv_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([{c: row.get(c) for c in COLUMNS}], columns=COLUMNS)

    if path.is_file() and path.stat().st_size > 0:
        old = pd.read_csv(path, dtype={"date": str})
        for c in COLUMNS:                       # 对齐旧档（容缺列）
            if c not in old.columns:
                old[c] = None
        old = old[COLUMNS]
        old = old[old["date"].astype(str) != str(row["date"])]   # 剔同日 → 幂等覆盖
        out = pd.concat([old, new], ignore_index=True)
    else:
        out = new

    out = out.sort_values("date", kind="stable").reset_index(drop=True)
    out.to_csv(path, index=False)
    logger.info("胜率牌已写 %s（date=%s n_buy=%s 未触发=%s）",
                path, row.get("date"), row.get("n_buy"), row.get("n_untriggered"))
    return str(path)


def update_scorecard(date: str, picks: list[dict], csv_path=None) -> tuple[dict, str]:
    """便捷：算当日行 + 幂等 append。返回 (row, path)。"""
    row = compute_daily_row(date, picks)
    path = append_daily_row(row, csv_path=csv_path)
    return row, path


# ————————————————————————————————————————————————————————————————
# CLI（供 eod-review SKILL 调用：读逐票记分 JSON → 算行 → 幂等 append）
# ————————————————————————————————————————————————————————————————
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="次日实盘口径胜率牌：读复盘逐票记分 JSON，聚合一行并幂等 append 到 csv")
    ap.add_argument("--date", required=True, help="复盘日 D+1（被记分交易日），YYYY-MM-DD")
    ap.add_argument("--picks", required=True,
                    help="逐票记分 JSON 文件（list[dict]，字段见 compute_daily_row）")
    ap.add_argument("--out", help="csv 路径，缺省 data/analysis/backtest/nextday_scorecard.csv")
    ap.add_argument("--dry-run", action="store_true", help="只算行、不落盘（打印行）")
    args = ap.parse_args(argv)

    picks = json.loads(Path(args.picks).read_text(encoding="utf-8"))
    if isinstance(picks, dict):
        picks = [picks]
    row = compute_daily_row(args.date, picks)
    if args.dry_run:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0
    path = append_daily_row(row, csv_path=args.out)
    print(f"已写 {path}（date={row['date']} n_buy={row['n_buy']} "
          f"绝对胜率={row['abs_win_rate']} 未触发={row['n_untriggered']}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
