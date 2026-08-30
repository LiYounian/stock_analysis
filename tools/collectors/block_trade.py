"""大宗交易(每日成交明细)采集 —— WI-6 Phase 1-b「可回测事件源」。

数据源:东财 `ak.stock_dzjy_mrmx(symbol='A股', start_date, end_date)`(本机实测可用)。
按**日期区间**一次拉全市场大宗交易明细,含折溢率 / 成交额 / 成交额占流通市值 /
买卖双方营业部(机构专用可识别机构席位)。

===== 防未来函数(红线) =====
大宗交易为**盘后披露**:交易日 T 的成交明细在 T 收盘后公开。事件对决策**最早可用
时点 = T+1 开盘**。落盘 `trade_date`(=交易日 T)作披露锚点 + `visible_after_close=True`;
消费方必须在 **T+1 开盘及之后**使用,严禁用交易日盘中/收盘信息(见 block_asof 语义)。

===== 落盘契约(照 baidu_news.py 模式) =====
- raw kind = "block_trade",按 code + 采集日分区,payload = 该票大宗成交事件列表
  (按 trade_date 倒序)。
- **幂等 + 前向增量并集**:同票新旧快照按 (trade_date, buyer, seller, volume, deal_price)
  去重合并(一票一日可多笔),重跑不产重复。
- **新鲜度门控**:缓存 ≤ BLOCK_STALE_DAYS 天视为新鲜 → 当日跳过重拉。
- **优雅降级**:限流/非 200/空/结构漂移单元跳过,不中断整批。

方向代理(不预设 alpha,交回测检验):
  · premium_rate(折溢率):折价(<0)常见于持股方套现/减持压力,溢价(>0)偶见承接意愿。
  · inst_buy:买方营业部含"机构专用" → 机构接盘(可能吸筹)。
是否有选择性由回测判定,不预设方向即利好/利空。
非投资建议;历史披露数据仅供研究。
"""
from __future__ import annotations

import logging
import os
from datetime import date, timedelta

import pandas as pd

from tools.store import repo as store

logger = logging.getLogger("collectors.block_trade")

_SOURCE = "eastmoney"
_KIND = "block_trade"
_INST_SEAT = "机构专用"

# 门控/窗口默认值(env 可覆盖;不改 settings.py,保持文件归属边界)
BLOCK_STALE_DAYS = float(os.getenv("BLOCK_STALE_DAYS", "1"))
BLOCK_LOOKBACK_DAYS = int(os.getenv("BLOCK_LOOKBACK_DAYS", "30"))

# 东财原始列 → 归一字段名
_COL_MAP = {
    "证券代码": "code",
    "证券简称": "name",
    "交易日期": "trade_date",
    "涨跌幅": "pct_chg",
    "收盘价": "close",
    "成交价": "deal_price",
    "折溢率": "premium_rate",
    "成交量": "volume",
    "成交额": "amount",
    "成交额/流通市值": "amount_to_float",
    "买方营业部": "buyer",
    "卖方营业部": "seller",
}


def _to_float(v):
    """宽松转 float;失败/空 → None。"""
    try:
        if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def block_inst_buy(buyer) -> int:
    """买方是否机构席位:买方营业部含"机构专用" → 1,否则 0。"""
    return 1 if _INST_SEAT in str(buyer or "") else 0


def block_direction(premium_rate) -> int:
    """折溢率方向标签:溢价(>0)→ +1,折价(<0)→ −1,平价/缺失 → 0。

    **仅方向代理,不预设 alpha**;折溢率与后续收益的关系由回测检验。
    """
    f = _to_float(premium_rate)
    if f is None:
        return 0
    if f > 0:
        return 1
    if f < 0:
        return -1
    return 0


def _norm_date(d: str) -> str:
    """'YYYYMMDD' / 'YYYY-MM-DD' → 'YYYY-MM-DD'。"""
    s = str(d).strip().replace("-", "").replace("/", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(d)[:10]


def _norm_row(row: dict) -> dict | None:
    """东财一行 → 归一事件 dict。无 code/trade_date → None。"""
    code = str(row.get("证券代码", "")).strip()
    if not code or not code.isdigit():
        return None
    td = _norm_date(row.get("交易日期", ""))
    if len(td) != 10:
        return None
    out = {"visible_after_close": True}   # 盘后披露 → T+1 才可用
    for zh, en in _COL_MAP.items():
        if en in ("code", "name", "trade_date", "buyer", "seller"):
            out[en] = str(row.get(zh, "")).strip()
        else:
            out[en] = _to_float(row.get(zh))
    out["code"] = code
    out["trade_date"] = td
    out["inst_buy"] = block_inst_buy(out.get("buyer"))
    out["direction"] = block_direction(out.get("premium_rate"))
    return out


def _event_key(ev: dict):
    """幂等键:同票同日同买卖席位同量同价视为一笔(一票一日可多笔大宗)。"""
    return (ev.get("trade_date", ""), ev.get("buyer", ""), ev.get("seller", ""),
            ev.get("volume"), ev.get("deal_price"))


def _merge_incremental(new_events: list[dict], prev_events: list[dict]) -> list[dict]:
    """前向增量并集:新旧按 _event_key 去重合并,按 trade_date 倒序。幂等。"""
    seen: set = set()
    merged: list[dict] = []
    for ev in list(new_events) + list(prev_events):
        k = _event_key(ev)
        if k in seen:
            continue
        seen.add(k)
        merged.append(ev)
    merged.sort(key=lambda x: x.get("trade_date", ""), reverse=True)
    return merged


def _prev_snapshot(code: str) -> list[dict]:
    """读该票最近一次 block_trade 快照(任意分区);无则 []。"""
    try:
        prev = store.get_raw(_KIND, code)
        return prev if isinstance(prev, list) else []
    except FileNotFoundError:
        return []


def _default_window() -> tuple[str, str]:
    """未显式给区间时的默认窗口:[今天 − BLOCK_LOOKBACK_DAYS, 今天](YYYYMMDD)。"""
    end = date.today()
    start = end - timedelta(days=BLOCK_LOOKBACK_DAYS)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def fetch_range_df(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """拉一个日期区间的全市场大宗交易明细,归一为 DataFrame(不落盘,便于回测/mock)。

    列含归一字段 + inst_buy + direction。空/失败 → 空 DataFrame(不抛)。
    """
    import akshare as ak

    s0, e0 = _default_window()
    s = (start or s0).replace("-", "")
    e = (end or e0).replace("-", "")
    try:
        raw = ak.stock_dzjy_mrmx(symbol="A股", start_date=s, end_date=e)
    except Exception as exc:   # noqa: BLE001
        logger.warning("block_trade 区间拉取失败 [%s,%s](降级空): %s", s, e, str(exc)[:120])
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    rows = [r for r in (_norm_row(rec) for rec in raw.to_dict("records")) if r]
    return pd.DataFrame(rows)


def stale_codes(codes: list[str], max_days: float | None = None) -> list[str]:
    """返回需要重拉的票(缓存陈旧/无缓存)。供编排层 skip-if-cached 过滤用。"""
    md = BLOCK_STALE_DAYS if max_days is None else max_days
    return [c for c in codes if store.is_stale(_KIND, c, md)]


def fetch_block_trade(start: str | None = None, end: str | None = None,
                      skip_fresh: bool = True, max_days: float | None = None,
                      codes: list[str] | None = None) -> dict[str, list[dict]]:
    """批量采集大宗交易并**按票落盘**(前向增量、幂等、带新鲜度门控)。

    数据源天然按日返回全市场 → 按区间一次拉取,分发到各票分区累积。
    参数同 lhb.fetch_lhb。落盘 store.put_raw("block_trade", code, [事件...])。
    返回 {code: [事件...]}。单票写入失败 → log 跳过,不中断整批。
    """
    settings_ensure_dirs()
    md = BLOCK_STALE_DAYS if max_days is None else max_days
    df = fetch_range_df(start, end)
    out: dict[str, list[dict]] = {}
    if df.empty:
        logger.info("block_trade 区间无数据 [%s,%s]", start, end)
        return out

    wl = set(codes) if codes else None
    failed: list[str] = []
    for code, g in df.groupby("code"):
        code = str(code)
        if wl is not None and code not in wl:
            continue
        if skip_fresh and not store.is_stale(_KIND, code, md):
            try:
                out[code] = store.get_raw(_KIND, code)
            except FileNotFoundError:
                out[code] = []
            continue
        try:
            fresh = g.to_dict("records")
            merged = _merge_incremental(fresh, _prev_snapshot(code))
            store.put_raw(_KIND, code, merged,
                          meta={"source": _SOURCE, "new_pulled": len(fresh),
                                "total": len(merged)})
            out[code] = merged
        except Exception as exc:   # noqa: BLE001
            failed.append(code)
            logger.warning("block_trade 落盘 %s 失败(跳过): %s", code, str(exc)[:120])
    logger.info("block_trade 采集完成:%d 票落盘%s", len(out),
                f",{len(failed)} 票失败" if failed else "")
    return out


def load_block_trade(code: str, date: str | None = None) -> list[dict]:
    """读单票大宗交易快照。缺失抛 FileNotFoundError。date=None → 最新分区。"""
    return store.get_raw(_KIND, code, date=date or "latest")


def block_asof(code: str, as_of: str, date: str | None = None) -> list[dict]:
    """as-of 无未来函数切片:只返回**交易日 < as_of** 的事件(按 trade_date 倒序)。

    大宗盘后披露,交易日 T 当天不可用(见模块 docstring)→ 用**严格小于**。
    调用方若在 T+1 决策,把 as_of 设为决策日即自然满足 trade_date < as_of。
    """
    cutoff = str(as_of)[:10]
    items = load_block_trade(code, date=date)
    return [ev for ev in items if str(ev.get("trade_date", ""))[:10] < cutoff]


def settings_ensure_dirs() -> None:
    """确保数据目录存在;隔离 settings 依赖便于 mock。"""
    from tools.config import settings
    settings.ensure_dirs()
