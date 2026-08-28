"""行业变迁史采集(消除回测前视偏差)。

借鉴 a-stock-data §6.7。项目现有 board.membership 只存**当前所属行业**——用它回测会引入
**前视偏差**:一只股票今天在「机器人」板块,不代表三年前回测时段它就属于该板块(可能刚借壳/转型)。
本采集拉每只股票的**历史行业变更记录**,并提供 `industry_at(code, date)` 还原任一历史时点的所属行业,
供回测/归因按「当时」而非「现在」的板块取数。

数据源:巨潮 `stock_industry_change_cninfo`(akshare 封装,给出每次变更的:变更日期/分类标准/前后行业)。
  - 与 board.py 的申万/证监会「当前分类」互补:board 给现状(供选股),本模块给历史(供回测)。
  - 港股无此数据 → 落空降级。
列名按 akshare 现行输出**防御式取数**,容忍列名漂移。
落盘:走 store 层(kind="industry_history",json:按变更日期升序的记录列表),旁记 meta.source="cninfo"。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.industry_history")

_SOURCE = "cninfo"  # 巨潮
# 只关注一种分类标准以免多标准混淆时点(证监会行业为巨潮主口径;可按需放开)
_PREFERRED_STD = "证监会行业分类标准"


def _pick(row: pd.Series, *names):
    for nm in names:
        if nm in row.index and pd.notna(row[nm]):
            return row[nm]
    return None


def _parse(df: pd.DataFrame) -> list[dict]:
    """把巨潮行业变更 df 解析成按变更日期升序的记录列表。"""
    items = []
    for _, r in df.iterrows():
        d = str(_pick(r, "变更日期", "生效日期", "公告日期") or "")[:10]
        ind = _pick(r, "行业名称", "行业门类", "所属行业", "变更后行业")
        std = _pick(r, "分类标准", "行业分类标准")
        if not d or ind is None:
            continue
        items.append({"date": d, "industry": str(ind).strip(),
                      "std": str(std).strip() if std is not None else None})
    items.sort(key=lambda x: x["date"])
    return items


def _fetch_cninfo(code: str) -> list[dict]:
    """巨潮某票行业变更史。空/失败抛错(交上层降级)。"""
    import akshare as ak
    df = ak.stock_industry_change_cninfo(symbol=code, start_date="19900101",
                                         end_date=pd.Timestamp.today().strftime("%Y%m%d"))
    if df is None or len(df) == 0:
        raise ValueError("行业变更史为空")
    return _parse(df)


def fetch_industry_history(codes: list[str]) -> dict[str, list[dict]]:
    """批量采集行业变迁史并落盘。单票失败记 log 跳过,不中断整批;港股整体落空。

    该数据变动很低频(多年一次),编排上走 skip-if-cached(见 run.collect_values):
    只补尚无缓存的票,不每日重复拉巨潮。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    out: dict[str, list[dict]] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 行业变迁 %s 采集...", i, n, code)
        try:
            if stock_pool.is_hk(code):
                items: list[dict] = []
                store.put_raw("industry_history", code, items, meta={"source": "none(hk)"})
            else:
                items = _fetch_cninfo(code)
                store.put_raw("industry_history", code, items, meta={"source": _SOURCE})
            out[code] = items
            logger.info("行业变迁 %s:%d 次变更", code, len(items))
        except Exception as e:
            failed.append(code)
            logger.error("行业变迁 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("行业变迁拉取失败(%d): %s", len(failed), failed)
    return out


def industry_at(code: str, date: str, std: str | None = _PREFERRED_STD) -> str | None:
    """还原某票在 date(YYYY-MM-DD)当时的所属行业:取变更日期 ≤ date 的**最后一条**。

    std 指定分类标准(默认证监会行业);传 None 则不过滤标准(用全部记录)。
    无历史记录/该时点前无生效记录 → None(advisory,不抛;交上层回退 board 现状或降级)。
    """
    try:
        items = load_industry_history(code)
    except FileNotFoundError:
        return None
    if std:
        items = [x for x in items if x.get("std") == std] or \
                [x for x in items if x.get("std")]        # 该标准无记录时退回有标准的全部
    hit = [x for x in items if x["date"] <= date]
    return hit[-1]["industry"] if hit else None


def load_industry_history(code: str) -> list[dict]:
    """读单票行业变迁史(按变更日期升序)。缺失抛 FileNotFoundError。"""
    return store.get_raw("industry_history", code)
