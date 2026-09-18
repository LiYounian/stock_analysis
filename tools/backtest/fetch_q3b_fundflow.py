"""午盘 Q · Q3b 日线资金流采集(新浪 MoneyFlow · 供 Q3b 回测)。

## 为什么是"Q3b"而不是 Q3

原 Q3(`Q3_资金流前瞻.md`)的核心信号是 **当日 13:00→14:50 的分时主力净流入**
(`MainNetInflow_PM`),方案 §核心假设写明:「分时主力净流入在午后**仍持续为正**,
反映主力**当日已在建仓**」,并专门设双点复核来识别「日内 T+0 出货」。

那个数据**拿不到历史**(东财 `fflow/kline` 只返当天,`lmt` 给多大都没用)。

本模块用**日线**资金流,信号变成「主力**过去几天**的累积流向」——
这是**另一条策略**,不是 Q3 的近似:
    · 原 Q3 :盘中捕捉"主力今天正在动手"的**即时**信号 → 前瞻次日
    · Q3b   :看主力**前几日**的流向趋势 → 慢速的资金面确认

故独立命名、独立回测、独立判定。**不得把 Q3b 的结论当作 Q3 的结论。**

## 数据源:新浪 MoneyFlow(不是东财)

2026-09-18 实测东财资金流端点状态(同一时刻):
    push2 `fflow/kline` (分钟)     ✅ rc=0
    push2 `fflow/daykline` (日线)  ❌ ConnectionError
    push2his `fflow/daykline`      ❌ ConnectionError
→ 东财**日线**资金流当前不可用,而项目 `fundflow.py` 的三源降级链
  (东财→腾讯→新浪)里:
    腾讯   只 20 行(近一个月)      —— 不够回测
    新浪   **4015 行,回溯 2010 年** —— 采用

## ⚠️ 新浪口径的硬限制(直接决定 Q3b 只能做单信号)

新浪 MoneyFlow 只给 **`netamount`(主力净额)一列**,五档(小单/中单/大单/超大单)
与主力净占比**全是 NaN**(见 `fundflow._fetch_sina` 的"宁缺勿凑"注释)。

→ 原 Q3 的第二个信号 `LargeOrderPct`(大单+超大单 / 总成交额)**算不出来**。
   Q3b 只能用主力净流入单信号。这是数据限制,不是设计选择,已在回测报告标注。

→ 且新浪"主力"的划分口径与东财**不可比**(`fundflow.py` 标 tier=main_only)。
   故 Q3b 的阈值**不能照搬** Q3 的 `MainNetPM ≥ 0`,须自己按分位数定。

## 防未来红线

回测中只能用 **T-1 及以前**的日线资金流。**当日(T)的日线资金流含 14:50→15:00
那一段,14:50 决策时尚未发生,用它就是未来函数。**本采集器落全量,
防未来由回测侧 `< date` 切片保证(同 `midday_q_screen._load_klines` 口径)。

## 落盘

`data/analysis/midday_q/q3b_fundflow/<code>.parquet`,列:date, 主力净流入

⚠️ 测试环境研究用,非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

from tools.config import settings

logger = logging.getLogger("backtest.fetch_q3b_fundflow")

OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "q3b_fundflow"
BARS_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "m3b_bars"

MAX_ATTEMPTS = 3
_RETRY_BASE_SEC = 1.0

# 新浪限流:2026-09-18 实测并发 4 跑到约 3800 只后开始**整批失败**,
# 单只复测返 **HTTP 456**(新浪反爬状态码)。故默认并发降到 2 并加请求间隔。
# ⚠️ 别为了快调高 —— 触发 456 后会持续拒绝一段时间,反而更慢。
_SLEEP_BETWEEN_SEC = 0.25


def target_codes(limit: int | None = None) -> list[str]:
    """票池 = 已有分时 bar 的票(Q3b 回测要和 M3.b 同池才可比)。"""
    codes = sorted(p.stem for p in BARS_DIR.glob("*.parquet"))
    return codes[:limit] if limit else codes


def _ok_on_disk(path: Path, start: str, end: str) -> bool:
    """已落盘是否可信。同 M3.b 口径:两端各留 10 天容差(start/end 常落非交易日)。"""
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    start_ceil = (pd.Timestamp(start) + pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    end_floor = (pd.Timestamp(end) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    return (str(df["date"].min()) <= start_ceil
            and str(df["date"].max()) >= end_floor)


def fetch_one(code: str, start: str, end: str) -> tuple[str, int, str]:
    """拉单票日线资金流并落盘。返回 (code, 行数, 状态)。"""
    from tools.collectors import fundflow

    out = OUT_DIR / f"{code}.parquet"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            df = fundflow._fetch_sina(code)
            if df is None or df.empty:
                time.sleep(_RETRY_BASE_SEC * attempt)
                continue
            df = df[["date", "主力净流入"]].copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df[(df["date"] >= start) & (df["date"] <= end)]
            df = df.dropna(subset=["主力净流入"])
            if df.empty:
                return (code, 0, "empty")
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            df.sort_values("date").reset_index(drop=True).to_parquet(out, index=False)
            time.sleep(_SLEEP_BETWEEN_SEC)
            return (code, len(df), "ok")
        except Exception as e:
            logger.debug("%s 第%d次失败: %s", code, attempt, e)
            time.sleep(_RETRY_BASE_SEC * attempt)
    return (code, 0, "fail")


_JOB: dict = {}


def _init(start: str, end: str) -> None:
    _JOB["start"], _JOB["end"] = start, end


def _work(code: str) -> tuple[str, int, str]:
    return fetch_one(code, _JOB["start"], _JOB["end"])


def run(start: str, end: str, *, limit: int | None = None,
        procs: int = 2, force: bool = False) -> int:
    codes = target_codes(limit)
    if not codes:
        logger.error("票池为空:先跑 fetch_midday_q_bars 落分时 bar")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = codes if force else [c for c in codes
                                 if not _ok_on_disk(OUT_DIR / f"{c}.parquet", start, end)]
    skipped = len(codes) - len(todo)
    logger.info("Q3b 日线资金流:共 %d 只,已完整 %d 只(跳过),待取 %d 只;并发 %d",
                len(codes), skipped, len(todo), procs)
    if not todo:
        return 0

    ok = fail = empty = 0
    failed: list[str] = []
    t0 = time.time()
    with Pool(procs, initializer=_init, initargs=(start, end)) as pool:
        for i, (code, n, st) in enumerate(pool.imap_unordered(_work, todo), 1):
            if st == "ok":
                ok += 1
            elif st == "empty":
                empty += 1
            else:
                fail += 1
                failed.append(code)
            if i % 200 == 0 or i == len(todo):
                el = time.time() - t0
                logger.info("[%d/%d] 成功%d 失败%d 无数据%d | %.2fs/只 | 剩约%.0f分",
                            i, len(todo), ok, fail, empty, el / i,
                            el / i * (len(todo) - i) / 60)
    logger.info("完成:成功 %d / 失败 %d / 无数据 %d / 跳过 %d,用时 %.0f 分钟",
                ok, fail, empty, skipped, (time.time() - t0) / 60)
    if failed:
        fp = OUT_DIR.parent / "q3b_fetch_failed.json"
        fp.write_text(json.dumps({"failed": sorted(failed)}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        logger.warning("失败 %d 只,清单见 %s", len(failed), fp)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Q3b 日线资金流采集(新浪)")
    ap.add_argument("--start", default="2025-06-01",
                    help="多取半年:Q3b 信号要算 N 日均值,回测起点前需有历史")
    ap.add_argument("--end", default="2026-09-10")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--procs", type=int, default=2,
                    help="并发。实测 4 会触发新浪 HTTP 456 限流,默认 2")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    return run(a.start, a.end, limit=a.limit, procs=a.procs, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
