"""通达信盘口微观结构采集:逐笔成交(盘后归档)。

借鉴 a-stock-data 的 mootdx(通达信 TCP 7709)源——**免费源里唯一能拿逐笔/盘口的通道**。
本采集器先做**逐笔成交盘后归档**(五档实时快照留待盘中定时任务扩展,见 docs)。

为什么盘后:逐笔当日累积、收盘后可一次性整段拉全(mootdx `transaction`/`transactions`),
契合项目现有盘后 EOD 流水线;五档是纯实时快照、不能历史回补,不在本采集器内。

数据源:mootdx `Quotes.factory(market="std")`,惰性导入(不装 mootdx 也能 import 本模块)。
  - `transaction(symbol, start, offset)`      当日分笔
  - `transactions(symbol, start, offset, date)` 历史某日分笔(date=YYYYMMDD)
  分页拉取(每页 _PAGE 条)累积到全天;列名按 mootdx 各版本**防御式归一**。
落盘:走 store 层,kind="tick"(parquet,大表),按 store 当前日期分区。港股不支持 → 跳过。

⚠️ 依赖 `mootdx`(requirements 已加);通达信走服务器池,盘中/服务器波动时可能失败,
   单票 try/except 降级不炸整批。逐笔数据量大,**建议只对自选池/达标池跑,勿全A**。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.tdx_l2")

_SOURCE = "mootdx"
_PAGE = 2000            # mootdx 单页最大条数
_MAX_TICKS = 60000     # 单票单日逐笔上限(极活跃票兜底,防失控)
# buyorsell(通达信主动买卖方向)→ 中文
_DIR = {0: "买", 1: "卖", 2: "中性"}


def _get_client():
    """惰性建 mootdx 标准市场客户端(A股)。未装 mootdx → 抛 ImportError(上层降级)。"""
    from mootdx.quotes import Quotes
    return Quotes.factory(market="std")


def _fetch_page(client, code: str, start: int, date: str | None) -> pd.DataFrame:
    """拉一页分笔。date 为 None → 当日 transaction;否则历史 transactions(date=YYYYMMDD)。"""
    if date:
        df = client.transactions(symbol=code, start=start, offset=_PAGE, date=int(date))
    else:
        df = client.transaction(symbol=code, start=start, offset=_PAGE)
    return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _fetch_raw(code: str, date: str | None = None) -> pd.DataFrame:
    """分页拉全天逐笔(拼接原始页)。空数据抛 ValueError。抽出便于测试 mock。"""
    client = _get_client()
    pages, start = [], 0
    while start < _MAX_TICKS:
        page = _fetch_page(client, code, start, date)
        if page is None or len(page) == 0:
            break
        pages.append(page)
        if len(page) < _PAGE:              # 末页
            break
        start += len(page)
    if not pages:
        raise ValueError(f"{code} 逐笔为空(非交易日/服务器无数据)")
    return pd.concat(pages, ignore_index=True)


def _pick_col(df: pd.DataFrame, *names):
    """按候选列名(容错 mootdx 版本差异)取首个存在的列名;无 → None。"""
    for nm in names:
        if nm in df.columns:
            return nm
    return None


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """mootdx 原始分笔 → 标准列 [time, price, volume, num, direction]。

    列名各版本有差异(volume/vol、buyorsell/nBuyOrSell 等),防御式取;缺列补 NA。
    volume 口径原样保留(通达信为「手」);direction 由 buyorsell 映射为 买/卖/中性。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["time", "price", "volume", "num", "direction"])
    c_time = _pick_col(df, "time", "date", "datetime")
    c_price = _pick_col(df, "price", "close")
    c_vol = _pick_col(df, "volume", "vol", "amount")
    c_num = _pick_col(df, "num", "count")
    c_bs = _pick_col(df, "buyorsell", "nBuyOrSell", "bs")
    out = pd.DataFrame()
    out["time"] = df[c_time].astype(str) if c_time else ""
    out["price"] = pd.to_numeric(df[c_price], errors="coerce") if c_price else pd.NA
    out["volume"] = pd.to_numeric(df[c_vol], errors="coerce") if c_vol else pd.NA
    out["num"] = pd.to_numeric(df[c_num], errors="coerce") if c_num else pd.NA
    if c_bs:
        out["direction"] = pd.to_numeric(df[c_bs], errors="coerce").map(_DIR).fillna("中性")
    else:
        out["direction"] = "中性"
    return out.sort_values("time", kind="stable").reset_index(drop=True)


def summarize(df: pd.DataFrame) -> dict:
    """逐笔派生微观结构摘要(供盘口异动/打板策略消费)。空 → 全 None/0。

    主动买卖:direction=买/卖 的成交量占比与净额;供「盘口买压」信号。
    """
    null = {"总笔数": 0, "总成交量": None, "主买占比": None, "主卖占比": None,
            "净主动买量": None, "大单笔数": 0}
    if df is None or len(df) == 0:
        return null
    vol = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    total = float(vol.sum())
    buy = float(vol[df["direction"] == "买"].sum())
    sell = float(vol[df["direction"] == "卖"].sum())
    # 大单阈值:单笔量 ≥ 该票当日单笔量的 95 分位(相对口径,免绝对手数误判)
    big_thr = float(vol.quantile(0.95)) if len(vol) else 0.0
    return {
        "总笔数": int(len(df)),
        "总成交量": round(total, 2),
        "主买占比": round(buy / total, 4) if total else None,
        "主卖占比": round(sell / total, 4) if total else None,
        "净主动买量": round(buy - sell, 2),
        "大单笔数": int((vol >= big_thr).sum()) if big_thr > 0 else 0,
    }


def fetch_one(code: str, date: str | None = None) -> pd.DataFrame:
    """拉单票逐笔并归一(不落盘)。港股返回空 df;逐笔为空抛 ValueError。"""
    from tools.config import stock_pool
    if stock_pool.is_hk(code):
        return pd.DataFrame(columns=["time", "price", "volume", "num", "direction"])
    return _normalize(_fetch_raw(code, date))


def fetch_ticks(codes: list[str], date: str | None = None) -> dict[str, dict]:
    """批量盘后归档逐笔并落盘。返回 {code: summarize(df)} 概览。

    date:None → 当日(收盘后跑);"YYYYMMDD" → 回补历史某日(需该日 store 分区)。
    单票失败/空/港股 → log 跳过,不中断整批。逐笔量大,建议只对自选池/达标池跑。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 逐笔 %s 归档...", i, n, code)
        if stock_pool.is_hk(code):
            logger.info("逐笔 %s:港股(mootdx std 不支持),跳过", code)
            continue
        try:
            df = fetch_one(code, date)
            if df.empty:
                failed.append(code)
                continue
            summ = summarize(df)
            store.put_raw("tick", code, df,
                          meta={"source": _SOURCE, "date": date, "rows": len(df)})
            # 摘要单独落小 json(供 serialize 按 as_of 无未来函数读,不重载大 parquet)
            store.put_raw("tick_summary", code, summ, meta={"source": _SOURCE, "date": date})
            out[code] = summ
            logger.info("逐笔 %s:%d 笔,主买占比 %s,净主动买量 %s",
                        code, summ["总笔数"], summ["主买占比"], summ["净主动买量"])
        except ImportError:
            logger.error("逐笔 %s 失败:未安装 mootdx(pip install mootdx)", code)
            failed.append(code)
        except Exception as e:
            failed.append(code)
            logger.warning("逐笔 %s 失败(降级跳过): %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("逐笔归档失败/跳过(%d): %s", len(failed), failed)
    return out


def load_ticks(code: str, date: str | None = None) -> pd.DataFrame:
    """读单票逐笔(分析层用,不触网)。date=None→最新分区;缺失抛 FileNotFoundError。"""
    return store.get_raw("tick", code, date=date or "latest")


def load_summary(code: str, date: str | None = None) -> dict | None:
    """读单票逐笔微观结构摘要(**轻量** json,不重载大 parquet)。

    摘要在 fetch_ticks 落盘时单独写为 tick_summary(小 json)。与 consensus 同款 as_of 语义:
      - date=None → 全局最新快照(store.get_raw)。
      - date='YYYY-MM-DD' → date-pin 到 ≤as_of 最近分区(store.get_raw_resolved),
        绝不返回未来分区;≤as_of 无任何分区 → None。
    无缓存 → None(上层降级,前端卡片自动不显示)。
    """
    try:
        if not date or date == "latest":
            return store.get_raw("tick_summary", code)
        payload, _resolved, _fetched = store.get_raw_resolved("tick_summary", code, date=date)
        return payload
    except FileNotFoundError:
        return None
