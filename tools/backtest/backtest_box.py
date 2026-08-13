"""策略 3「箱体形态」持仓回测(信号日收盘机械买入基线)。

⚠️ 口径:与 `backtest_s02.py` **完全同口径**——复用 S01 持仓回测器
(`position_backtest.simulate_position`:信号日收盘 P0 建仓 + 5 条离场状态机 + 一字板顺延),
离场规则/参数读 `THRESHOLDS["趋势深跌反包"]`(未改一行离场逻辑)。因此它衡量的是
**箱体放量突破信号的原始质量**——「信号日收盘机械买入、S01 离场」这条基线,**不代表最终买法**。
与 S01/S02 同口径 → 三者可直接横比(胜率/盈亏比/Alpha/即死率)。

信号源 = 策略3 `screen_box.signal_at`(窄幅箱体 AND 突破箱顶 AND 放量,单日触发)。

防未来函数:screen_box 只用 t 及之前(detect_box 截 [0,t] 识别、不含末根之后);离场每日
决策沿用 S01 状态机(只用当日及之前)。数据只读复用 collectors.market.load_kline。
入口:`python -m tools.backtest.backtest_box [--codes ...|--universe N] [--sample N] [--fetch] [--no-view]`。
"""
from __future__ import annotations

import logging

from tools.backtest import position_backtest as pb
from tools.pipeline import screen_box
from tools.store import repo as store

logger = logging.getLogger("backtest.position_box")

_VIEW = "箱体形态回测"
_MIN_SAMPLE = 10


def find_signals_box(kdf, use_v2: bool = True) -> list[int]:
    """扫 kdf 全历史,返回所有命中箱体 SELECT 的整数索引 t(升序)。历史不足自动跳过。"""
    n = len(kdf)
    start = max(screen_box.min_history(use_v2) - 1, 1)
    return [t for t in range(start, n) if screen_box.signal_at(kdf, t, use_v2=use_v2).get("SELECT")]


def _entry(kdf, t: int, entry: str):
    """按进场口径返回 (entry_idx, entry_price) 或 None(不可成交)。

    「当日收盘」:(t, None)→ P0=close[t](旧基线,与 S01/S02 可比);
    「次日开盘」:(t+1, open[t+1])→ 突破日收盘确认后次日开盘竞价买入(无未来函数,头条口径);
      t+1 越界 或 t+1 一字板(high==low,开盘即锁死买不到)→ None(计"无法成交")。
    """
    if entry == "当日收盘":
        return t, None
    n = len(kdf)
    if t + 1 >= n:
        return None
    if float(kdf["high"].iloc[t + 1]) == float(kdf["low"].iloc[t + 1]):
        return None                                         # 一字板,竞价买不到 → 跳过
    return t + 1, float(kdf["open"].iloc[t + 1])


def backtest_one_box(kdf, code: str | None = None, bench=None,
                     use_v2: bool = True, entry: str = "次日开盘") -> tuple[list[dict], int]:
    """单票:找所有箱体信号 → 按进场口径建仓 → S01 离场 → 每笔补基准同持有期收益 + Alpha。

    返回 (交易列表, 无法成交笔数)。进场价通过 simulate_position(entry_price=...) 注入,离场逻辑未改。
    """
    trades, unfilled = [], 0
    for t in find_signals_box(kdf, use_v2=use_v2):
        ent = _entry(kdf, t, entry)
        if ent is None:
            unfilled += 1
            continue
        eidx, eprice = ent
        tr = pb.simulate_position(kdf, eidx, code=code, entry_price=eprice)  # S01 离场状态机(未改)
        tr["code"] = code
        tr["信号日"] = str(kdf["date"].iloc[t])[:10]
        tr["进场口径"] = entry
        if tr["状态"] == "已离场" and bench is not None:
            br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
            tr["基准收益"] = br
            tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
        trades.append(tr)
    return trades, unfilled


def summarize(codes: list[str] | None = None, fetch: bool = False,
              min_sample: int = _MIN_SAMPLE, generated_at: str | None = None,
              use_v2: bool = True, entry: str = "次日开盘") -> dict:
    """跨票**单进程串行**跑箱体持仓回测并汇总(纯计算,不落库)。缺 K线的票诚实跳过。

    use_v2:v2 提供者规格 vs v1 旧箱体;entry:进场口径(次日开盘/当日收盘)。
    """
    from tools.collectors import market

    codes = codes if codes is not None else store.list_master_codes()
    bench = pb._load_bench(fetch)
    need = screen_box.min_history(use_v2)
    all_trades: list[dict] = []
    scanned = skipped = signal_codes = unfilled = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        trades, uf = backtest_one_box(kdf, code=code, bench=bench, use_v2=use_v2, entry=entry)
        unfilled += uf
        if trades:
            signal_codes += 1
        all_trades.extend(trades)

    summary = pb.summarize_trades(all_trades, min_sample=min_sample)
    # 即死率(硬止损占比)= 硬止损离场数 / 已离场数(与 scan_s01 同口径,治即死61% 的核心指标)
    _dist = summary.get("离场规则分布", {}) or {}
    _nexit = summary.get("已离场数", 0) or 0
    summary["即死率(硬止损占比)"] = round(_dist.get("硬止损", 0) / _nexit, 6) if _nexit else None
    entry_desc = ("P0=信号日收盘(与 S01/S02 同口径可比)" if entry == "当日收盘"
                  else "P0=突破日次日开盘(收盘确认突破→次日竞价买;无未来函数;一字板不可成交已剔)")
    result = {
        "策略": "箱体形态(策略3)", "版本": "v2" if use_v2 else "v1", "进场口径": entry,
        "扫描票数": len(codes), "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped, "出信号票数": signal_codes,
        "无法成交笔数(次日一字/越界)": unfilled,
        "有基准": bench is not None and len(bench) > 0,
        "汇总": summary,
        "口径": (f"进场:{entry_desc};离场:复用 S01 持仓回测器 5 条离场状态机(参数读 "
                 "THRESHOLDS['趋势深跌反包'],未改一行)→ 衡量箱体突破信号质量;防未来函数(识别/趋势门"
                 "截至当日、离场只用当日及之前);一字板不可成交顺延标注"),
        "免责声明": "历史回测证据,非投资建议;样本随主档积累与信号出现而增长,统计力逐步增强。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算(--fetch 采集后可得)"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_ab(codes: list[str] | None = None, fetch: bool = False,
           min_sample: int = _MIN_SAMPLE, generated_at: str | None = None) -> dict:
    """A/B 对比:{v1,v2} × {当日收盘, 次日开盘} 四组汇总,便于看 v2/趋势门是否降即死、提 Alpha。"""
    combos = [(False, "当日收盘"), (False, "次日开盘"), (True, "当日收盘"), (True, "次日开盘")]
    out = {}
    for v2, entry in combos:
        key = f"{'v2' if v2 else 'v1'}·{entry}"
        out[key] = summarize(codes=codes, fetch=fetch, min_sample=min_sample,
                             generated_at=generated_at, use_v2=v2, entry=entry)
    return out


def run_and_store(codes: list[str] | None = None, fetch: bool = False,
                  no_view: bool = False, min_sample: int = _MIN_SAMPLE,
                  generated_at: str | None = None, use_v2: bool = True,
                  entry: str = "次日开盘") -> dict:
    """算汇总并落 view「箱体形态回测」(当前运行日期)。no_view=True 只算不落。"""
    result = summarize(codes=codes, fetch=fetch, min_sample=min_sample,
                       generated_at=generated_at, use_v2=use_v2, entry=entry)
    if not no_view:
        store.put_view(_VIEW, result)
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 3 箱体形态 持仓回测汇总(单进程串行)")
    ap.add_argument("--codes", help="逗号分隔代码(默认=本地所有滚动主档)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(--codes 优先)")
    ap.add_argument("--sample", type=int, metavar="N", help="从主档随机抽 N 只(破偏差,--codes/--universe 优先)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--no-view", action="store_true", help="只算不落库(打印汇总)")
    ap.add_argument("--min-sample", type=int, default=_MIN_SAMPLE, help="统计力阈值")
    ap.add_argument("--v1", action="store_true", help="用旧 detect_box(默认 v2 提供者规格)")
    ap.add_argument("--entry", choices=["次日开盘", "当日收盘"], default="次日开盘",
                    help="进场口径(默认次日开盘=头条口径)")
    ap.add_argument("--ab", action="store_true", help="A/B:{v1,v2}×{当日收盘,次日开盘} 四组对比(只算不落)")
    a = ap.parse_args(argv)

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
    if a.ab:
        ab = run_ab(codes=codes, fetch=a.fetch, min_sample=a.min_sample, generated_at=stamp)
        brief = {k: {"出信号票数": v["出信号票数"], "无法成交": v["无法成交笔数(次日一字/越界)"],
                     "汇总": v["汇总"]} for k, v in ab.items()}
        print(json.dumps(brief, ensure_ascii=False, indent=2))
        return 0
    r = run_and_store(codes=codes, fetch=a.fetch, no_view=a.no_view,
                      min_sample=a.min_sample, generated_at=stamp,
                      use_v2=not a.v1, entry=a.entry)
    logger.info("扫描 %d / 有效 %d / 出信号 %d;汇总:%s",
                r["扫描票数"], r["有效样本票"], r["出信号票数"], r["汇总"]["状态"])
    print(json.dumps({"版本": r["版本"], "进场口径": r["进场口径"], "扫描票数": r["扫描票数"],
                      "有效样本票": r["有效样本票"], "出信号票数": r["出信号票数"],
                      "无法成交笔数": r["无法成交笔数(次日一字/越界)"], "汇总": r["汇总"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
