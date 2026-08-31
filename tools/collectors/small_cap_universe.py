# ⚠️ 已下线(存档保留):2026-09-01 起,小市值组合策略(web 策略5 / 策略D)从生产/展示摘除
#    (从未前瞻回测/未证明有效/无明确用途)。本采集器不删、可恢复,见 docs/日志/开发日志.md。
"""中小板指(399101)历史成分股→票池落盘(策略C_小市值组合 用)。

深交所中小板指 2021-04 已并入主板停发,akshare CSIndex 接口仍能拉到 958 只历史成分
(纯 002/003 深主板中小);与主档 code_name.json 交集去退市 → config/small_cap_universe.json。
**离线用**:一次性拉取,不进日常闭环。刷新:`python -m tools.collectors.small_cap_universe`。
"""
from __future__ import annotations

import json
import logging

from tools.config import settings

logger = logging.getLogger("collectors.small_cap_universe")

_OUT_PATH = settings.PROJECT_ROOT / "config" / "small_cap_universe.json"
_CODE_NAME_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"
_INDEX_SYMBOL = "399101"


def refresh() -> list[str]:
    """拉成分股 → 与主档 code_name.json 交集去退市 → 排序落盘;返回最终代码列表。"""
    import akshare as ak
    df = ak.index_stock_cons(symbol=_INDEX_SYMBOL)
    if df is None or len(df) == 0 or "品种代码" not in df.columns:
        raise ConnectionError(f"CSIndex {_INDEX_SYMBOL} 成分股为空")
    cons = [str(x) for x in df["品种代码"].tolist()]

    try:
        code_name = json.loads(_CODE_NAME_PATH.read_text("utf-8"))
        inter = sorted(set(cons) & set(code_name.keys()))
    except FileNotFoundError:
        logger.warning("code_name.json 缺失,不做退市过滤(全量成分股落盘)")
        inter = sorted(cons)

    _OUT_PATH.write_text(json.dumps(inter, ensure_ascii=False, indent=2), "utf-8")
    logger.info("小市值票池落盘 %s(%d 只)", _OUT_PATH, len(inter))
    return inter


def load() -> list[str]:
    """读票池;缺失抛 FileNotFoundError(不静默返空)。"""
    return json.loads(_OUT_PATH.read_text("utf-8"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    refresh()
