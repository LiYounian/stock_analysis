"""回踩低吸统一入场框架 · 三组同口径 A/B/C 对比回测。

需求源自 docs/计划/回踩低吸统一入场框架_设计与预注册.md §4/§5:
  (A) 新框架「回踩缩量进场」 —— screen_pullback.find_signals_pullback(突破→观察→回踩缩量)
  (B) 箱体 v2「突破日进场」   —— backtest_box.find_signals_box(use_v2=True)
  (C) S02 原版               —— backtest_s02.find_signals_s02

**同口径**(三组唯一差异=入场信号来源):
  · 进场:各组信号日 t 判定 → **t+1 开盘**进场(无未来函数;t+1 一字板/越界 → 无法成交剔除);
  · 离场:一律复用 S01 持仓回测器 5 条离场状态机(`position_backtest.simulate_position`,未改一行);
  · 基准:同持有期相对沪深300 的 Alpha。
→ 因此可直接横比 **即死率(硬止损占比) + 平均/中位收益 + Alpha + 显著性(聚类 t)**。

显著性用**按个股聚类的稳健 t**(cluster-robust,聚类=股票代码)——同一票多笔重叠交易相关,
朴素 t 会高估显著性;聚类 t 用组内残差和的平方修正,更诚实。分别对「收益」与「Alpha」计。

防未来函数红线:各组信号 screener 只用 ≤ 各自判定日(突破按 b、回踩按 t、S02 当周整周剔除、
箱体不含末根);进场严格 t+1;离场每日决策只用当日及之前。数据只读复用 collectors.market。
入口:`python -m tools.backtest.backtest_pullback [--sample N|--codes ...|--universe N] [--fetch] [--no-view]`。
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict

from tools.backtest import position_backtest as pb
from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_pullback
from tools.store import repo as store

logger = logging.getLogger("backtest.pullback")

_VIEW = "回踩低吸框架回测"
_MIN_SAMPLE = 10


# ———————————————————— 进场口径(t+1 开盘)————————————————————
def _entry(kdf, t: int, entry: str = "次日开盘"):
    """按进场口径返回 (entry_idx, entry_price) 或 None(不可成交)。

    「次日开盘」:(t+1, open[t+1])——信号日收盘确认后次日开盘竞价买入(无未来函数);
      t+1 越界 或 t+1 一字板(high==low,开盘即锁死买不到)→ None(计"无法成交")。
    「当日收盘」:(t, None)→ P0=close[t](旧基线口径)。
    """
    if entry == "当日收盘":
        return t, None
    n = len(kdf)
    if t + 1 >= n:
        return None
    if float(kdf["high"].iloc[t + 1]) == float(kdf["low"].iloc[t + 1]):
        return None
    return t + 1, float(kdf["open"].iloc[t + 1])


# ———————————————————— 三组信号源(统一签名:kdf → list[int] 信号日索引)————————————————————
def signals_pullback(kdf) -> list[int]:
    """A 组:回踩低吸框架进场信号日(回踩缩量日 t)。"""
    return [s["t"] for s in screen_pullback.find_signals_pullback(kdf)]


def signals_box_v2(kdf) -> list[int]:
    """B 组:箱体 v2 突破日。"""
    from tools.backtest import backtest_box
    return backtest_box.find_signals_box(kdf, use_v2=True)


def signals_s02(kdf) -> list[int]:
    """C 组:S02 原版信号日。"""
    from tools.backtest import backtest_s02
    return backtest_s02.find_signals_s02(kdf)


_GROUPS = {
    "A_回踩缩量进场": (signals_pullback, screen_pullback.min_history),
    "B_箱体v2突破日进场": (signals_box_v2, None),
    "C_S02原版": (signals_s02, None),
}


def _need_bars(min_hist_fn) -> int:
    if min_hist_fn is None:
        return 200                                       # 兜底:趋势门/H52 等最重历史需求量级
    try:
        return int(min_hist_fn())
    except Exception:
        return 200


# ———————————————————— 单组回测 ————————————————————
def _backtest_group(kdfs: dict, bench, find_fn, entry: str) -> tuple[list[dict], int, int]:
    """对预加载 {code: kdf} 跑一组:找信号 → t+1 进场 → S01 离场 → 补 Alpha。

    返回 (交易列表, 出信号票数, 无法成交笔数)。
    """
    all_trades: list[dict] = []
    signal_codes = unfilled = 0
    for code, kdf in kdfs.items():
        got = False
        for t in find_fn(kdf):
            ent = _entry(kdf, t, entry)
            if ent is None:
                unfilled += 1
                continue
            eidx, eprice = ent
            tr = pb.simulate_position(kdf, eidx, code=code, entry_price=eprice)
            tr["code"] = code
            tr["信号日"] = str(kdf["date"].iloc[t])[:10]
            if tr["状态"] == "已离场" and bench is not None:
                br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
                tr["基准收益"] = br
                tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
            all_trades.append(tr)
            got = True
        if got:
            signal_codes += 1
    return all_trades, signal_codes, unfilled


# ———————————————————— 显著性:按个股聚类的稳健 t ————————————————————
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clustered_t(values: list[float], codes: list[str]) -> dict:
    """H0: 均值=0 的**按 code 聚类稳健 t**。cluster-robust SE = √[ Σ_g (Σ_{i∈g} resid_i)² / N² · G/(G−1) ]。

    组内相关(同票重叠交易)会被组内残差和的平方吸收,故比朴素 t 更保守/诚实。
    返回 {N, 簇数, 均值, t, p(近似正态双尾)};样本 < 2 或簇 < 2 → 相应字段 None。
    """
    n = len(values)
    if n < 2:
        return {"N": n, "簇数": len(set(codes)), "均值": None, "t": None, "p": None}
    mean = statistics.mean(values)
    groups: dict[str, float] = defaultdict(float)
    for v, c in zip(values, codes):
        groups[c] += (v - mean)                          # 组内残差和
    G = len(groups)
    meat = sum(gsum * gsum for gsum in groups.values())
    if G >= 2 and meat > 0:
        var = meat / (n * n) * (G / (G - 1))
        se = math.sqrt(var)
        mode = "聚类"
    else:                                                # 退化:单簇/零肉 → 朴素 t 兜底
        sd = statistics.pstdev(values) * math.sqrt(n / (n - 1))
        se = sd / math.sqrt(n) if sd > 0 else 0.0
        mode = "朴素(簇不足)"
    if se <= 0:
        return {"N": n, "簇数": G, "均值": round(mean, 6), "t": None, "p": None, "口径": mode}
    t = mean / se
    return {"N": n, "簇数": G, "均值": round(mean, 6), "t": round(t, 4),
            "p": round(2 * (1 - _norm_cdf(abs(t))), 4), "口径": mode}


def _summ_group(trades: list[dict], signal_codes: int, unfilled: int,
                min_sample: int) -> dict:
    """单组汇总:复用 summarize_trades + 即死率 + 收益/Alpha 聚类 t。"""
    s = pb.summarize_trades(trades, min_sample=min_sample)
    dist = s.get("离场规则分布", {}) or {}
    nexit = s.get("已离场数", 0) or 0
    s["即死率(硬止损占比)"] = round(dist.get("硬止损", 0) / nexit, 6) if nexit else None
    s["出信号票数"] = signal_codes
    s["无法成交笔数(次日一字/越界)"] = unfilled
    closed = [t for t in trades if t["状态"] == "已离场"]
    s["收益_聚类t"] = _clustered_t([t["收益"] for t in closed],
                                    [t.get("code", "?") for t in closed])
    alpha_closed = [t for t in closed if t.get("Alpha") is not None]
    s["Alpha_聚类t"] = _clustered_t([t["Alpha"] for t in alpha_closed],
                                     [t.get("code", "?") for t in alpha_closed])
    return s


# ———————————————————— 数据加载 + 顶层 A/B/C ————————————————————
def _load_kdfs(codes: list[str], fetch: bool, need: int) -> dict:
    from tools.collectors import market
    kdfs: dict = {}
    skipped = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        kdfs[code] = kdf
    return kdfs, skipped


def run_abc(codes: list[str] | None = None, fetch: bool = False,
            entry: str = "次日开盘", min_sample: int = _MIN_SAMPLE,
            generated_at: str | None = None, bench=None) -> dict:
    """三组 A/B/C 同口径回测汇总(纯计算,不落库)。缺 K线的票诚实跳过。

    bench:沪深300 基准 DataFrame 覆盖(缺省走 pb._load_bench)。深历史回测须传全历史 HS300,
    否则本地 index 分区只覆盖近一年 → 早年交易 Alpha 失真(基准被当作近乎持平),务必显式注入。
    """
    codes = codes if codes is not None else store.list_master_codes()
    if bench is None:
        bench = pb._load_bench(fetch)
    # 用最大历史需求统一加载一次(200 兜底覆盖 B 的趋势门 MA200 / C 的完整周),各组共用同一票池。
    need = max(_need_bars(fn) for _, fn in _GROUPS.values())
    kdfs, skipped = _load_kdfs(codes, fetch, need)

    groups: dict[str, dict] = {}
    for name, (find_fn, _mh) in _GROUPS.items():
        trades, sig_codes, unfilled = _backtest_group(kdfs, bench, find_fn, entry)
        groups[name] = _summ_group(trades, sig_codes, unfilled, min_sample)
        logger.info("%s:交易 %d / 已离场 %d / 即死率 %s / 平均Alpha %s",
                    name, groups[name]["交易数"], groups[name]["已离场数"],
                    groups[name]["即死率(硬止损占比)"],
                    groups[name].get("平均Alpha(同持有期vs沪深300)"))

    result = {
        "框架": "回踩低吸统一入场框架 · 三组同口径 A/B/C 对比",
        "进场口径": entry,
        "扫描票数": len(codes), "有效样本票": len(kdfs),
        "跳过票数(历史不足/无K线)": skipped,
        "有基准": bench is not None and len(bench) > 0,
        "组": groups,
        "口径": ("三组唯一差异=入场信号源;进场统一 t+1 开盘、离场统一 S01 持仓回测器 5 条状态机"
                 "(参数读 THRESHOLDS['趋势深跌反包'],未改一行);同持有期相对沪深300 Alpha;"
                 "即死率=硬止损离场数/已离场数;显著性=按个股聚类稳健 t(收益/Alpha 各一);"
                 "防未来函数(突破按 b/回踩按 t/S02 当周整周剔除/箱体不含末根;进场 t+1;离场只用当日及之前)"),
        "预注册对照": ("§4:A 组即死率应显著 << B 组(v2 突破日进场,预注册 67%),目标 <40%;"
                       "A 组 Alpha ≥ B 组且为正。达不到如实报。"),
        "免责声明": "历史回测证据,非投资建议;样本随主档积累与信号出现而增长,统计力逐步增强。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算(--fetch 采集后可得)"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_and_store(codes: list[str] | None = None, fetch: bool = False,
                  no_view: bool = False, entry: str = "次日开盘",
                  min_sample: int = _MIN_SAMPLE,
                  generated_at: str | None = None, bench=None) -> dict:
    """算 A/B/C 汇总并落 view「回踩低吸框架回测」。no_view=True 只算不落。"""
    result = run_abc(codes=codes, fetch=fetch, entry=entry,
                     min_sample=min_sample, generated_at=generated_at, bench=bench)
    if not no_view:
        store.put_view(_VIEW, result)
    return result


def _brief(result: dict) -> dict:
    """打印用精简:每组关键指标一行。"""
    out = {}
    for name, s in result["组"].items():
        out[name] = {
            "交易数": s["交易数"], "已离场数": s["已离场数"],
            "出信号票数": s["出信号票数"],
            "无法成交": s["无法成交笔数(次日一字/越界)"],
            "即死率": s["即死率(硬止损占比)"],
            "胜率": s["胜率"], "平均收益": s["平均收益"], "中位收益": s["中位收益"],
            "盈亏比": s["盈亏比"],
            "平均Alpha": s.get("平均Alpha(同持有期vs沪深300)"),
            "Alpha聚类t": s["Alpha_聚类t"].get("t"),
            "Alpha聚类p": s["Alpha_聚类t"].get("p"),
            "簇数": s["Alpha_聚类t"].get("簇数"),
            "收益聚类t": s["收益_聚类t"].get("t"),
        }
    return out


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="回踩低吸框架 A/B/C 三组同口径回测")
    ap.add_argument("--codes", help="逗号分隔代码")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只")
    ap.add_argument("--sample", type=int, metavar="N", help="从主档随机抽 N 只(破偏差)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--no-view", action="store_true", help="只算不落库(打印)")
    ap.add_argument("--entry", choices=["次日开盘", "当日收盘"], default="次日开盘")
    ap.add_argument("--min-sample", type=int, default=_MIN_SAMPLE)
    ap.add_argument("--bench-file", help="沪深300 基准 parquet 路径(深历史须传全历史 HS300;缺省走本地 index 分区)")
    a = ap.parse_args(argv)

    bench = None
    if a.bench_file:
        import pandas as pd
        bench = pd.read_parquet(a.bench_file)
        logger.info("基准注入:%s(%d 根)", a.bench_file, len(bench))

    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.universe:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    elif a.sample:
        import random
        allc = sorted(store.list_master_codes())
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    else:
        codes = None
    stamp = _dt.datetime.now().isoformat(timespec="seconds")
    r = run_and_store(codes=codes, fetch=a.fetch, no_view=a.no_view,
                      entry=a.entry, min_sample=a.min_sample, generated_at=stamp,
                      bench=bench)
    print(json.dumps({"有效样本票": r["有效样本票"], "进场口径": r["进场口径"],
                      "组": _brief(r)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
