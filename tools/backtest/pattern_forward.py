"""形态选股·前瞻胜率回测(V1 批次C / P4)。

把"某日达标池里的票"当作事件,复用 `event_study.forward_returns/summarize` 算
**前瞻 5/10/20 日收益 + 相对沪深300 Alpha + 胜率**,并对比 **regime 择时开关开/关** 的增益。

数据(只读 store view,不重算):
  · 达标池:历史各日「形态选股」view 的 `达标清单`(store.get_view,按日期)。
  · 基准:沪深300 指数 K线(collectors.index)。
  · 个股 K线:collectors.market(需含达标日之后的 bar 才有前瞻收益)。
  · regime:各日「市场状态」view 的标签(择时开关)。

防未来函数(与 BT.1 / event_study 同源):
  · 达标信号在 t 日盘后产生(screen 用 ≤t 数据);进场锚在 **t+1 交易日**(execution_lag=1),
    前瞻收益只用 t+1 及之后价 → 不回看。event_study 已保证 t0 及之后取价。
  · 本模块只做历史评估,绝不把"已实现前瞻收益"回喂任何实时信号。

数据现实:真胜率需**多日历史达标池快照**(靠盘后每日跑 screen_pattern 积累)。样本不足时
诚实报告"样本 N 天,待积累";快照够了同一入口直接出数。

边界:只读 view / K线,复用 event_study;不改 engine.py/event_study.py。
入口:`python -m tools.backtest.pattern_forward [--windows 5,10,20] [--date-from D] [--no-view]`。
"""
from __future__ import annotations

import logging

from tools.backtest import event_study
from tools.store import repo as store

logger = logging.getLogger("backtest.pattern_forward")

_POOL_VIEW = "形态选股"
_REGIME_VIEW = "市场状态"
_BENCH = "000300"                                   # 沪深300
# regime 择时"可交易"档(gate ON 只在市场偏强时买突破;占位默认,可调)
TRADABLE_LABELS = ("震荡", "分化", "牛市共振")
DEFAULT_WINDOWS = (5, 10, 20)


# ———————————————————— view 读取 helper(只读)————————————————————
def pool_dates() -> list[str]:
    """有「形态选股」达标池 view 的历史日期(升序)。"""
    out = []
    for d in store.list_dates("analysis"):
        try:
            store.get_view(_POOL_VIEW, date=d)
            out.append(d)
        except FileNotFoundError:
            continue
    return out


def collect_events(dates: list[str] | None = None) -> list[dict]:
    """从各日达标池 view 收「达标事件」[{code, date}]。dates 缺省=所有有 view 的日期。"""
    dates = dates or pool_dates()
    events = []
    for d in dates:
        try:
            v = store.get_view(_POOL_VIEW, date=d)
        except FileNotFoundError:
            continue
        for item in v.get("达标清单", []):
            code = item.get("code") if isinstance(item, dict) else item
            if code:
                events.append({"code": code, "date": d})
    return events


def regime_label(date: str) -> str | None:
    """某日市场状态标签;无 regime view→None。"""
    try:
        return store.get_view(_REGIME_VIEW, date=date).get("标签")
    except FileNotFoundError:
        return None


def _entry_offset_date(d: str):
    """达标日 → 进场锚(+1 自然日,event_study 取其后首个交易日=t+1 交易日,execution_lag=1)。"""
    import pandas as pd
    return pd.to_datetime(d) + pd.Timedelta(days=1)


# ———————————————————— 回测 ————————————————————
def _bench_df(fetch: bool):
    from tools.collectors import index
    try:
        return index.load_index(_BENCH)
    except FileNotFoundError:
        return index.fetch_index(["沪深300"]).get(_BENCH) if fetch else None


def run_backtest(windows=DEFAULT_WINDOWS, gate: bool = False,
                 dates: list[str] | None = None, fetch: bool = False) -> dict:
    """跑前瞻回测。gate=True 时只保留达标日 regime∈可交易档的事件。

    fetch=False:只读本地缓存(离线复算,默认);True 缺 K线/基准时采集。
    """
    from tools.collectors import market

    events = collect_events(dates)
    used_dates = sorted({e["date"] for e in events})
    if gate:
        events = [e for e in events if regime_label(e["date"]) in TRADABLE_LABELS]
    bench = _bench_df(fetch)

    per_event = []
    skipped = 0
    for e in events:
        try:
            kdf = market.load_kline(e["code"])
        except FileNotFoundError:
            kdf = market.fetch_kline([e["code"]]).get(e["code"]) if fetch else None
        if kdf is None or len(kdf) == 0:
            skipped += 1
            continue
        rows = event_study.forward_returns([_entry_offset_date(e["date"])], kdf,
                                           windows=windows, benchmark_df=bench)
        per_event.extend(rows)

    summary = event_study.summarize(per_event, windows=windows)
    return {
        "样本天数": len(used_dates), "样本日期": used_dates,
        "事件数": len(events), "有效个股K线缺失跳过": skipped,
        "gate": gate, "gate档": list(TRADABLE_LABELS) if gate else None,
        "窗口": list(windows), "汇总": summary,
        "口径": ("达标为事件·进场 t+1(execution_lag=1)·前瞻N交易日·Alpha vs 沪深300·"
                 f"防未来函数;{'regime择时ON(仅可交易档)' if gate else 'regime择时OFF(全量)'}"),
    }


def regime_gain(windows=DEFAULT_WINDOWS, dates: list[str] | None = None,
                fetch: bool = False) -> dict:
    """对比 regime 择时开关开/关的增益(胜率差 / 平均Alpha差,逐窗)。"""
    off = run_backtest(windows, gate=False, dates=dates, fetch=fetch)
    on = run_backtest(windows, gate=True, dates=dates, fetch=fetch)

    def _d(a, b):
        return round(a - b, 6) if (a is not None and b is not None) else None

    增益 = {}
    for n in windows:
        so, sn = off["汇总"].get(n, {}), on["汇总"].get(n, {})
        增益[n] = {
            "胜率差(ON−OFF)": _d(sn.get("胜率"), so.get("胜率")),
            "平均Alpha差(ON−OFF)": _d(sn.get("平均Alpha"), so.get("平均Alpha")),
            "样本_ON": sn.get("样本数"), "样本_OFF": so.get("样本数"),
        }
    return {"gate_off": off, "gate_on": on, "regime增益": 增益,
            "结论": _gain_verdict(增益, off, on)}


def _gain_verdict(增益, off, on) -> str:
    if off["样本天数"] == 0:
        return "无历史达标池快照(样本0天),待盘后每日跑 screen_pattern 积累后再评估"
    if on["事件数"] == 0:
        return "regime择时ON 后无事件(缺 regime view 或无可交易日),增益无法评估;先积累 regime 快照"
    ups = [g["胜率差(ON−OFF)"] for g in 增益.values() if g["胜率差(ON−OFF)"] is not None]
    if not ups:
        return "样本不足以判定 regime 增益(前瞻窗口未到期),待积累"
    avg = sum(ups) / len(ups)
    return (f"regime 择时平均提升胜率 {avg:+.2%}(仅 {off['样本天数']} 天样本,统计力弱,需继续积累)"
            if avg else "regime 择时对胜率无明显增益(样本弱)")


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="形态选股前瞻胜率回测")
    ap.add_argument("--windows", default="5,10,20", help="前瞻交易日窗口,逗号分隔")
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--no-view", action="store_true", help="不落回测 view")
    a = ap.parse_args(argv)
    windows = tuple(int(x) for x in a.windows.split(",") if x.strip())
    r = regime_gain(windows=windows, fetch=a.fetch)
    logger.info("样本天数=%d 事件数(OFF)=%d;结论:%s",
                r["gate_off"]["样本天数"], r["gate_off"]["事件数"], r["结论"])
    print(json.dumps({"regime增益": r["regime增益"], "结论": r["结论"],
                      "样本天数": r["gate_off"]["样本天数"]}, ensure_ascii=False, indent=2))
    if not a.no_view:
        store.put_view("形态选股回测", r)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
