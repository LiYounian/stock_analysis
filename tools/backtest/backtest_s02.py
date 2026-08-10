"""策略 S02「放量后缩量回踩」持仓回测(信号日收盘机械买入基线)。

⚠️ 口径:本回测**复用 S01 的持仓回测器**(`position_backtest.simulate_position`:信号日收盘
P0 建仓 + 5 条离场状态机 + 一字板不可成交顺延),离场规则/参数一律读
`THRESHOLDS["趋势深跌反包"]`(未改一行离场逻辑)。因此它衡量的是 **S02 入场信号的原始
质量**——「信号日收盘机械买入、S01 离场」这条基线,**不代表 S02 的最终买法**。

与 S01 回测的唯一差异:信号来源换成 S02 的 `screen_s02.signal_at`(通过本文件新增的
`find_signals_s02` 适配),**不改 S01 的 find_signals 路径**。汇总/基准/最大回撤/Alpha
全部复用 `position_backtest` 的既有函数,保证与 S01 回测同口径可比。

防未来函数:S02 screener 本身只用 t 及之前(当周整周剔除);离场每日决策沿用 S01 状态机
(只用当日及之前)。数据只读复用 collectors.market.load_kline(缺则按 fetch 采集)。
入口:`python -m tools.backtest.backtest_s02 [--codes ...|--universe N] [--fetch] [--no-view]`。
"""
from __future__ import annotations

import logging

from tools.backtest import position_backtest as pb
from tools.pipeline import screen_s02
from tools.store import repo as store

logger = logging.getLogger("backtest.position_s02")

_VIEW = "放量后缩量回踩回测"
_MIN_SAMPLE = 10


def find_signals_s02(kdf, cfg: dict | None = None) -> list[int]:
    """扫 kdf 全历史,返回所有命中 S02 SELECT 的整数索引 t(升序)。历史不足自动跳过。"""
    n = len(kdf)
    start = max(screen_s02.min_history() - 1, 1)
    out = []
    for t in range(start, n):
        if screen_s02.signal_at(kdf, t).get("SELECT"):
            out.append(t)
    return out


def backtest_one_s02(kdf, code: str | None = None, bench=None) -> list[dict]:
    """单票:找所有 S02 信号 → 逐个用 S01 持仓回测器建仓跑离场 → 每笔补基准同持有期收益 + Alpha。"""
    trades = []
    for t in find_signals_s02(kdf):
        tr = pb.simulate_position(kdf, t, code=code)        # 复用 S01 离场状态机(未改)
        tr["code"] = code
        if tr["状态"] == "已离场" and bench is not None:
            br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
            tr["基准收益"] = br
            tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
        trades.append(tr)
    return trades


def summarize(codes: list[str] | None = None, fetch: bool = False,
              min_sample: int = _MIN_SAMPLE, generated_at: str | None = None) -> dict:
    """跨票**单进程串行**跑 S02 持仓回测并汇总(纯计算,不落库)。

    codes=None → 用本地所有滚动主档(store.list_master_codes)。缺 K线的票诚实跳过。
    绝不并行(防机器过热);慢无妨。数据不足时优雅标注,不报错、不编造。
    """
    from tools.collectors import market

    codes = codes if codes is not None else store.list_master_codes()
    bench = pb._load_bench(fetch)
    need = screen_s02.min_history()
    all_trades: list[dict] = []
    scanned = skipped = signal_codes = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        trades = backtest_one_s02(kdf, code=code, bench=bench)
        if trades:
            signal_codes += 1
        all_trades.extend(trades)

    summary = pb.summarize_trades(all_trades, min_sample=min_sample)
    result = {
        "策略": "放量后缩量回踩(S02)",
        "扫描票数": len(codes), "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped, "出信号票数": signal_codes,
        "有基准": bench is not None and len(bench) > 0,
        "汇总": summary,
        "口径": ("⚠️信号日收盘机械买入基线(复用 S01 持仓回测器:P0=信号日收盘 → 逐日 5 条离场"
                 "状态机撮合;离场参数读 THRESHOLDS['趋势深跌反包'])→ 衡量 S02 入场信号原始质量,"
                 "非 S02 最终买法;胜率/中位收益/盈亏比/最大回撤/平均持有天数 + 同持有期相对沪深300 Alpha;"
                 "防未来函数(S02 当周整周剔除、离场只用当日及之前);一字板不可成交顺延标注"),
        "免责声明": "历史回测证据,非投资建议;样本随主档积累与信号出现而增长,统计力逐步增强。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算(--fetch 采集后可得)"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_and_store(codes: list[str] | None = None, fetch: bool = False,
                  no_view: bool = False, min_sample: int = _MIN_SAMPLE,
                  generated_at: str | None = None) -> dict:
    """算汇总并落 view「放量后缩量回踩回测」(当前运行日期)。no_view=True 只算不落。"""
    result = summarize(codes=codes, fetch=fetch, min_sample=min_sample,
                       generated_at=generated_at)
    if not no_view:
        store.put_view(_VIEW, result)
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S02 放量后缩量回踩 持仓回测汇总(单进程串行)")
    ap.add_argument("--codes", help="逗号分隔代码(默认=本地所有滚动主档)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(--codes 优先)")
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--no-view", action="store_true", help="只算不落库(打印汇总)")
    ap.add_argument("--min-sample", type=int, default=_MIN_SAMPLE, help="统计力阈值")
    a = ap.parse_args(argv)

    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.universe:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    else:
        codes = None
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    r = run_and_store(codes=codes, fetch=a.fetch, no_view=a.no_view,
                      min_sample=a.min_sample, generated_at=stamp)
    logger.info("扫描 %d / 有效 %d / 出信号 %d;汇总:%s",
                r["扫描票数"], r["有效样本票"], r["出信号票数"], r["汇总"]["状态"])
    print(json.dumps({"扫描票数": r["扫描票数"], "有效样本票": r["有效样本票"],
                      "出信号票数": r["出信号票数"], "汇总": r["汇总"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
