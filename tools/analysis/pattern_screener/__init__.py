"""形态选股(pattern_screener)—— 独立策略包。

一个自包含策略:靠可计算的图形形态 + 相对强度 + 量能 + 负向护栏做硬规则选股,
并产出全市场达标占比作为市场宽度信号。与 tools/analysis/ 下其它策略平级、互不耦合;
将来新增策略(如 mean_reversion/、event_driven/)各建同级目录。

需求见 docs/计划/V1_形态选股与市场状态系统.md;参数 Config `THRESHOLDS["形态选股"]`/`["市场状态"]`。
取数在 tools/collectors、参数在 tools/config —— 本包只放策略逻辑。

子模块:
  pattern —— 形态几何引擎(箱体/杯柄/楔形/旗形)         F2.1
  rs      —— 相对强度(20 日收益率差)                    F2.2
  screen  —— 选股核心(护栏 + 硬规则 AND + 全市场达标占比)F2.3/F2.4/F2.6
  (后续 regime.py 市场状态、backtest.py 前瞻胜率进本包)
"""
from __future__ import annotations

from tools.analysis.pattern_screener import pattern, rs, screen
from tools.analysis.pattern_screener.pattern import detect
from tools.analysis.pattern_screener.rs import compute as rs_compute, is_strong
from tools.analysis.pattern_screener.screen import (
    guardrail, is_qualified, market_breadth, volume_ok,
)

__all__ = [
    "pattern", "rs", "screen",
    "detect", "rs_compute", "is_strong",
    "guardrail", "is_qualified", "market_breadth", "volume_ok",
]
