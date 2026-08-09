"""策略 S01「趋势深跌反包」带离场规则的**持仓回测器**(状态机)。

比 pattern_forward 的「前瞻固定 N 日」重一层:对每个历史信号日**建仓**,逐日跑
5 条离场状态机撮合出真实的「持有天数 + 离场价 + 收益」,再汇总
胜率 / 中位收益 / 盈亏比 / 最大回撤 / 平均持有天数 + 同持有期相对沪深300 Alpha。

建仓/离场口径(策略端定稿;均前复权收盘):
  · 建仓价 P0 = 信号日(t)收盘;进场日 = 信号日(当日盘后即视为持有,次日起逐日检查离场)。
  · 离场状态机(优先级 = 列表顺序,同日多条命中取靠前者成交价):
      1 硬止损  :LOW≤P0×硬止损系数 → 以 P0×硬止损系数 成交(盘中触及)
      2 趋势止损:C<MA13(收盘跌破)→ 收盘价
      3 加速止盈:C/C₍ₜ₋₃₎−1 ≥ 阈值 → 收盘价
      4 放量滞涨:V>V₍ₜ₋₁₎×倍数 且 (C/C₍ₜ₋₁₎−1)<(C₍ₜ₋₁₎/C₍ₜ₋₂₎−1) → 收盘价
      5 时间成本:持股>最大持有交易日 且 C/P0−1<收益阈值 → 第(N+1)日收盘强制离场

防未来函数(正确性红线):
  · 信号 screener 的 H52 已不含当日;离场每日决策只用 j(该日)及之前的量,绝不回看。
  · 窗口/K线未到来的天不编造 → 持仓到数据末仍未离场 → 状态「持有中(数据不足)」,收益 None。

一字板口径(涨跌停一字、无法成交):某离场日命中离场条件但当日为**跌停一字**(零振幅、
卖不出)→ 该日不成交,**顺延**至下一可成交日,并在该笔标注「一字板顺延」+ 顺延天数;
绝不假装能在一字板上成交。

参数全读 `THRESHOLDS["趋势深跌反包"]`(入场/离场/一字板),不散写硬编码。
数据只读复用 collectors.market.load_kline(优先主档)+ 沪深300 基准(collectors.index)。
边界:只新增本文件;不改 engine / pattern_forward / event_study / screen_pattern。
入口:`python -m tools.backtest.position_backtest [--codes ...|--universe N] [--fetch] [--no-view]`。
"""
from __future__ import annotations

import logging
import statistics

import numpy as np

from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_s01
from tools.store import repo as store

logger = logging.getLogger("backtest.position_s01")

_ALL = THRESHOLDS["趋势深跌反包"]
_EXIT = _ALL["离场"]
_LIMIT = _ALL["一字板"]
_BENCH = "000300"                                       # 沪深300


# ———————————————————— 一字板判定(卖方不可成交)————————————————————
def _limit_frac(code: str | None, cfg: dict) -> float:
    """该票的涨跌停幅度:科创/创业(30/68/688/689 前缀)取宽档,其余主档档。"""
    lc = cfg["一字板"]
    if code:
        for pre in lc.get("科创创业前缀", []):
            if code.startswith(pre):
                return float(lc["科创创业幅度"])
    return float(lc["涨跌停幅度"])


def _oneword_down(high: float, low: float, close: float, prev_close: float,
                  code: str | None, cfg: dict) -> bool:
    """当日是否「跌停一字」(零振幅 + 跌停) → 卖方无法成交。prev_close≤0 视为不可判→False。"""
    if prev_close is None or prev_close <= 0:
        return False
    if abs(high - low) > 1e-9:                          # 有振幅 → 非一字
        return False
    chg = close / prev_close - 1.0
    return chg <= -(_limit_frac(code, cfg) - 1e-9)


# ———————————————————— 离场状态机 ————————————————————
def _exit_signal(arrays: dict, j: int, entry_idx: int, p0: float,
                 cfg: dict) -> tuple[int, str, float] | None:
    """判持有第 (j-entry_idx) 交易日是否命中离场;命中返回 (规则号, 规则名, 成交价),否则 None。

    按优先级 1→5 顺序,先命中先返回(同日多条命中取靠前者)。只用 j 及之前的量。
    """
    close, open_, high, low, vol = (arrays["close"], arrays["open"],
                                    arrays["high"], arrays["low"], arrays["vol"])
    ex = cfg["离场"]
    held = j - entry_idx                                # 持有交易日数(进场次日=1)

    # 1 硬止损:盘中触及 P0×系数 → 以该价成交
    stop_px = p0 * float(ex["硬止损系数"])
    if low[j] <= stop_px:
        return (1, "硬止损", round(stop_px, 4))

    # 2 趋势止损:收盘跌破 MA13
    mp = int(ex["趋势MA周期"])
    if j - mp + 1 >= 0:
        ma = float(close[j - mp + 1: j + 1].mean())
        if close[j] < ma:
            return (2, "趋势止损", round(float(close[j]), 4))

    # 3 加速止盈:C/C₍ₜ₋₃₎−1 ≥ 阈值
    look = int(ex["加速止盈回看"])
    if j - look >= 0 and close[j - look] > 0:
        if close[j] / close[j - look] - 1.0 >= float(ex["加速止盈阈值"]):
            return (3, "加速止盈", round(float(close[j]), 4))

    # 4 放量滞涨:放量 + 涨幅递减
    if j - 2 >= 0 and vol[j - 1] > 0 and close[j - 1] > 0 and close[j - 2] > 0:
        vol_spike = vol[j] > vol[j - 1] * float(ex["放量倍数"])
        chg_today = close[j] / close[j - 1] - 1.0
        chg_prev = close[j - 1] / close[j - 2] - 1.0
        if vol_spike and chg_today < chg_prev:
            return (4, "放量滞涨", round(float(close[j]), 4))

    # 5 时间成本:持股 > N 且 收益 < 阈值 → 第 (N+1) 日收盘强制离场
    if held > int(ex["最大持有交易日"]) and (close[j] / p0 - 1.0) < float(ex["时间成本收益阈值"]):
        return (5, "时间成本", round(float(close[j]), 4))

    return None


def simulate_position(kdf, entry_idx: int, cfg: dict | None = None,
                      code: str | None = None) -> dict:
    """从 entry_idx(信号日)建仓,逐日跑离场状态机,返回一笔交易 dict。

    Returns dict 含:进场日/进场价P0/离场日/离场价/离场规则/离场规则号/持有天数/收益/
                    状态/一字板顺延/顺延天数。未离场(数据不足或一字板未解)→ 收益 None。
    """
    cfg = cfg or _ALL
    n = len(kdf)
    dates = kdf["date"].astype(str).tolist()
    arrays = {
        "close": kdf["close"].to_numpy(dtype=float),
        "open": kdf["open"].to_numpy(dtype=float),
        "high": kdf["high"].to_numpy(dtype=float),
        "low": kdf["low"].to_numpy(dtype=float),
        "vol": kdf["volume"].to_numpy(dtype=float),
    }
    p0 = float(arrays["close"][entry_idx])
    trade = {
        "进场日": dates[entry_idx], "进场价P0": round(p0, 4),
        "离场日": None, "离场价": None, "离场规则": None, "离场规则号": None,
        "持有天数": None, "收益": None,
        "状态": "持有中(数据不足)", "一字板顺延": False, "顺延天数": 0,
    }
    deferred = 0
    for j in range(entry_idx + 1, n):
        sig = _exit_signal(arrays, j, entry_idx, p0, cfg)
        if sig is None:
            continue
        prev_close = arrays["close"][j - 1]
        if _oneword_down(arrays["high"][j], arrays["low"][j], arrays["close"][j],
                         prev_close, code, cfg):
            deferred += 1                                # 想卖但跌停一字卖不出 → 顺延
            continue
        rule_no, rule_name, px = sig
        trade.update({
            "离场日": dates[j], "离场价": px, "离场规则": rule_name, "离场规则号": rule_no,
            "持有天数": j - entry_idx, "收益": round(px / p0 - 1.0, 6),
            "状态": "已离场", "一字板顺延": deferred > 0, "顺延天数": deferred,
        })
        return trade
    trade["一字板顺延"] = deferred > 0
    trade["顺延天数"] = deferred
    return trade


# ———————————————————— 信号扫描 + 单票回测 ————————————————————
def find_signals(kdf, cfg: dict | None = None) -> list[int]:
    """扫 kdf 全历史,返回所有命中 SELECT 的整数索引 t(升序)。历史不足自动跳过。"""
    n = len(kdf)
    out = []
    for t in range(screen_s01.min_history() - 1, n):
        if screen_s01.signal_at(kdf, t).get("SELECT"):
            out.append(t)
    return out


def _bench_ret(bench, entry_date: str, exit_date: str) -> float | None:
    """沪深300 在 [进场日, 离场日] 的同持有期收益(按日期对齐,取该日或其后首个交易日)。"""
    if bench is None or len(bench) == 0 or "close" not in bench.columns:
        return None
    import pandas as pd

    b = bench.copy()
    b["date"] = pd.to_datetime(b["date"])
    b = b.sort_values("date").reset_index(drop=True)
    bd = b["date"].tolist()
    bp = b["close"].astype(float).tolist()

    def _idx(d):
        t0 = pd.to_datetime(d)
        for i, x in enumerate(bd):
            if x >= t0:
                return i
        return None

    ie, ix = _idx(entry_date), _idx(exit_date)
    if ie is None or ix is None or bp[ie] <= 0:
        return None
    return round(bp[ix] / bp[ie] - 1.0, 6)


def backtest_one(kdf, cfg: dict | None = None, code: str | None = None,
                 bench=None) -> list[dict]:
    """单票:找所有信号 → 逐个建仓跑状态机 → 每笔补 benchmark 同持有期收益 + Alpha。"""
    cfg = cfg or _ALL
    trades = []
    for t in find_signals(kdf, cfg):
        tr = simulate_position(kdf, t, cfg, code=code)
        tr["code"] = code
        if tr["状态"] == "已离场" and bench is not None:
            br = _bench_ret(bench, tr["进场日"], tr["离场日"])
            tr["基准收益"] = br
            tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
        trades.append(tr)
    return trades


# ———————————————————— 汇总(胜率/中位/盈亏比/最大回撤/持有天数/Alpha)————————————————————
def _max_drawdown(closed: list[dict]) -> float | None:
    """按离场日排序把每笔收益复利成净值曲线,取峰谷最大回撤(返回正值幅度)。"""
    if not closed:
        return None
    seq = sorted(closed, key=lambda t: t["离场日"])
    equity = 1.0
    peak = 1.0
    maxdd = 0.0
    for t in seq:
        equity *= (1.0 + t["收益"])
        peak = max(peak, equity)
        maxdd = min(maxdd, equity / peak - 1.0)
    return round(abs(maxdd), 6)


def summarize_trades(trades: list[dict], min_sample: int = 10) -> dict:
    """把多笔交易汇成策略级指标。只统计「已离场」交易;未离场单独计数。"""
    closed = [t for t in trades if t["状态"] == "已离场"]
    rets = [t["收益"] for t in closed]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    alphas = [t["Alpha"] for t in closed if t.get("Alpha") is not None]

    avg_win = statistics.mean(wins) if wins else None
    avg_loss = statistics.mean(losses) if losses else None
    if avg_win is not None and avg_loss not in (None, 0):
        pnl_ratio = round(avg_win / abs(avg_loss), 4)
    else:
        pnl_ratio = None                                 # 无亏损样本(或无盈利样本)→ 不可计,诚实置空

    n = len(rets)
    return {
        "交易数": len(trades),
        "已离场数": n,
        "未离场数(持有中)": sum(1 for t in trades if t["状态"] != "已离场"),
        "一字板顺延笔数": sum(1 for t in trades if t.get("一字板顺延")),
        "胜率": round(sum(1 for r in rets if r > 0) / n, 6) if n else None,
        "中位收益": round(statistics.median(rets), 6) if rets else None,
        "平均收益": round(statistics.mean(rets), 6) if rets else None,
        "盈亏比": pnl_ratio,
        "最大回撤": _max_drawdown(closed),
        "平均持有天数": round(statistics.mean([t["持有天数"] for t in closed]), 4) if closed else None,
        "平均Alpha(同持有期vs沪深300)": round(statistics.mean(alphas), 6) if alphas else None,
        "离场规则分布": _rule_dist(closed),
        "状态": _state(n, min_sample),
    }


def _rule_dist(closed: list[dict]) -> dict:
    d: dict[str, int] = {}
    for t in closed:
        d[t["离场规则"]] = d.get(t["离场规则"], 0) + 1
    return d


def _state(n: int, min_sample: int) -> str:
    if n == 0:
        return "无已离场交易(信号少/前瞻未到期)→ 待积累,属正常(样本小)"
    if n < min_sample:
        return f"样本少(N={n}),统计力弱,待积累"
    return f"可用(N={n})"


# ———————————————————— 回测汇总报告接线(模块③)————————————————————
_MIN_SAMPLE = 10
_VIEW = "趋势深跌反包回测"                                # → data/analysis/<date>/趋势深跌反包回测.json


def _load_bench(fetch: bool):
    """沪深300 基准 K线(只读缓存,fetch=True 缺则采集)。取不到→None(Alpha 不计,诚实标注)。"""
    from tools.collectors import index
    try:
        return index.load_index(_BENCH)
    except FileNotFoundError:
        try:
            return index.fetch_index(["沪深300"]).get(_BENCH) if fetch else None
        except Exception:  # noqa: BLE001 采集失败不阻塞回测主流程
            return None


def summarize(codes: list[str] | None = None, fetch: bool = False,
              min_sample: int = _MIN_SAMPLE, generated_at: str | None = None) -> dict:
    """跨票跑 S01 持仓回测并汇总(纯计算,不落库)。

    codes=None → 用本地所有滚动主档(store.list_master_codes)。缺 K线的票诚实跳过。
    数据不足(无主档 / 无信号 / 前瞻未到期)时优雅标注,不报错、不编造。
    """
    from tools.collectors import market

    codes = codes if codes is not None else store.list_master_codes()
    bench = _load_bench(fetch)
    all_trades: list[dict] = []
    scanned = skipped = signal_codes = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < screen_s01.min_history():
            skipped += 1
            continue
        scanned += 1
        trades = backtest_one(kdf, code=code, bench=bench)
        if trades:
            signal_codes += 1
        all_trades.extend(trades)

    summary = summarize_trades(all_trades, min_sample=min_sample)
    result = {
        "策略": "趋势深跌反包(S01)",
        "扫描票数": len(codes), "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped, "出信号票数": signal_codes,
        "有基准": bench is not None and len(bench) > 0,
        "汇总": summary,
        "口径": ("每个历史信号日建仓(P0=信号日收盘)→ 逐日 5 条离场状态机撮合 → "
                 "胜率/中位收益/盈亏比/最大回撤/平均持有天数 + 同持有期相对沪深300 Alpha;"
                 "防未来函数(H52不含当日、离场只用当日及之前);一字板不可成交顺延标注"),
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
    """算汇总并落 view「趋势深跌反包回测」(当前运行日期)。no_view=True 只算不落。"""
    result = summarize(codes=codes, fetch=fetch, min_sample=min_sample,
                       generated_at=generated_at)
    if not no_view:
        store.put_view(_VIEW, result)
    return result


# ———————————————————— CLI ————————————————————
def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S01 趋势深跌反包 持仓回测汇总")
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
        codes = None                                     # 默认:本地所有主档
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
