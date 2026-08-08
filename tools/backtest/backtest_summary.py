"""前瞻回测闭环·跨多日累积汇总(独立入口)。

把「盘后每日跑 screen_pattern 攒下的历史达标池快照」滚动汇总成一份证据:
**这套选股到底准不准**。对每个历史达标日的达标池,复用 `pattern_forward` /
`event_study` 的**防未来函数**口径算前瞻 5/10/20 日收益 + 相对沪深300 的 Alpha,
再**按市场状态(regime)分层**看各持有期胜率(择时增益的证据基座)。

与 `pattern_forward` 的分工(不重复造轮子,只读复用):
  · `pattern_forward` = 单次"gate 开/关"对比 + 落「形态选股回测」view。
  · 本模块 = **跨多达标日累积** + **逐日快照** + **regime 分层胜率**,落每日
    `data/analysis/<date>/backtest.json`,给"数周后出证据"的闭环用。复用
    `pattern_forward.{pool_dates,collect_events,regime_label,_entry_offset_date,
    _bench_df}` 与 `event_study.{forward_returns,summarize}`,口径逐字段一致。

防未来函数(与 pattern_forward / event_study 同源,绝不回看):
  · 达标信号在 t 日盘后产生(screen 用 ≤t 数据);进场锚 **t+1 交易日**
    (execution_lag=1);前瞻收益只用 t+1 及之后价。
  · 只做历史评估,绝不把"已实现前瞻收益"回喂任何实时信号。
  · 某达标日+某窗口的 t+1+N 根 bar 尚未到来(未来没发生)→ 该窗记 None,
    **诚实标注"待观察"**,绝不用未到的数据编造收益。

数据现实:现只有少数几天快照 + 前瞻窗多半未到期 → 绝大多数"待观察",这是**对的**。
目的是把机器挂上、开始攒;快照与 K线积累够了,同一入口直接出数。

边界:只新增本文件 + 其 test;只读复用 pattern_forward / event_study / store;
不改 run.py / screen_pattern / council / web / engine.py。

入口:`python -m tools.backtest.backtest_summary
        [--windows 5,10,20] [--fetch] [--per-date] [--no-view] [--min-sample N]`
"""
from __future__ import annotations

import logging

from tools.backtest import event_study, pattern_forward
from tools.store import repo as store

logger = logging.getLogger("backtest.summary")

DEFAULT_WINDOWS = pattern_forward.DEFAULT_WINDOWS       # (5, 10, 20)
_BACKTEST_VIEW = "backtest"                             # → data/analysis/<date>/backtest.json
_SUMMARY_VIEW = "形态选股回测汇总"                       # 可读别名(同内容,便于 web/报告检索)
_MIN_SAMPLE = 10                                        # 低于此样本视为"统计力弱"
_UNTRADED = "未分类"                                    # 无 regime view 时的分层桶


# ———————————————————— 窗口/整体状态标注 ————————————————————
def _window_state(n: int, sample: int, min_sample: int) -> str:
    """单持有期的成熟度标注(防未来函数下,样本=已到期事件数)。"""
    if sample <= 0:
        return f"待观察(前瞻{n}日未到期或无K线)"
    if sample < min_sample:
        return f"样本少(N={sample}),统计力弱,待积累"
    return f"可用(N={sample})"


def _overall_state(day_count: int, per_window: dict, windows) -> str:
    """整份汇总的成熟度结论。"""
    if day_count == 0:
        return "无达标池快照(0 达标日):待盘后每日跑 screen_pattern 积累后再评估"
    matured = [n for n in windows if (per_window.get(n) or {}).get("样本数", 0) > 0]
    if not matured:
        return f"已积累 {day_count} 达标日,但前瞻窗均未到期 → 全部待观察(继续攒 K线即自动出数)"
    return f"已积累 {day_count} 达标日,{len(matured)}/{len(windows)} 个持有期到期出数(样本弱,证据初现)"


# ———————————————————— 逐事件收益(带 regime 标签,供分层)————————————————————
def _events_with_returns(windows, dates, fetch: bool, bench):
    """对每个达标事件算前瞻收益 + 打上其达标日的 regime 标签。

    Returns:
        (rows, meta):
          rows = [{code, date, regime, 前瞻{N:收益|None}, alpha{N:值}}]
          meta = {达标日列表, 事件数, K线缺失跳过}
    复用 pattern_forward.collect_events(达标池) + event_study.forward_returns(前瞻)。
    进场锚 t+1(_entry_offset_date + forward_returns 取 t0 及之后价)。
    """
    from tools.collectors import market

    events = pattern_forward.collect_events(dates)
    used_dates = sorted({e["date"] for e in events})
    rows, skipped = [], 0
    for e in events:
        try:
            kdf = market.load_kline(e["code"])
        except FileNotFoundError:
            kdf = market.fetch_kline([e["code"]]).get(e["code"]) if fetch else None
        if kdf is None or len(kdf) == 0:
            skipped += 1
            continue
        fr = event_study.forward_returns(
            [pattern_forward._entry_offset_date(e["date"])], kdf,
            windows=windows, benchmark_df=bench)
        if not fr:
            skipped += 1
            continue
        r = fr[0]
        rows.append({"code": e["code"], "date": e["date"],
                     "regime": pattern_forward.regime_label(e["date"]) or _UNTRADED,
                     "前瞻": r.get("前瞻", {}), "alpha": r.get("alpha", {})})
    return rows, {"达标日列表": used_dates, "事件数": len(events), "K线缺失跳过": skipped}


def _summ(rows, windows, min_sample: int) -> dict:
    """把一组事件行(含 前瞻/alpha)汇成 {N:{样本数,胜率,平均收益,平均Alpha,状态}}。

    直接借 event_study.summarize 保持胜率/均值口径一致,再补"成熟度状态"。
    """
    base = event_study.summarize(rows, windows=windows)
    out = {}
    for n in windows:
        b = base.get(n, {})
        out[n] = {
            "样本数": b.get("样本数", 0),
            "胜率": b.get("胜率"),
            "平均收益": b.get("平均收益"),
            "平均Alpha": b.get("平均Alpha"),
            "状态": _window_state(n, b.get("样本数", 0), min_sample),
        }
    return out


def _stratify(rows, windows, min_sample: int) -> dict:
    """按 regime 分层的各持有期胜率:{regime: {N:{...}}}。桶按事件数降序。"""
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(r["regime"], []).append(r)
    ordered = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return {label: {"事件数": len(rs), "各持有期": _summ(rs, windows, min_sample)}
            for label, rs in ordered}


def _per_date(rows, windows, dates, min_sample: int) -> list[dict]:
    """逐达标日快照:[{日期, 市场状态, 事件数, 各持有期{...}}](按日期升序)。"""
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["date"], []).append(r)
    out = []
    for d in sorted(dates):
        rs = by_date.get(d, [])
        out.append({
            "日期": d,
            "市场状态": pattern_forward.regime_label(d) or _UNTRADED,
            "事件数": len(rs),
            "各持有期": _summ(rs, windows, min_sample),
        })
    return out


# ———————————————————— 主汇总 ————————————————————
def summarize(windows=DEFAULT_WINDOWS, dates: list[str] | None = None,
              fetch: bool = False, min_sample: int = _MIN_SAMPLE,
              generated_at: str | None = None) -> dict:
    """跨多达标日的前瞻回测汇总(纯计算,不落库)。

    Args:
        windows: 前瞻交易日窗口。
        dates: 指定达标日;缺省=所有有「形态选股」view 的日期(pattern_forward.pool_dates)。
        fetch: 缺 K线/基准时是否采集(默认 False=只读缓存,离线安全)。
        min_sample: 低于此样本数的持有期标"统计力弱"。
        generated_at: 生成时间戳(调用方注入,便于测试/复现;None 则不带)。

    Returns:
        dict,含 task 要求字段:样本数 / 各持有期(胜率·平均收益·Alpha) /
        按市场状态分层 / 逐日 / 状态标注 / 口径 / 元信息。数据不足时优雅标注"待观察"。
    """
    dates = dates if dates is not None else pattern_forward.pool_dates()
    bench = pattern_forward._bench_df(fetch)
    rows, meta = _events_with_returns(windows, dates, fetch, bench)

    per_window = _summ(rows, windows, min_sample)
    result = {
        "样本数": meta["事件数"],
        "达标日数": len(meta["达标日列表"]),
        "达标日列表": meta["达标日列表"],
        "各持有期": per_window,
        "按市场状态分层": _stratify(rows, windows, min_sample),
        "逐日": _per_date(rows, windows, meta["达标日列表"], min_sample),
        "K线缺失跳过": meta["K线缺失跳过"],
        "窗口": list(windows),
        "基准": pattern_forward._BENCH,
        "有基准": bench is not None and len(bench) > 0,
        "状态": _overall_state(len(meta["达标日列表"]), per_window, windows),
        "口径": ("达标为事件·进场 t+1(execution_lag=1)·前瞻N交易日·"
                 "Alpha vs 沪深300·regime 按达标日标签分层·防未来函数(窗口未到期记待观察)"),
        "免责声明": "历史回测证据,非投资建议;样本随每日快照积累增长,统计力逐步增强。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算(--fetch 采集后可得)"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_and_store(windows=DEFAULT_WINDOWS, fetch: bool = False,
                  per_date: bool = False, no_view: bool = False,
                  min_sample: int = _MIN_SAMPLE,
                  generated_at: str | None = None) -> dict:
    """算汇总并落库:最新达标日的 backtest.json(+可选逐日各自的 backtest.json)。

    per_date=True 时,把「逐日」里每天那一段也各自写回 data/analysis/<该日>/backtest.json,
    让每天的达标快照自带一份前瞻证据(daily 积累用)。默认只写最新一份滚动汇总。
    """
    result = summarize(windows=windows, fetch=fetch, min_sample=min_sample,
                       generated_at=generated_at)
    if no_view:
        return result

    used = result["达标日列表"]
    if used:
        store.put_view(_BACKTEST_VIEW, result, date=used[-1])
        store.put_view(_SUMMARY_VIEW, result, date=used[-1])
        if per_date:
            for day in result["逐日"]:
                d = day["日期"]
                daily = dict(result)                       # 同口径 + 突出当日
                daily["当日"] = day
                store.put_view(_BACKTEST_VIEW, daily, date=d)
    else:
        # 0 达标日:仍落一份"待观察"到最新分析日期,让 web/巡检看得到机器已挂上
        try:
            latest = store.list_dates("analysis")[-1]
            store.put_view(_BACKTEST_VIEW, result, date=latest)
        except IndexError:
            logger.info("无任何分析日期目录,跳过落库(纯打印)")
    return result


# ———————————————————— CLI ————————————————————
def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="前瞻回测闭环·跨多日累积汇总")
    ap.add_argument("--windows", default="5,10,20", help="前瞻交易日窗口,逗号分隔")
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--per-date", action="store_true", help="逐日也各自写回 backtest.json")
    ap.add_argument("--no-view", action="store_true", help="只算不落库(打印汇总)")
    ap.add_argument("--min-sample", type=int, default=_MIN_SAMPLE, help="统计力阈值")
    a = ap.parse_args(argv)

    windows = tuple(int(x) for x in a.windows.split(",") if x.strip())
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    r = run_and_store(windows=windows, fetch=a.fetch, per_date=a.per_date,
                      no_view=a.no_view, min_sample=a.min_sample, generated_at=stamp)

    logger.info("达标日数=%d 事件数=%d;结论:%s", r["达标日数"], r["样本数"], r["状态"])
    print(json.dumps({
        "达标日数": r["达标日数"], "样本数": r["样本数"],
        "各持有期": r["各持有期"], "按市场状态分层": r["按市场状态分层"],
        "状态": r["状态"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
