"""S01「趋势深跌反包」全A历史回测**薄驱动**(只编排,不改回测器算法)。

为什么需要它:`position_backtest.summarize` 走 `market.load_kline` → `store.get_raw`
默认取 raw 的「latest」日期分区。当最新分区是空占位目录(如当日盘中尚未落 K 线)时,
latest 解析到空目录会让全A票全部被跳过。本驱动显式指定「有数据的最新分区日期」直接读 raw,
再复用回测器现成的 `backtest_one` / `summarize_trades`,绕开 latest 空目录问题。

只新增本文件;不改 position_backtest 状态机 / screen_s01 入场 / strategy.py 参数。
入口:`python -m tools.backtest.run_s01_backtest [--date YYYY-MM-DD] [--limit N] [--out PATH]`。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from tools.backtest import position_backtest as pb
from tools.config import settings
from tools.pipeline import screen_s01
from tools.store import repo as store

logger = logging.getLogger("backtest.run_s01")

_BENCH = "000300"


def resolve_data_date(explicit: str | None = None) -> str:
    """选「实际有 kline 数据的最新 raw 日期分区」。显式 date 优先;否则倒序找首个非空 kline 目录。"""
    if explicit:
        return explicit
    raw = settings.DATA_RAW
    dates = sorted(
        (p.name for p in raw.iterdir()
         if p.is_dir() and store._DATE_RE.match(p.name)),
        reverse=True,
    )
    for d in dates:
        kdir = raw / d / "kline"
        if kdir.is_dir() and any(kdir.glob("*.parquet")):
            return d
    raise FileNotFoundError(f"{raw} 下未找到任何含 kline 的日期分区")


def list_codes(date: str, limit: int | None = None) -> list[str]:
    """列出该日期分区 kline 目录下的全部票代码(升序;.meta.json 不计)。"""
    kdir = settings.DATA_RAW / date / "kline"
    codes = sorted(p.stem for p in kdir.glob("*.parquet"))
    return codes[:limit] if limit else codes


def load_bench(date: str):
    """读该日期分区的沪深300指数 K 线;缺则 None(Alpha 不计,诚实标注)。"""
    try:
        return store.get_raw("index_kline", _BENCH, date=date)
    except FileNotFoundError:
        return None


def _board(code: str) -> str:
    if code[:3] in ("688", "689") or code[:2] == "68":
        return "科创板"
    if code[:2] == "30":
        return "创业板"
    if code[:1] in ("8", "4"):
        return "北交所"
    if code[:2] == "60":
        return "沪主板"
    if code[:2] in ("00", "02"):
        return "深主板"
    return "其他"


def run(date: str | None = None, limit: int | None = None,
        min_sample: int = pb._MIN_SAMPLE) -> dict:
    """全A(或前 limit 只)跑 S01 持仓回测。显式日期读 raw,复用回测器算法与汇总。"""
    d = resolve_data_date(date)
    codes = list_codes(d, limit)
    bench = load_bench(d)
    logger.info("数据日期=%s 票数=%d 基准=%s", d, len(codes), "有" if bench is not None else "无")

    all_trades: list[dict] = []
    by_board: dict[str, list[dict]] = {}
    scanned = skipped = signal_codes = 0
    t0 = time.time()
    for i, code in enumerate(codes):
        try:
            kdf = store.get_raw("kline", code, date=d)
        except FileNotFoundError:
            skipped += 1
            continue
        if kdf is None or len(kdf) < screen_s01.min_history():
            skipped += 1
            continue
        scanned += 1
        trades = pb.backtest_one(kdf, code=code, bench=bench)
        if trades:
            signal_codes += 1
        all_trades.extend(trades)
        by_board.setdefault(_board(code), []).extend(trades)
        if (i + 1) % 500 == 0:
            logger.info("...进度 %d/%d 已扫 %d 出信号票 %d 累计笔 %d (%.1fs)",
                        i + 1, len(codes), scanned, signal_codes, len(all_trades),
                        time.time() - t0)

    summary = pb.summarize_trades(all_trades, min_sample=min_sample)
    board_summary = {
        b: pb.summarize_trades(tr, min_sample=min_sample)
        for b, tr in sorted(by_board.items())
    }
    result = {
        "策略": "趋势深跌反包(S01)",
        "数据日期": d,
        "扫描票数": len(codes),
        "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped,
        "出信号票数": signal_codes,
        "有基准": bench is not None and len(bench) > 0,
        "汇总": summary,
        "分板块": board_summary,
        "耗时秒": round(time.time() - t0, 1),
        "口径": ("显式日期分区读 raw → 复用 position_backtest.backtest_one 逐票扫 S01 信号建仓 → "
                 "5 条离场状态机撮合 → summarize_trades 汇总;防未来+一字板顺延口径不变"),
        "免责声明": "历史回测证据,非投资建议;本地历史 ~1.4 年,入场需 251 根,可扫窗仅约 80 交易日,样本置信度有限。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算"
    return result


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="S01 趋势深跌反包 全A历史回测薄驱动")
    ap.add_argument("--date", help="数据日期分区(默认=最新有 kline 的分区)")
    ap.add_argument("--limit", type=int, help="只跑前 N 只(冒烟/调试)")
    ap.add_argument("--min-sample", type=int, default=pb._MIN_SAMPLE)
    ap.add_argument("--out", help="把完整结果 JSON 写到该路径")
    a = ap.parse_args(argv)

    r = run(date=a.date, limit=a.limit, min_sample=a.min_sample)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("结果写入 %s", a.out)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
