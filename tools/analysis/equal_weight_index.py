"""全A等权净值指数(α 基准取数)。

## 为什么有这个模块(2026-09-07)

持仓账本(`tools.pipeline.position_ledger`)算 α 超额要注入"该时点全A等权净值点位"。
项目每日收盘广度节点已把**全A等权当日涨幅** `mean_pct`(%)落在 `data/breadth/<date>.json`
(经验#11/#17 的 α 基准口径 = 全A等权,与 `cross_section_stats` 同源)。本模块把这条日度
`mean_pct` 序列**链成累计等权净值指数**(每日再平衡),供账本按日取基准点位。

## 口径与诚实边界

  · 净值[D] = 净值[D−1] × (1 + mean_pct[D]/100),锚定首个可得日 = `base`(默认 1000)。
  · **日度粒度**:`level_at(D)` = D 日收盘后的等权净值。个股 entry/exit 是尾盘价(≈收盘),
    与日度净值近似对齐;**日内精确对齐需全A盘中横截面(午休快照只覆盖候选∪自选,非全A)**,
    故 v1 用日度近似,已知误差对超短线(持有 1~数日)很小,记此边界不假装精确。
  · 某日 breadth 缺失/`mean_pct` 为 null → 该日不进链(跳过,不假造 0),`level_at` 对缺失日返回 None。

⚠️ 测试环境研究用,非投资建议。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from tools.config import settings

logger = logging.getLogger("analysis.equal_weight_index")

BREADTH_DIR = settings.PROJECT_ROOT / "data" / "breadth"
BASE_LEVEL = 1000.0


def load_daily_mean_pct(breadth_dir: str | Path = BREADTH_DIR) -> dict[str, float]:
    """读 data/breadth/<date>.json → {date: mean_pct(%)};缺文件/字段/null 的日跳过。"""
    d = Path(breadth_dir)
    out: dict[str, float] = {}
    if not d.exists():
        logger.warning("breadth 目录不存在 %s,等权净值为空", d)
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("breadth 解析失败 %s:%s", p, e)
            continue
        date = data.get("date") or p.stem
        mp = data.get("mean_pct")
        if mp is None:
            continue                       # 缺就跳过,不假造 0
        out[date] = float(mp)
    return out


def net_value_series(breadth_dir: str | Path = BREADTH_DIR,
                     base: float = BASE_LEVEL) -> dict[str, float]:
    """日度 mean_pct 链成累计等权净值 {date: level}(按日期升序复利,锚定首日=base)。"""
    daily = load_daily_mean_pct(breadth_dir)
    level = base
    series: dict[str, float] = {}
    for date in sorted(daily):
        level = level * (1.0 + daily[date] / 100.0)
        series[date] = level
    return series


def level_at(date: str, breadth_dir: str | Path = BREADTH_DIR,
             base: float = BASE_LEVEL) -> float | None:
    """某日全A等权净值点位;该日无 breadth → None(账本据此把 alpha 记 null,不假造)。"""
    return net_value_series(breadth_dir, base).get(date)
