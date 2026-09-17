"""统一大复盘生成路径（tools/review）· 跨两条线选股复盘 + 归因。

契约：docs/计划/2026-09-17_统一大复盘生成路径_设计与实现计划.md（v1，统筹 stock-analysis-37
拍板·回声-0914）。KPI=**绝对收益**。定位：在现有分线 forward 记分器之上做**跨线 JOIN + 归因**，
**不重造收益口径**——Model-A 撮合复用 `tools/research/sector_news_forward`（其两条语义已被
`tests/test_sector_news_forward.py` 锁死）。

单向数据流：loaders(读产物→Pick) → model_a(撮合+收益→ModelALabels) → attribution(规则归因)
→ render(每日复盘 MD + 汇总 CSV/MD)。build_grand_review 编排。

双列口径（统筹拍板）：
  · 各线**原生口径**列（join 现有 csv，不重算）+ 统一 **Model-A 列**（跨版/跨线收益榜唯一可比）。
  · **主榜=r_exit**（含 D+2 了结，用户最关心"谁收益好"）；**胜率=r_d1**（D+1 收盘为正，决策闸）。

防未来硬红线（两列共守）：只用 t≤date 数据 + 事后 master kline（无未来 bar=天然护栏）；
未到期留 None；as-of 守卫拒未来块；α 源 market_ew.parquet 缺→α=null 有声缺失（绝不顶替/编）。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

from tools.review.types import Attribution, ModelALabels, Pick

__all__ = ["Pick", "ModelALabels", "Attribution"]
