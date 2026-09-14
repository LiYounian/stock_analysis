"""Live capitulation 信号(收盘确定性节点):当日全A广度是否触发"极端底部"(β 择时用)。

## 为什么有这个脚本(2026-09-14)

底部超跌反抽回测(docs/计划/2026-09-14_底部超跌反抽假设回测_报告.md,合入 main afd660a)
坐实 **H1-β**:capitulation 极端底部(广度双极端)之后,超跌票 5 日**绝对**收益 24/24
高于普通普跌日 → 经验 #10「普跌日整体降级」对**真正的 capitulation 极端底部过度保守**,
应给市场 β 敞口。本节点把回测里**已验证、因果、可日更**的 capitulation 判据搬成 live 信号,
供次日选股/市场环境判断读:识别到 capitulation → 放松整体降级、给 β 敞口。

## 最重要的三条纪律(与回测作者交接的复用约束一致)

1. **收盘确定性信号,只门控 t+1**:吃当日收盘 breadth(与回测 t+1 进场同口径),
   产出供**当晚选股 / 次日**读;**绝不拿它做盘中实时门控**(盘中 breadth 会漂移,经验 #17)。
2. **β 择时,不是个股 α**:信号只回答"市场是否 capitulation → 要不要放松整体降级",
   **绝不据此给个股加'超跌选股 α 分'**——回测 H1-α **证不了**(反抽是 β/市场级)。
   下游 SOP 严守这条边界(见经验 #10 例外档、doc-lint 测试)。
3. **防未来函数**:判据 = `build_capitulation_flags` 的 trailing-500 分位(rolling、min_periods
   强制、只回看),因果 by construction;`--date` 历史补跑强制走本地 K线,不拿今天实时价冒充。

## 复用,不重造

- capitulation 判据:直接调回测 `tools.backtest.capitulation.breadth_features.build_capitulation_flags`
  (取 as-of 那行),**不复制阈值逻辑**。
- 广度口径:破位广度(below_ma20_ratio)/ 全A等权(mean_pct)复用**单一真源**
  `breadth._per_stock_indicators` + `breadth.cross_section_stats`(与大盘预测/盘尾 α 记分同源),
  **不自写一份**,避免"全A等权"两个互相漂移的定义。

## trailing 序列:自包含、有界、增量、自愈(不改 live 闭环 breadth 生产代码)

as-of 信号只需 trailing-500 窗口 → 本节点维护**自己的**有界缓存
`data/breadth_series/capitulation_breadth.parquet`(仅留最近 ~KEEP_DAYS 交易日,**绝不全 2018 重扫**):
  · 增量:只算缓存里缺的窗口日(常态=当日 1 天);
  · 自愈:窗口内任何缺口(含空缓存首跑)一律补齐,无 gap;
  · 因果:每日 below_ma20_ratio 的 MA20 在**个股全史**上算(_per_stock_indicators),再筛到窗口日
    → 与全量 compute_breadth 逐日**数值一致**(test 锁),只是内存/算力有界。
本缓存**独立于** `tools.pipeline.market_breadth`(15:05 收盘节点)与 `compute_breadth`,不动它们。

## 契约

输入:全A票池 = `store.list_master_codes()`(与广度聚合同源);`--date` 缺省今天,非今天=历史补跑。
输出:`data/signals/capitulation/<YYYY-MM-DD>.json`,见 `build_payload()`。
纪律:幂等(文件在→不覆盖,除 --force);非交易日跳过退0;warmup 未满 500 → degraded 不给 flag。

用法:
    python -m tools.pipeline.capitulation_signal                 # 今天,收盘后跑
    python -m tools.pipeline.capitulation_signal --date 2026-09-11   # 历史补跑(读本地K线)
    python -m tools.pipeline.capitulation_signal --force

⚠️ 测试环境研究用,非投资建议;只读行情、不下单、不做盘中门控。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tools.analysis.market_forecast import breadth as B
from tools.backtest.capitulation.breadth_features import (
    build_capitulation_flags, _grid_key)
from tools.collectors import calendar as cal
from tools.config import settings
from tools.pipeline.intraday_snapshot import _write_atomic

logger = logging.getLogger("pipeline.capitulation_signal")

SCRIPT_VERSION = "1.0.0"

#: 主档 operating point(统筹拍板 cr10;防偷看:固定跨窗口稳的中段,不事后择优)。
#: 依据回测报告:H1-β 在**全 24 组网格通过**(cr 维非选择性),cr10 比 cr05 事件更多(OOS 63 vs 46)
#: → trailing 分位阈值估计更稳,故取 cr10 作主判;α 显著点在回测里恰是 cr5/深档,那正是
#: H1-α「证不了」不去追的方向,主档取 cr10 与"不冒充个股α"一致。
PRIMARY_GRID = (0.90, 0.10, 2)      # os90 × cr10 × w2
#: 深档(更极端更稀)仅作"更保守时"参考输出,**主判仍用 PRIMARY**,不据深档事后择优。
DEEP_GRID = (0.90, 0.05, 2)         # os90 × cr05 × w2

TRAILING = 500                      # 与 build_capitulation_flags 默认一致(分位窗口)
#: 缓存保留的 trailing 交易日数:> TRAILING + 余量,保证 as-of 行已过 warmup(不被 rolling 掩掉)。
KEEP_DAYS = 620

OUT_ROOT = settings.PROJECT_ROOT / "data" / "signals" / "capitulation"
CACHE_PATH = settings.PROJECT_ROOT / "data" / "breadth_series" / "capitulation_breadth.parquet"

#: 本信号只需这三列(build_capitulation_flags 的输入)。
_SIGNAL_COLS = ["below_ma20_ratio", "mean_pct", "net_adv"]


# ────────────────────────── 有界窗口广度聚合(复用单一真源) ──────────────────────────

def _aggregate_breadth_window(codes: list[str], keep_dates, *, cfg: dict | None = None,
                              get_kline=None, data_root=None) -> pd.DataFrame:
    """只对 `keep_dates` 这些交易日算广度(below_ma20_ratio/mean_pct/net_adv),内存有界。

    复用**单一真源** `breadth._per_stock_indicators`(破位广度/涨跌家数,MA20 在个股全史上算,因果)
    + `breadth.cross_section_stats`(全A等权 mean_pct);聚合公式镜像 `compute_breadth` 的对应行,
    **只是先筛到 keep_dates 再累加** → 与全量逐日数值一致(test 锁),不引入第二套口径。

    `get_kline` 可注入(测试喂合成 K线);缺省 = `store.get_master_kline`(需先 ensure_data_root)。
    """
    cfg = cfg or B._CFG
    if get_kline is None:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        from tools.store import repo as store
        ensure_data_root(str(data_root) if data_root else None)
        get_kline = store.get_master_kline

    keep_set = set(pd.to_datetime(sorted(keep_dates)))
    if not keep_set:
        return pd.DataFrame(columns=_SIGNAL_COLS)

    maw = int(cfg["破位MA"])
    count_cols = ["listed", "adv", "dec", f"above_ma{maw}", f"below_ma{maw}", f"ma{maw}_valid"]

    acc: pd.DataFrame | None = None
    pct_lists: dict = {}
    for code in codes:
        try:
            df = get_kline(code)
        except Exception:
            continue
        ind = B._per_stock_indicators(df, code, cfg)     # 个股全史 → 逐日贡献(MA20 因果、精确)
        if ind is None:
            continue
        ind = ind[ind["date"].isin(keep_set)]            # 筛到窗口日(内存有界的关键)
        if ind.empty:
            continue
        cnt = ind.groupby("date")[count_cols].sum()
        acc = cnt if acc is None else acc.add(cnt, fill_value=0)
        for dt, sub in ind.groupby("date")["pct"]:
            pct_lists.setdefault(dt, []).append(sub.to_numpy())

    if acc is None:
        return pd.DataFrame(columns=_SIGNAL_COLS)

    res = acc.sort_index()
    res.index.name = "date"
    total = res["listed"].replace(0, np.nan)
    res["net_adv"] = (res["adv"] - res["dec"]) / total
    ma_valid = res[f"ma{maw}_valid"].replace(0, np.nan)
    res["below_ma20_ratio"] = res[f"below_ma{maw}"] / ma_valid       # 破位广度(与 compute_breadth 同式)
    day_stats = {dt: B.cross_section_stats(np.concatenate(v), total=int(res.loc[dt, "listed"]))
                 for dt, v in pct_lists.items()}
    res["mean_pct"] = pd.Series({d: s["mean_pct"] for d, s in day_stats.items()}).sort_index()
    return res[_SIGNAL_COLS].sort_index()


def refresh_breadth_cache(request_date: str, *, cache_path: Path | None = CACHE_PATH,
                          keep_days: int = KEEP_DAYS, codes: list[str] | None = None,
                          get_kline=None, data_root=None) -> pd.DataFrame:
    """增量刷新有界 trailing 广度缓存到 request_date;返回窗口内(≤request_date)广度序列。

    · 窗口 = 日历里 ≤request_date 的最近 keep_days 个交易日;
    · 只算缓存缺的窗口日(常态 1 天;空缓存/缺口→补齐,自愈无 gap);
    · 只保留窗口日(有界,绝不全 2018 累积)。
    """
    cache_path = Path(cache_path) if cache_path else None
    all_td = sorted(d for d in cal.trading_dates() if d <= request_date)
    window = all_td[-keep_days:] if len(all_td) >= keep_days else all_td
    window_set = set(window)

    have: pd.DataFrame | None = None
    if cache_path and cache_path.exists():
        have = pd.read_parquet(cache_path)
        if "date" in have.columns:
            have = have.set_index("date")
        have.index = pd.to_datetime(have.index)

    have_dates = set(have.index.strftime("%Y-%m-%d")) if have is not None else set()
    need = window_set - have_dates

    if need:
        if codes is None:
            from tools.analysis.market_forecast.dataroot import ensure_data_root
            from tools.store import repo as store
            ensure_data_root(str(data_root) if data_root else None)
            codes = store.list_master_codes()
        new = _aggregate_breadth_window(codes, need, get_kline=get_kline, data_root=data_root)
        merged = pd.concat([have, new]) if have is not None and not have.empty else new
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = have if have is not None else pd.DataFrame(columns=_SIGNAL_COLS)

    # 有界:只留窗口日
    if not merged.empty:
        keep_mask = merged.index.strftime("%Y-%m-%d").isin(window_set)
        merged = merged[keep_mask].sort_index()

    if cache_path and need and not merged.empty:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        merged.reset_index().to_parquet(cache_path)

    return merged[_SIGNAL_COLS] if not merged.empty else merged


# ────────────────────────── 信号计算(复用回测判据) ──────────────────────────

def _f(v) -> float | None:
    """标量 → float 或 None(NaN/None → None,便于 JSON 与下游判空)。"""
    try:
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return None
        return round(float(v), 6)
    except (TypeError, ValueError):
        return None


def compute_signal(breadth: pd.DataFrame, as_of: str) -> dict | None:
    """对 breadth 序列跑主档+深档 capitulation 判据,取 as-of(≤as_of 的最近交易日)那行。

    返回 None ⟺ 序列里无 ≤as_of 的行。`warmup=True` ⟺ trailing 未满 500(此时 cap 恒 False)。
    """
    if breadth is None or breadth.empty:
        return None
    grid = [PRIMARY_GRID, DEEP_GRID]
    flags = build_capitulation_flags(breadth[_SIGNAL_COLS], grid=grid, trailing=TRAILING)

    as_of_ts = pd.Timestamp(as_of)
    avail = flags.index[flags.index <= as_of_ts]
    if len(avail) == 0:
        return None
    row_ts = avail[-1]
    row = flags.loc[row_ts]

    def _sig(g) -> dict:
        key = _grid_key(*g)
        osr = row["below_ma20_ratio"]
        os_thr = row[f"{key}_os_thr"]
        cum = row[f"{key}_cum"]
        crash_thr = row[f"{key}_crash_thr"]
        return {
            "grid_key": key,
            "capitulation": bool(row[key]),
            "below_ma20_ratio": _f(osr),
            "below_ma20_thr": _f(os_thr),               # trailing-500 q_os 分位
            "cum_w": _f(cum),                            # 最近 w 日等权累计涨跌
            "crash_thr": _f(crash_thr),                  # trailing-500 q_crash 分位
            # 连续强度:两维距各自阈值的余量(>0 = 满足该维,越大越极端)
            "os_margin": _f(None if _f(osr) is None or _f(os_thr) is None else osr - os_thr),
            "crash_margin": _f(None if _f(crash_thr) is None or _f(cum) is None else crash_thr - cum),
        }

    return {
        "as_of": row_ts.strftime("%Y-%m-%d"),
        "warmup": bool(row["warmup"]),
        "trailing_n": int((flags.index <= row_ts).sum()),
        "primary": _sig(PRIMARY_GRID),
        "deep": _sig(DEEP_GRID),
    }


# ────────────────────────── 组装与落盘 ──────────────────────────

def signal_path(date: str) -> Path:
    return OUT_ROOT / f"{date}.json"


def build_payload(date: str, signal: dict | None, breadth: pd.DataFrame,
                  captured_at: datetime, mode: str) -> dict:
    """产出 JSON。degraded ⟺ 无信号 / warmup 未满 / 序列末日早于请求日。"""
    reasons: list[str] = []
    if signal is None:
        reasons.append("无 ≤请求日的广度行:序列为空或数据缺失,不给 flag")
    else:
        if signal["warmup"]:
            reasons.append("trailing 未满 %d 交易日(warmup):capitulation 判据未生效,cap 恒 False"
                           % TRAILING)
        if signal["as_of"] != date:
            reasons.append("广度序列末日 %s 早于请求日 %s:用最近可得日作 as-of(数据滞后)"
                           % (signal["as_of"], date))
    primary = signal["primary"] if signal else None
    deep = signal["deep"] if signal else None
    span = None
    if breadth is not None and not breadth.empty:
        span = {"start": breadth.index.min().strftime("%Y-%m-%d"),
                "end": breadth.index.max().strftime("%Y-%m-%d"), "n": int(len(breadth))}
    return {
        "date": date,
        "as_of": signal["as_of"] if signal else None,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "grid_key_primary": _grid_key(*PRIMARY_GRID),
        "grid_key_deep": _grid_key(*DEEP_GRID),
        # 主判(β 择时用):是否 capitulation
        "capitulation": (primary["capitulation"] if primary else None),
        "capitulation_deep": (deep["capitulation"] if deep else None),
        "primary": primary,
        "deep": deep,
        "warmup": (signal["warmup"] if signal else None),
        "trailing_n": (signal["trailing_n"] if signal else None),
        "breadth_span": span,
        "degraded": bool(reasons),
        "degrade_reasons": reasons,
        "caveats": [
            "收盘确定性信号,只门控次日(t+1)选股/市场环境判断;绝不盘中实时门控(经验#17)",
            "capitulation=市场级 β 择时(放松整体降级、给β敞口);绝不据此给个股加'超跌选股α分'(H1-α证不了)",
            "trailing-500 分位因果自标定,只用≤当日已披露数据(防未来函数)",
        ],
        "meta": {
            "source": "tools.pipeline.capitulation_signal",
            "script_version": SCRIPT_VERSION,
            "mode": mode,
            "detector": "tools.backtest.capitulation.breadth_features.build_capitulation_flags",
            "breadth_proxy_definition": B.CROSS_SECTION_SOURCE,
            "operating_point_note": (
                "主档 cr10:回测 H1-β 全24组通过(cr非选择性)、cr10事件更多阈值更稳;"
                "非事后择优(α显著点在cr5/深档,那是H1-α证不了、不追的方向)"),
            "report": "docs/计划/2026-09-14_底部超跌反抽假设回测_报告.md",
            "note": "capitulation β豁免信号,供次日选股;非投资建议",
        },
    }


def run(*, date: str | None = None, force: bool = False, data_root: str | None = None,
        keep_days: int = KEEP_DAYS) -> int:
    """跑一次 capitulation 信号。返回退出码(0=成功/跳过,1=不可用)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    date = date or today

    if not cal.is_trading_day(date):
        logger.info("跳过:%s 非 A 股交易日", date)
        return 0

    out = signal_path(date)
    if out.exists() and not force:
        logger.info("跳过:信号文件已存在,不覆盖 %s(要重算加 --force)", out)
        return 0

    mode = "realtime_close" if date == today else "kline_backfill"
    captured_at = datetime.now().astimezone()
    try:
        breadth = refresh_breadth_cache(date, keep_days=keep_days, data_root=data_root)
    except Exception as e:
        logger.exception("广度缓存刷新失败:%s: %s", type(e).__name__, e)
        return 1

    if breadth is None or breadth.empty:
        logger.error("广度序列为空(数据根/票池?),不落文件、非0退出")
        return 1

    signal = compute_signal(breadth, date)
    payload = build_payload(date, signal, breadth, captured_at, mode)
    _write_atomic(out, payload)
    logger.info("落盘 %s:as_of=%s capitulation=%s(deep=%s) warmup=%s trailing_n=%s degraded=%s",
                out, payload["as_of"], payload["capitulation"], payload["capitulation_deep"],
                payload["warmup"], payload["trailing_n"], payload["degraded"])
    if payload["degraded"]:
        for r in payload["degrade_reasons"]:
            logger.warning("降级:%s", r)
    return 0


# ────────────────────────── CLI ──────────────────────────

def _setup_logging() -> None:
    log_path = settings.PROJECT_ROOT / "logs" / "capitulation_signal.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()):
        h.setFormatter(fmt)
        root.addHandler(h)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Live capitulation 信号(收盘确定性节点):当日全A广度是否触发极端底部,供次日β择时")
    ap.add_argument("--date", default=None, help="口径日 YYYY-MM-DD(默认今天);非今天=历史补跑读本地K线")
    ap.add_argument("--force", action="store_true", help="覆盖已有信号文件")
    ap.add_argument("--data-root", default=None, help="生产数据根(默认自动探测主仓)")
    ap.add_argument("--keep-days", type=int, default=KEEP_DAYS, help="trailing 缓存保留交易日数")
    args = ap.parse_args(argv)
    _setup_logging()
    try:
        return run(date=args.date, force=args.force, data_root=args.data_root,
                   keep_days=args.keep_days)
    except Exception as e:
        logger.exception("capitulation 信号任务异常退出:%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
