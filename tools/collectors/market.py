"""行情采集:日 K线、量价。

多源 fallback:腾讯 → 新浪 → 东财。
  - 主源腾讯 `stock_zh_a_hist_tx`:本机实测可用,返回 OHLCV+成交额+换手率。
  - 东财 `stock_zh_a_hist`:本机被其 TLS 指纹反爬(python-requests 被 RST),留作其他环境备选。
    详见 docs/问题/问题台账.md R4。
落盘:走 store 层(kind="kline",parquet),旁记 meta.source=实际命中源。
契约见 docs/计划/P1_技术面打通.md Step 1。
"""
from __future__ import annotations

import logging
import random
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed

import pandas as pd

from tools.collectors import baostock_src
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.market")

# 统一输出列
_STD_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]
# 东财中文列 → 标准列
_EM_COL_MAP = {
    "日期": "date", "开盘": "open", "收盘": "close", "最高": "high", "最低": "low",
    "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg", "换手率": "turnover",
}
# 源优先级(本机腾讯可用;东财被指纹墙,置末)
DEFAULT_SOURCES = ("tencent", "sina", "eastmoney")


def market_prefix(code: str) -> str:
    """6 位代码 → 带交易所前缀(sh/sz/bj)。腾讯/新浪接口需带前缀。"""
    if code[0] in ("6", "9"):
        return f"sh{code}"
    if code[0] in ("0", "2", "3"):
        return f"sz{code}"
    if code[0] in ("8", "4"):
        return f"bj{code}"
    return code


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名/类型/排序,补算 pct_chg。"""
    df = df.rename(columns=_EM_COL_MAP)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close", "volume", "amount", "turnover"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "pct_chg" not in df.columns or df["pct_chg"].isna().all():
        df["pct_chg"] = df["close"].pct_change() * 100  # 首行 NaN,正常
    for c in _STD_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_STD_COLS]


def _fetch_tencent(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist_tx(symbol=market_prefix(code),
                                 start_date=start, end_date=end, adjust=adjust)


def _fetch_sina(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    df = ak.stock_zh_a_daily(symbol=market_prefix(code),
                             start_date=start, end_date=end, adjust=adjust)
    return df


def _fetch_eastmoney(code, start, end, adjust) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_zh_a_hist(symbol=code, period="daily",
                              start_date=start, end_date=end, adjust=adjust)


_FETCHERS = {"tencent": _fetch_tencent, "sina": _fetch_sina, "eastmoney": _fetch_eastmoney}


def _fetch_one_with_source(code: str, start: str, end: str, adjust: str,
                           sources: tuple[str, ...] = DEFAULT_SOURCES
                           ) -> tuple[pd.DataFrame, str]:
    """拉单票日 K线,返回 (归一化 df, 命中源名)。全失败抛 ConnectionError。

    落盘时要把实际命中的源写进 raw meta.source,故此处把命中源一并透出。
    """
    errors = []
    for src in sources:
        try:
            df = _FETCHERS[src](code, start, end, adjust)
            if df is None or len(df) == 0:
                raise ValueError("空数据")
            out = _normalize(df)
            logger.debug("K线 %s 命中源 %s", code, src)
            return out, src
        except Exception as e:  # 换下一个源
            errors.append(f"{src}: {type(e).__name__} {str(e)[:40]}")
    raise ConnectionError(f"{code} 所有源均失败: {errors}")


def fetch_one(code: str, start: str, end: str, adjust: str,
              sources: tuple[str, ...] = DEFAULT_SOURCES) -> pd.DataFrame:
    """拉单票日 K线(多源 fallback,不落盘)。

    依次尝试 sources 各源;全失败抛 ConnectionError(不返回空 df 伪装成功)。
    """
    df, _src = _fetch_one_with_source(code, start, end, adjust, sources)
    return df


def _default_range(start: str | None, end: str | None) -> tuple[str, str]:
    """缺省日期区间:end=今天,start≈今天往前 KLINE_DAYS×2 自然日(覆盖非交易日)。"""
    if start is None:
        start = (pd.Timestamp.today() - pd.Timedelta(days=settings.KLINE_DAYS * 2)
                 ).strftime("%Y%m%d")
    if end is None:
        end = pd.Timestamp.today().strftime("%Y%m%d")
    return start, end


def fetch_kline(codes: list[str], start: str | None = None,
                end: str | None = None, adjust: str = settings.KLINE_ADJUST,
                workers: int | None = None) -> dict[str, pd.DataFrame]:
    """拉取多票 K线并落盘 parquet(逐只 akshare 多源 fallback;主档缺失时的兜底路径)。

    start/end 为 None 时:end=今天,start≈今天往前 KLINE_DAYS×2 自然日(覆盖非交易日)。
    单票失败记 logger 并跳过,不中断整批;返回成功票的 {code: DataFrame}。

    workers(方案B 并发兜底):None→取 settings.FETCH_WORKERS。
      =1 串行(默认,含 FETCH_SLEEP_SEC 节流);
      >1 有界线程池 + 每请求 jitter(不 sleep,靠并发度而非节流控速)。
    """
    settings.ensure_dirs()
    start, end = _default_range(start, end)
    workers = settings.FETCH_WORKERS if workers is None else workers
    n = len(codes)
    out: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    def _do(code: str) -> tuple[pd.DataFrame, str]:
        if workers > 1 and settings.FETCH_JITTER_SEC:
            time.sleep(random.uniform(0, settings.FETCH_JITTER_SEC))
        df, src = _fetch_one_with_source(code, start, end, adjust)
        store.put_raw("kline", code, df, meta={"source": src})
        return df, src

    if workers and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_do, c): c for c in codes}
            for done, f in enumerate(as_completed(futs), 1):
                code = futs[f]
                try:
                    df, src = f.result()
                    out[code] = df
                    logger.info("[%d/%d] K线 %s 落盘 %d 根(源 %s)", done, n, code, len(df), src)
                except Exception as e:
                    failed.append(code)
                    logger.error("K线 %s 失败: %s", code, e)
    else:
        for i, code in enumerate(codes, 1):
            logger.info("[%d/%d] K线 %s 采集...", i, n, code)
            try:
                df, src = _do(code)
                out[code] = df
                logger.info("K线 %s 落盘 %d 根(源 %s)", code, len(df), src)
            except Exception as e:
                failed.append(code)
                logger.error("K线 %s 失败: %s", code, e)
            time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("拉取失败票(%d): %s", len(failed), failed)
    return out


# ————————————————————————————————————————————————
# 滚动主档:①全量历史落地(baostock) ②每日增量(akshare spot)
# ————————————————————————————————————————————————
def _backfill_master_chunk(args: tuple[list[str], str, str, str]) -> tuple[int, list[str]]:
    """一个子进程独立登录 Baostock 并写入自己负责的代码分片。"""
    codes, start, end, adjust = args
    ok, failed = 0, []
    with baostock_src.session():
        for code in codes:
            try:
                df = baostock_src.fetch_one(code, start, end, adjust=adjust)
                store.put_master_kline(code, df, meta={"source": "baostock", "adjust": adjust})
                ok += 1
            except Exception as ex:
                logger.error("主档 %s 失败: %s", code, ex)
                failed.append(code)
    return ok, failed


def backfill_master(codes: list[str], start: str | None = None, end: str | None = None,
                    adjust: str = settings.KLINE_ADJUST, workers: int = 1) -> dict[str, int]:
    """用 baostock 逐只拉全历史日K(前复权)→ 全量覆盖写滚动主档。

    baostock 不封 → 无 sleep 快速循环。start/end 用 YYYYMMDD 或 YYYY-MM-DD(缺省同 fetch_kline)。
    workers>1 时按代码分片给多个独立 Baostock 连接并行执行；单只失败不中断整批。
    """
    settings.ensure_dirs()
    start, end = _default_range(start, end)
    s = _to_dash(start)
    e = _to_dash(end)
    workers = max(1, int(workers))
    if workers == 1:
        ok, failed = _backfill_master_chunk((codes, s, e, adjust))
    else:
        chunks = [codes[i::workers] for i in range(workers)]
        ok, failed = 0, []
        with ProcessPoolExecutor(max_workers=workers) as ex:
            args = [(chunk, s, e, adjust) for chunk in chunks]
            for done, (part_ok, part_failed) in enumerate(ex.map(_backfill_master_chunk, args), 1):
                ok += part_ok
                failed.extend(part_failed)
                logger.info("[%d/%d] 主档分片完成:成功 %d / 失败 %d",
                            done, len(chunks), part_ok, len(part_failed))
    if failed:
        logger.warning("主档落地失败票(%d): %s", len(failed), failed[:50])
    return {"ok": ok, "failed": len(failed)}


# 东财 spot 中文列 → 标准列
_SPOT_COL_MAP = {
    "代码": "code", "今开": "open", "最高": "high", "最低": "low",
    "最新价": "close", "成交量": "volume", "成交额": "amount",
    "换手率": "turnover", "涨跌幅": "pct_chg",
}


def fetch_spot_all() -> pd.DataFrame:
    """akshare stock_zh_a_spot_em():一次请求拿全A当日 bar。返回标准列(含 code)。"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    if df is None or len(df) == 0:
        raise ConnectionError("akshare spot 全A当日行情为空")
    df = df.rename(columns=_SPOT_COL_MAP)
    keep = [c for c in ("code", "open", "high", "low", "close",
                        "volume", "amount", "turnover", "pct_chg") if c in df.columns]
    df = df[keep].copy()
    for c in keep:
        if c != "code":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df


def update_master_from_spot(codes: list[str] | None = None, date: str | None = None,
                            spot: pd.DataFrame | None = None,
                            source: str = "akshare_spot") -> dict[str, int]:
    """每日增量:一次 spot 拿全A当日 bar → 逐股按 date 去重 append 到主档(幂等)。

    codes=None → 更新所有已有主档的股票(spot 缺该股=停牌,跳过)+ 新股首次落。
    date=None → 用今天(YYYY-MM-DD)。spot 可传入(测试/复用),否则实时拉。

    幂等:同日多次跑,append_master_kline 按 date 覆盖,不产生重复行。
    注:spot 为未复权当日价;前复权主档在无新除权时"最新 bar 的 qfq 值=其实际价",
    故追加正确;发生除权后需 backfill_master 全量重算(见方案文档 §4)。
    """
    if spot is None:
        spot = fetch_spot_all()
    d = date or pd.Timestamp.today().strftime("%Y-%m-%d")
    spot = spot.set_index("code")
    if codes is None:
        codes = sorted(set(store.list_master_codes()) | set(spot.index))
    ok = 0
    skipped = 0
    for code in codes:
        if code not in spot.index:
            skipped += 1              # 停牌/无当日 bar
            continue
        row = spot.loc[code]
        bar = pd.DataFrame([{
            "date": pd.Timestamp(d),
            "open": row.get("open"), "high": row.get("high"), "low": row.get("low"),
            "close": row.get("close"), "volume": row.get("volume"),
            "amount": row.get("amount"), "turnover": row.get("turnover"),
            "pct_chg": row.get("pct_chg"),
        }])
        store.append_master_kline(code, bar, meta={"source": source})
        ok += 1
    logger.info("spot 增量 append:更新 %d 只,跳过(停牌/无 bar)%d 只 @ %s", ok, skipped, d)
    return {"ok": ok, "skipped": skipped}


def _to_dash(d: str) -> str:
    """YYYYMMDD → YYYY-MM-DD(已带 - 则原样)。"""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and "-" not in d else d


def load_kline(code: str) -> pd.DataFrame:
    """读单票 K线(分析层用,不触网)。**优先读滚动主档**,回退当日 raw 分区。

    对外签名不变(向后兼容);内部读取源:主档存在→主档全历史;否则→现有 raw。
    两处皆缺抛 FileNotFoundError。
    """
    if store.has_master_kline(code):
        return store.get_master_kline(code)
    return store.get_raw("kline", code)
