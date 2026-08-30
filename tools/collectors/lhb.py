"""龙虎榜(每日席位榜)采集 —— WI-6 Phase 1-b「可回测事件源」。

数据源:东财 `ak.stock_lhb_detail_em(start_date, end_date)`(本机实测可用)。
按**日期区间**一次拉全市场当日上榜明细,权威披露口径(交易所盘后公布)。

===== 防未来函数(红线) =====
龙虎榜为**盘后披露**:上榜日 T 的席位榜在 T 收盘后(约当晚)才公开。因此
事件对决策**最早可用时点 = T+1 开盘**。本采集器落盘 `list_date`(=上榜日 T)作为
披露日锚点,并显式标注 `visible_after_close=True`。任何消费方(回测/选股)必须
在 **T+1 开盘及之后**才使用该事件,严禁用上榜日盘中/收盘信息(见 lhb_asof 语义)。

**严禁**落"上榜后 1/2/5/10 日收益"这些东财自带的**前视列** —— 它们是未来信息,
只可作离线对拍参照,绝不作为特征入库。本采集器解析时直接丢弃这些列。

===== 落盘契约(照 baidu_news.py 模式) =====
- raw kind = "lhb",按 code + 采集日分区(走 store 层),payload = 该票上榜事件列表
  (按 list_date 倒序)。
- **幂等 + 前向增量并集**:同票新旧快照按 (list_date, reason) 去重合并,重跑不产重复,
  保留被后续下架的历史上榜记录(无幸存者偏差的前向样本)。
- **新鲜度门控**:缓存 ≤ LHB_STALE_DAYS 天视为新鲜 → 当日跳过重拉(对齐 baidu_news)。
- **优雅降级**:限流/非 200/空/结构漂移一律单元跳过,不中断整批;港股无此源 → 落空。

非投资建议;历史披露数据仅供研究,方向标签由回测检验、不预设 alpha。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import date, timedelta

import pandas as pd

from tools.store import repo as store

logger = logging.getLogger("collectors.lhb")

_SOURCE = "eastmoney"
_KIND = "lhb"

# 门控/窗口默认值(env 可覆盖;不改 settings.py,保持文件归属边界)
LHB_STALE_DAYS = float(os.getenv("LHB_STALE_DAYS", "1"))          # 缓存≤1天视为新鲜
LHB_LOOKBACK_DAYS = int(os.getenv("LHB_LOOKBACK_DAYS", "30"))     # 未给区间时默认回看天数

# 东财原始列 → 归一字段名(只保留披露口径 + 席位口径,前视列一律丢弃)
_COL_MAP = {
    "代码": "code",
    "名称": "name",
    "上榜日": "list_date",
    "解读": "interpret",
    "收盘价": "close",
    "涨跌幅": "pct_chg",
    "龙虎榜净买额": "net_buy",
    "龙虎榜买入额": "buy_amt",
    "龙虎榜卖出额": "sell_amt",
    "龙虎榜成交额": "lhb_turnover",
    "市场总成交额": "mkt_turnover",
    "净买额占总成交比": "net_buy_ratio",
    "成交额占总成交比": "turnover_ratio",
    "换手率": "turnover_rate",
    "流通市值": "float_mv",
    "上榜原因": "reason",
}
# 显式列入「前视黑名单」—— 绝不入库(防未来函数)
_LEAK_COLS = ("上榜后1日", "上榜后2日", "上榜后5日", "上榜后10日")


def _to_float(v):
    """宽松转 float;失败/空 → None。"""
    try:
        if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def lhb_direction(net_buy) -> int:
    """龙虎榜方向标签:龙虎榜净买额 > 0 → +1(净买入),< 0 → −1(净卖出),否则 0。

    **仅为方向代理,不预设 alpha** —— 龙虎榜含游资/机构且买卖意图不一(可能派发),
    是否有选择性交由回测检验。net_buy 缺失 → 0(中性,保守)。
    """
    f = _to_float(net_buy)
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
    """东财一行 → 归一事件 dict。丢弃前视列;无 code/list_date → None。"""
    code = str(row.get("代码", "")).strip()
    if not code or not code.isdigit():
        return None
    ld = _norm_date(row.get("上榜日", ""))
    if len(ld) != 10:
        return None
    out = {"visible_after_close": True}   # 盘后披露 → T+1 才可用(防未来函数标记)
    for zh, en in _COL_MAP.items():
        if en in ("code", "name", "reason", "interpret", "list_date"):
            out[en] = str(row.get(zh, "")).strip()
        else:
            out[en] = _to_float(row.get(zh))
    out["code"] = code
    out["list_date"] = ld
    out["direction"] = lhb_direction(out.get("net_buy"))
    return out


def _event_key(ev: dict):
    """幂等键:同票同上榜日同原因视为一条(一票一日可因多原因多次上榜)。"""
    return (ev.get("list_date", ""), ev.get("reason", ""))


def _merge_incremental(new_events: list[dict], prev_events: list[dict]) -> list[dict]:
    """前向增量并集:新旧事件按 (list_date, reason) 去重合并,按 list_date 倒序。

    先放新事件(供字段更新),再补旧快照独有事件 → 保留历史上榜记录,幂等去重。
    """
    seen: set = set()
    merged: list[dict] = []
    for ev in list(new_events) + list(prev_events):
        k = _event_key(ev)
        if k in seen:
            continue
        seen.add(k)
        merged.append(ev)
    merged.sort(key=lambda x: x.get("list_date", ""), reverse=True)
    return merged


def _prev_snapshot(code: str) -> list[dict]:
    """读该票最近一次 lhb 快照(任意分区);无则 []。用于增量并集累积。"""
    try:
        prev = store.get_raw(_KIND, code)
        return prev if isinstance(prev, list) else []
    except FileNotFoundError:
        return []


def _default_window() -> tuple[str, str]:
    """未显式给区间时的默认窗口:[今天 − LHB_LOOKBACK_DAYS, 今天](YYYYMMDD)。"""
    end = date.today()
    start = end - timedelta(days=LHB_LOOKBACK_DAYS)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def fetch_range_df(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """拉一个日期区间的全市场龙虎榜明细,归一为 DataFrame(不落盘,便于回测/mock)。

    列含归一字段 + direction;**已剔除全部前视列**。空/失败 → 空 DataFrame(不抛)。
    start/end 接受 'YYYYMMDD' 或 'YYYY-MM-DD';缺省用 _default_window()。
    """
    import akshare as ak

    s0, e0 = _default_window()
    s = (start or s0).replace("-", "")
    e = (end or e0).replace("-", "")
    try:
        raw = ak.stock_lhb_detail_em(start_date=s, end_date=e)
    except Exception as exc:   # noqa: BLE001
        logger.warning("lhb 区间拉取失败 [%s,%s](降级空): %s", s, e, str(exc)[:120])
        return pd.DataFrame()
    if raw is None or raw.empty:
        return pd.DataFrame()
    rows = [r for r in (_norm_row(rec) for rec in raw.to_dict("records")) if r]
    return pd.DataFrame(rows)


def stale_codes(codes: list[str], max_days: float | None = None) -> list[str]:
    """返回需要重拉的票(缓存陈旧/无缓存)。供编排层 skip-if-cached 过滤用。"""
    md = LHB_STALE_DAYS if max_days is None else max_days
    return [c for c in codes if store.is_stale(_KIND, c, md)]


def fetch_lhb(start: str | None = None, end: str | None = None,
              skip_fresh: bool = True, max_days: float | None = None,
              codes: list[str] | None = None) -> dict[str, list[dict]]:
    """批量采集龙虎榜并**按票落盘**(前向增量、幂等、带新鲜度门控)。

    数据源天然按**日**返回全市场,故这里按区间一次拉取 → 分发到各票分区累积。
    参数:
      start/end  日期区间(缺省 = 今天回看 LHB_LOOKBACK_DAYS 天)。
      skip_fresh 门控开关(默认 True):对已有新鲜快照的票跳过写入(沿用既有)。
      max_days   门控阈值(默认 LHB_STALE_DAYS)。
      codes      仅落这些票(白名单);None = 落区间内出现的所有票。

    落盘:store.put_raw("lhb", code, [事件...], meta=...)。返回 {code: [事件...]}。
    单票写入失败 → log 跳过,不中断整批。
    """
    settings_ensure_dirs()
    md = LHB_STALE_DAYS if max_days is None else max_days
    df = fetch_range_df(start, end)
    out: dict[str, list[dict]] = {}
    if df.empty:
        logger.info("lhb 区间无数据 [%s,%s]", start, end)
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
            logger.warning("lhb 落盘 %s 失败(跳过): %s", code, str(exc)[:120])
    logger.info("lhb 采集完成:%d 票落盘%s", len(out),
                f",{len(failed)} 票失败" if failed else "")
    time.sleep(0)   # 无逐票网络调用(区间一次拉),无需限流 sleep
    return out


def load_lhb(code: str, date: str | None = None) -> list[dict]:
    """读单票龙虎榜快照。缺失抛 FileNotFoundError。date=None → 最新分区。"""
    return store.get_raw(_KIND, code, date=date or "latest")


def lhb_asof(code: str, as_of: str, date: str | None = None) -> list[dict]:
    """as-of 无未来函数切片:只返回**上榜日 < as_of** 的事件(按 list_date 倒序)。

    **注意披露时点**:龙虎榜盘后公布,上榜日 T 当天不可用(见模块 docstring)。
    因此这里用**严格小于**(list_date < as_of):在 as_of 这一天做决策时,只有
    **as_of 之前**上榜的记录才已公开。若需"含 as_of 当天且在 T+1 入场"的语义,
    调用方应把 as_of 设为决策日(通常 = T+1),此时上榜日 T < as_of 自然成立。
    date:锁读哪个采集分区(默认最新);缺快照抛 FileNotFoundError。
    """
    cutoff = str(as_of)[:10]
    items = load_lhb(code, date=date)
    return [ev for ev in items if str(ev.get("list_date", ""))[:10] < cutoff]


def settings_ensure_dirs() -> None:
    """确保数据目录存在(与既有采集器一致的初始化;隔离 settings 依赖便于 mock)。"""
    from tools.config import settings
    settings.ensure_dirs()
