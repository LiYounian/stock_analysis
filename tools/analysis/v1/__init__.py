"""V1 形态选股与市场状态系统 —— 自包含策略包。

对应需求 docs/计划/V1_形态选股与市场状态系统.md、参数 Config `THRESHOLDS["V1形态选股"]`/`["V1市场状态"]`。
本包只放本策略逻辑,与其它 analysis 模块解耦;后续 regime.py(模块一市场状态)、
backtest.py(前瞻胜率回测)也进本包。取数在 tools/collectors,参数在 tools/config。

子模块:
  pattern —— 形态几何引擎(箱体/杯柄/楔形/旗形)         F2.1
  rs      —— 相对强度(20 日收益率差)                    F2.2
  screen  —— 选股核心(护栏 + 硬规则 AND + 全市场达标占比)F2.3/F2.4/F2.6
"""
from __future__ import annotations

from tools.analysis.v1 import pattern, rs, screen
from tools.analysis.v1.pattern import detect
from tools.analysis.v1.rs import compute as rs_compute, is_strong
from tools.analysis.v1.screen import guardrail, is_qualified, market_breadth, volume_ok

__all__ = [
    "pattern", "rs", "screen",
    "detect", "rs_compute", "is_strong",
    "guardrail", "is_qualified", "market_breadth", "volume_ok",
]
