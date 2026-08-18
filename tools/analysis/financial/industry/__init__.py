"""行业财报专家注册表(申万一级 KEY → 专家模块;缺 → None,退回通用兜底)。

**自动发现**:扫本包内所有导出 `KEY` 的模块动态注册——新增一个行业只需往本目录丢
`<行业>.py`(导出 KEY/NOTE/dimension_specs/weights/SKIP_FLAGS/extra_flags),
**无需改本文件**,故各行业模块互相独立、可并行开发(不共享写)。

专家模块统一契约(见 docs/财报分析专家/架构设计.md §2):
  KEY: str                         # 申万一级行业名
  NOTE: str                        # 页面标注的口径说明
  dimension_specs() -> dict        # 覆写五维子指标区间(格式同 scoring.dimension_specs)
  weights() -> dict | None         # 覆写五维权重(None=用通用)
  SKIP_FLAGS: list[str]            # 本行业不适用的通用红旗名
  extra_flags(derived, structured) -> list[dict]   # 本行业专属红旗(成形 {code,命中,严重度,值})
"""
from __future__ import annotations

import importlib
import logging
import pkgutil

logger = logging.getLogger("analysis.financial.industry")

_REQUIRED = ("KEY", "dimension_specs", "SKIP_FLAGS", "extra_flags")


def _discover() -> dict:
    """扫本包,导入导出 KEY 的模块,建 {KEY: module}。单模块导入失败仅记日志、不炸整体。"""
    experts: dict = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as e:                       # noqa: BLE001
            logger.warning("行业专家 %s 导入失败,跳过: %s", mod.name, e)
            continue
        key = getattr(m, "KEY", None)
        if not key:
            continue
        missing = [a for a in _REQUIRED if not hasattr(m, a)]
        if missing:
            logger.warning("行业专家 %s 缺契约字段 %s,跳过", mod.name, missing)
            continue
        experts[key] = m
    return experts


EXPERTS: dict = _discover()


def get_expert(key: str | None):
    """按申万一级 KEY 取专家模块;无 key / 未注册 → None(用通用兜底)。"""
    if not key:
        return None
    return EXPERTS.get(key)
