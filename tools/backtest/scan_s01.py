"""S01「趋势深跌反包」参数敏感性 + 入场确认**扫描驱动**(只编排,不改回测器算法)。

目的:对入场/离场关键参数做网格,每组跑全A回测,记 即死率(硬止损占比)/胜率/平均收益/
中位/盈亏比/Alpha/样本/显著性;并对比「无确认 vs T+1 确认」入场。

为什么单独写(不复用 run_s01_backtest):
  · run_s01_backtest 经 position_backtest.find_signals,后者调 screen_s01.signal_at 时**不转发 cfg**,
    故入场参数(深跌阈值等)覆盖不会生效。本驱动**显式把 cfg 转发到入场判定**,并支持可选入场确认。
  · 预加载全A kdf 到内存一次,之后逐组合在内存里跑(避免每组合重复读 parquet)。

复用(均不改其算法):
  · screen_s01.signal_at(kdf, t, cfg)      —— 入场 C1..C4(cfg 可覆盖深跌阈值等)
  · screen_s01.confirm_entry(kdf, t, mode) —— 可选入场确认(T+1 不破低)
  · position_backtest.simulate_position    —— 5 条离场状态机(原样)
  · position_backtest.summarize_trades     —— 汇总(原样)

入口:`python -m tools.backtest.scan_s01 [--date D] [--limit N] [--out PATH]`
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import statistics
import time
from pathlib import Path

from tools.backtest import position_backtest as pb
from tools.backtest import run_s01_backtest as drv
from tools.pipeline import screen_s01

logger = logging.getLogger("backtest.scan_s01")


def _make_cfg(hard_stop: float | None = None, drop_thr: float | None = None,
              accel_thr: float | None = None, trend_ma: int | None = None) -> dict:
    """从 S01 默认 THRESHOLDS 深拷贝一份,按需覆盖入场/离场关键参数,返回可传入回测的 cfg。"""
    cfg = copy.deepcopy(pb._ALL)
    if hard_stop is not None:
        cfg["离场"]["硬止损系数"] = float(hard_stop)
    if drop_thr is not None:
        cfg["入场"]["深跌阈值"] = float(drop_thr)
    if accel_thr is not None:
        cfg["离场"]["加速止盈阈值"] = float(accel_thr)
    if trend_ma is not None:
        cfg["离场"]["趋势MA周期"] = int(trend_ma)
    return cfg


def load_all(date: str | None = None, limit: int | None = None) -> tuple[str, dict, object]:
    """预加载:返回 (数据日期, {code: kdf}, bench)。只保留历史 ≥ 最少历史根数的票。"""
    d = drv.resolve_data_date(date)
    codes = drv.list_codes(d, limit)
    bench = drv.load_bench(d)
    need = screen_s01.min_history()
    kdfs: dict[str, object] = {}
    from tools.store import repo as store
    for code in codes:
        try:
            kdf = store.get_raw("kline", code, date=d)
        except FileNotFoundError:
            continue
        if kdf is None or len(kdf) < need:
            continue
        kdfs[code] = kdf
    logger.info("预加载完成:数据日期=%s 有效票=%d 基准=%s",
                d, len(kdfs), "有" if bench is not None else "无")
    return d, kdfs, bench


def _find_signals_cfg(kdf, cfg: dict) -> list[int]:
    """扫全历史,返回命中 SELECT 的索引(**转发 cfg 到入场判定**,与 find_signals 的区别所在)。"""
    n = len(kdf)
    entry_cfg = cfg["入场"]                              # signal_at 读的是「入场」子字典
    need = int(entry_cfg["最少历史根数"])
    out = []
    for t in range(need - 1, n):
        if screen_s01.signal_at(kdf, t, entry_cfg).get("SELECT"):
            out.append(t)
    return out


def run_combo(kdfs: dict, bench, cfg: dict, confirm: str | None = None) -> dict:
    """对预加载的全A跑一组参数;confirm=入场确认 mode(None=无确认)。返回带即死率/显著性的汇总。"""
    all_trades: list[dict] = []
    signal_codes = 0
    for code, kdf in kdfs.items():
        sigs = _find_signals_cfg(kdf, cfg)
        got = False
        for t in sigs:
            entry = screen_s01.confirm_entry(kdf, t, confirm)
            if entry is None:                          # 确认未通过 → 放弃该信号
                continue
            tr = pb.simulate_position(kdf, entry, cfg, code=code)
            tr["code"] = code
            if tr["状态"] == "已离场" and bench is not None:
                br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
                tr["基准收益"] = br
                tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
            all_trades.append(tr)
            got = True
        if got:
            signal_codes += 1

    summary = pb.summarize_trades(all_trades, min_sample=pb._MIN_SAMPLE)
    closed = [t for t in all_trades if t["状态"] == "已离场"]
    rets = [t["收益"] for t in closed]
    n = len(rets)
    dist = summary.get("离场规则分布", {})
    insta = dist.get("硬止损", 0)
    summary["即死率(硬止损占比)"] = round(insta / n, 6) if n else None
    summary["出信号票数"] = signal_codes
    summary["信号数(建仓前)"] = None                   # 见下方,建仓数=交易数
    # 双尾 t 检验(H0: 均值=0),自算,不依赖 scipy
    if n >= 2:
        mean = statistics.mean(rets)
        sd = statistics.pstdev(rets) * math.sqrt(n / (n - 1)) if n > 1 else 0.0
        se = sd / math.sqrt(n) if sd > 0 else 0.0
        tstat = mean / se if se > 0 else 0.0
        summary["t统计量"] = round(tstat, 4)
        summary["p值(近似,正态双尾)"] = round(2 * (1 - _norm_cdf(abs(tstat))), 4)
    else:
        summary["t统计量"] = None
        summary["p值(近似,正态双尾)"] = None
    return summary


def _norm_cdf(x: float) -> float:
    """标准正态 CDF(erf 实现)。大样本下 t≈z,p 值仅作显著性量级参考。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _row(tag: str, s: dict) -> dict:
    """从汇总提取报告用的一行关键指标。"""
    return {
        "组": tag,
        "样本N": s.get("已离场数"),
        "即死率": s.get("即死率(硬止损占比)"),
        "胜率": s.get("胜率"),
        "平均收益": s.get("平均收益"),
        "中位收益": s.get("中位收益"),
        "盈亏比": s.get("盈亏比"),
        "平均Alpha": s.get("平均Alpha(同持有期vs沪深300)"),
        "平均持有天数": s.get("平均持有天数"),
        "t": s.get("t统计量"),
        "p": s.get("p值(近似,正态双尾)"),
        "出信号票数": s.get("出信号票数"),
    }


def run(date: str | None = None, limit: int | None = None) -> dict:
    """跑完整扫描:A) 单参数敏感性网格;B) 无确认 vs T+1 确认。返回结构化结果(含每组指标行)。"""
    t0 = time.time()
    d, kdfs, bench = load_all(date, limit)

    # 基线(现行默认参数,无确认)
    base_cfg = _make_cfg()
    base = run_combo(kdfs, bench, base_cfg, confirm=None)
    rows = [_row("基线(默认:止损0.97/深跌-0.04/无确认)", base)]

    # A1 硬止损系数扫描(其余默认)
    a_stop = []
    for hs in (0.94, 0.95, 0.96, 0.97, 0.98):
        s = run_combo(kdfs, bench, _make_cfg(hard_stop=hs), confirm=None)
        a_stop.append(_row(f"硬止损={hs}", s))
        logger.info("硬止损=%.2f 完成 (%.1fs)", hs, time.time() - t0)

    # A2 深跌阈值扫描(其余默认)
    a_drop = []
    for dr in (-0.03, -0.04, -0.05, -0.06):
        s = run_combo(kdfs, bench, _make_cfg(drop_thr=dr), confirm=None)
        a_drop.append(_row(f"深跌阈值={dr}", s))
        logger.info("深跌阈值=%.2f 完成 (%.1fs)", dr, time.time() - t0)

    # A3(可选)加速止盈阈值扫描
    a_accel = []
    for ac in (0.15, 0.20, 0.25, 0.30):
        s = run_combo(kdfs, bench, _make_cfg(accel_thr=ac), confirm=None)
        a_accel.append(_row(f"加速止盈阈值={ac}", s))

    # A4(可选)趋势MA周期扫描
    a_ma = []
    for m in (8, 13, 20):
        s = run_combo(kdfs, bench, _make_cfg(trend_ma=m), confirm=None)
        a_ma.append(_row(f"趋势MA={m}", s))

    # B 入场确认:无确认 vs T+1 不破低(默认参数)
    b_noconf = _row("入场:无确认(默认)", base)
    s_t1 = run_combo(kdfs, bench, base_cfg, confirm="t1_nobreak")
    b_t1 = _row("入场:T+1不破低确认", s_t1)

    # 组合探索:T+1 确认 × 放宽硬止损(看能否叠加降即死率+转正)
    combo_rows = []
    for hs in (0.95, 0.96):
        for conf in (None, "t1_nobreak"):
            s = run_combo(kdfs, bench, _make_cfg(hard_stop=hs), confirm=conf)
            combo_rows.append(_row(f"止损={hs}+{'T+1确认' if conf else '无确认'}", s))

    result = {
        "策略": "趋势深跌反包(S01)参数敏感性 + 入场确认扫描",
        "数据日期": d,
        "有效票数": len(kdfs),
        "有基准": bench is not None and len(bench) > 0,
        "基线": _row("基线", base),
        "A1_硬止损系数": a_stop,
        "A2_深跌阈值": a_drop,
        "A3_加速止盈阈值": a_accel,
        "A4_趋势MA周期": a_ma,
        "B_入场确认对比": [b_noconf, b_t1],
        "C_组合(止损×确认)": combo_rows,
        "耗时秒": round(time.time() - t0, 1),
        "口径": ("预加载全A raw kdf → 每组合显式转发 cfg 到入场判定(signal_at)+ 可选入场确认 → "
                 "simulate_position 5 条离场状态机(原样)→ summarize_trades;"
                 "即死率=硬止损离场数/已离场数;p 值为正态近似双尾,仅作显著性量级参考"),
        "免责声明": "历史回测证据,非投资建议;本地历史~1.4年、信号聚簇,有效独立样本远小于交易数,置信度有限。",
    }
    return result


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="S01 参数敏感性 + 入场确认扫描")
    ap.add_argument("--date", help="数据日期分区(默认=最新有 kline 的分区)")
    ap.add_argument("--limit", type=int, help="只跑前 N 只(冒烟/调试)")
    ap.add_argument("--out", help="把完整结果 JSON 写到该路径")
    a = ap.parse_args(argv)

    r = run(date=a.date, limit=a.limit)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("结果写入 %s", a.out)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
