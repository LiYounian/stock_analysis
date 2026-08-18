"""申万二级 半导体(801081)成分股→票池落盘(策略E_自选池半导体多因子 用)。

申万二级半导体行业 178 只成分股(2026-08 快照,后期可刷新),akshare CSIndex
接口 `index_stock_cons(symbol="801081")` 返回;与主档 code_name.json 交集 100%
命中(纯 A 股,无退市),按代码升序落 config/semi_universe.json。

**离线用**:一次性拉取,不进日常闭环。刷新:`python -m tools.collectors.semi_universe`。
"""
from __future__ import annotations

import json
import logging

from tools.config import settings

logger = logging.getLogger("collectors.semi_universe")

_OUT_PATH = settings.PROJECT_ROOT / "config" / "semi_universe.json"
_CODE_NAME_PATH = settings.PROJECT_ROOT / "config" / "code_name.json"
_INDEX_SYMBOL = "801081"


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
    logger.info("半导体票池落盘 %s(%d 只)", _OUT_PATH, len(inter))
    return inter


def load() -> list[str]:
    """读票池;缺失抛 FileNotFoundError(不静默返空)。"""
    return json.loads(_OUT_PATH.read_text("utf-8"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    refresh()
