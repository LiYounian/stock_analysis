#!/usr/bin/env python
"""H2 · 科创板 688/689 主档 volume 100× 高估的**一次性回补**(方案 B:baostock 全量重算)。

背景(见 docs/计划/2026-09-18_H1H5_数据地基缺陷_诊断与修复方案.md §H2):
腾讯 gtimg 两个端点对 688/689 段返回的 volume 已是"股",采集层历史上一律 ×100
→ 618 只科创板(617 只 688 + 689009)自 08-13 起约 1.4 万行 volume 高估 100 倍,
08-13~09-03 turnover 被连带 100×,部分回退日 amount 缺失(NaN)。

采集层根因已由 C1 修复(gtimg_quote 量额自证归一 + fqkline 分板块 ×100);本脚本
只处理**已经落在主档 parquet 里的历史脏行**,用 baostock 权威值全量重算这 618 只。

为什么用方案 B(整表换 baostock)而不是"坏行 ÷100":
  · baostock volume 全板块统一"股"、turnover 是百分数、amount 在位 → 一次把
    volume / turnover / amount / OHLC 全换成权威值,顺带补齐回退日的 amount NaN;
  · 避免"÷100 启发式"对新股/停牌 volume=0、参考窗选取的脆弱性。

**执行顺序硬约束(陷阱)**:方案 B 整表换源天然规避 turnover 侧陷阱——
  · **绝不**在当前状态下跑 `ops.fix_turnover_unit --apply`(会把 688 正确的 turnover ×100);
  · **绝不**在 volume 修好前跑 `ops.backfill_turnover --apply`(会用 100× volume 回填 turnover)。
本脚本先把 volume 换成权威值,turnover/amount 同步由 baostock 覆盖,单步完成、无中间态。

必须传 `start=meta.first_date`:`baostock_src.fetch_one` 无缺省早界,若用主档缺省区间
(约今天往前 500 自然日)会**截掉 2018~2024 的历史起点**。本脚本逐票读 meta.first_date
作为 start,保留每票历史起点。

用法(默认 dry-run,**不写盘**):
    python -m ops.backfill_star_volume_h2                     # 巡检:列出 618 只 + 抽样坏值,不动数据
    python -m ops.backfill_star_volume_h2 --apply            # 备份 → baostock 重算 → 复核(真写盘)
    python -m ops.backfill_star_volume_h2 --verify-only      # 只跑复核扫描(回补后校验)
    python -m ops.backfill_star_volume_h2 --codes 688981,688802  # 只处理指定票
    [--data-root DIR]   主档数据根(含 master/;缺省=主仓 data,自动定位,非 worktree)
    [--backup-dir DIR]  备份目录(缺省 <data-root>/master/kline_backup_YYYYmmdd_H2)

**主档写操作前必备份**;**回补必须避开收盘闭环运行窗 15:40~17:00**。
退出码:0 = 成功/干净;1 = 复核发现异常行;2 = 运行出错。
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from pathlib import Path

import pandas as pd

# 复核口径:volume / (amount/close) = close/VWAP,单位正确("股")时应≈1。
# 为什么用 [0.7,1.4] 而不是 [0.9,1.1]:该比值本质是 close/当日 VWAP,在高波动/涨跌停日
# 会合理偏离 ±15~30%(实测 688802 09-17 = 1.113,是真值不是错);而 100× 单位错的比值
# 是 ~100 或 ~0.01,与 1 相差两个数量级。宽到 [0.7,1.4] 能干净区分"单位对但当日波动大"
# 与"单位错 100×",不误伤波动票。落在此区间外的按"疑异常"列出、不强改。
_RATIO_LO, _RATIO_HI = 0.7, 1.4
_BLOCKED_WINDOW = (dt.time(15, 40), dt.time(17, 0))   # 收盘闭环运行窗,回补须避开


def _resolve_data_root(data_root: str | None) -> Path:
    """定位主档数据根(含 master/)。缺省=主仓 data 目录(非 worktree)。

    在 worktree 里跑时 settings.DATA_MASTER 会指向 worktree/data/master(通常不存在),
    故这里优先用显式 --data-root;缺省再从主仓路径推断(worktree 的 .claude/worktrees 上溯)。
    """
    if data_root:
        return Path(data_root).expanduser().resolve()
    here = Path(__file__).resolve()
    # ops/ 在 worktree 或主仓;主仓根 = 含真实 data/master 的那层
    for base in [here.parents[1], *here.parents]:
        cand = base / "data" / "master" / "kline"
        if cand.is_dir():
            return base / "data"
    # 兜底:worktree 常见布局 .../<repo>/.claude/worktrees/<wt>/ops → 上溯到 <repo>
    p = here
    while p != p.parent:
        if (p / "data" / "master" / "kline").is_dir():
            return p / "data"
        p = p.parent
    raise SystemExit("找不到 data/master/kline,请用 --data-root 显式指定主仓 data 目录")


def _point_store_at(data_root: Path):
    """把 store 的主档目录指到 data_root/master(worktree 里跑也能写主仓生产数据)。"""
    from tools.store import repo as store
    master = data_root / "master"
    (master / "kline").mkdir(parents=True, exist_ok=True)
    store._MASTER_DIR = master
    return store


def star_master_codes(store) -> list[str]:
    """主档里所有科创板 688/689 代码(升序)。判据走 exchange.is_star_market 单一真源。"""
    from tools.config import exchange
    kd = store._MASTER_DIR / "kline"
    codes = []
    for p in sorted(kd.glob("*.parquet")):
        c = p.stem
        if exchange.is_star_market(c):
            codes.append(c)
    return codes


def _ratio(df: pd.DataFrame) -> pd.Series:
    """逐行 volume / (amount/close);amount/close/volume 任一缺失或非正 → NaN(不判)。"""
    c = pd.to_numeric(df.get("close"), errors="coerce")
    v = pd.to_numeric(df.get("volume"), errors="coerce")
    a = pd.to_numeric(df.get("amount"), errors="coerce")
    shares = a / c
    r = v / shares
    return r.where((c > 0) & (v > 0) & (a > 0))


def scan_code(store, code: str) -> dict:
    """读一票主档,统计 volume 单位自洽情况(不写盘)。"""
    df = store.get_master_kline(code)
    r = _ratio(df)
    judged = int(r.notna().sum())
    ok = int(r.between(_RATIO_LO, _RATIO_HI).sum())
    bad = judged - ok
    bad_dates = pd.to_datetime(df.loc[r.notna() & ~r.between(_RATIO_LO, _RATIO_HI), "date"])
    return {
        "code": code, "rows": len(df), "judged": judged, "ok": ok, "bad": bad,
        "bad_dates": [d.strftime("%Y-%m-%d") for d in bad_dates],
        "amount_nan": int(pd.to_numeric(df.get("amount"), errors="coerce").isna().sum()),
    }


def backup(store, codes: list[str], backup_dir: Path) -> int:
    """把每票 parquet + meta.json 复制到 backup_dir(幂等:目标已存在则跳过)。返回复制文件数。"""
    kd = store._MASTER_DIR / "kline"
    backup_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for c in codes:
        for suffix in (".parquet", ".meta.json"):
            src = kd / f"{c}{suffix}"
            dst = backup_dir / f"{c}{suffix}"
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                n += 1
    return n


def backfill_one(store, code: str, adjust: str) -> dict:
    """用 baostock 从 meta.first_date 全量重算一票并覆盖写主档。需在 baostock session 内调用。

    保留历史起点:start=meta.first_date(缺省用主档最早 bar,再兜底 2015-01-01)。
    覆盖 volume/turnover/amount/OHLC 全列 → 顺带补齐回退日 amount NaN、修正 turnover 100×。
    """
    from tools.collectors import baostock_src
    meta = store.get_master_kline_meta(code) or {}
    old = store.get_master_kline(code)
    start = meta.get("first_date")
    if not start:
        try:
            start = pd.to_datetime(old["date"]).min().strftime("%Y-%m-%d")
        except Exception:
            start = "2015-01-01"
    end = dt.date.today().strftime("%Y-%m-%d")
    df = baostock_src.fetch_one(code, start, end, adjust=adjust)   # ValueError=空数据 / 源不支持
    store.put_master_kline(code, df, meta={"source": "baostock", "adjust": adjust,
                                           "h2_star_volume_backfilled": dt.datetime.now().isoformat(timespec="seconds")})
    return {"code": code, "start": start, "rows_old": len(old), "rows_new": len(df)}


def _in_blocked_window(now: dt.datetime | None = None) -> bool:
    t = (now or dt.datetime.now()).time()
    return _BLOCKED_WINDOW[0] <= t <= _BLOCKED_WINDOW[1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="H2 科创板 volume 100× 主档回补(方案 B)")
    ap.add_argument("--apply", action="store_true", help="真写盘(备份→回补→复核);缺省 dry-run")
    ap.add_argument("--verify-only", action="store_true", help="只跑复核扫描,不回补")
    ap.add_argument("--codes", default="", help="只处理指定票(逗号分隔);缺省=全部 688/689")
    ap.add_argument("--data-root", default=None, help="主档数据根(含 master/);缺省自动定位主仓 data")
    ap.add_argument("--backup-dir", default=None, help="备份目录;缺省 <data-root>/master/kline_backup_YYYYmmdd_H2")
    ap.add_argument("--adjust", default=None, help="复权口径;缺省用 settings.KLINE_ADJUST")
    ap.add_argument("--force-window", action="store_true", help="允许在 15:40~17:00 收盘窗内跑(默认拒绝)")
    args = ap.parse_args(argv)

    from tools.config import settings
    adjust = args.adjust or settings.KLINE_ADJUST

    data_root = _resolve_data_root(args.data_root)
    store = _point_store_at(data_root)
    print(f"[H2] 数据根: {data_root}  master: {store._MASTER_DIR}")

    codes = [c.strip() for c in args.codes.split(",") if c.strip()] or star_master_codes(store)
    print(f"[H2] 目标 688/689 主档: {len(codes)} 只"
          f"(688={sum(c.startswith('688') for c in codes)} 689={sum(c.startswith('689') for c in codes)})")

    # ——— 复核扫描(dry-run / verify-only / apply 后都会跑一遍) ———
    def run_scan(tag: str) -> tuple[int, int, list[dict]]:
        reps = [scan_code(store, c) for c in codes]
        n_bad_codes = sum(1 for r in reps if r["bad"] > 0)
        n_bad_rows = sum(r["bad"] for r in reps)
        n_amt_nan = sum(r["amount_nan"] for r in reps)
        print(f"[H2·{tag}] 异常票 {n_bad_codes}/{len(codes)}  异常行 {n_bad_rows}  amount 缺失行 {n_amt_nan}")
        return n_bad_codes, n_bad_rows, reps

    if args.verify_only:
        n_bad_codes, n_bad_rows, reps = run_scan("verify")
        _sample_report(store, codes)
        bad = [r for r in reps if r["bad"] > 0]
        if bad:
            print("[H2·verify] 异常票清单(前 30):")
            for r in bad[:30]:
                print(f"    {r['code']}  异常{r['bad']}/{r['judged']} 行  日期示例 {r['bad_dates'][:5]}")
        return 1 if n_bad_rows else 0

    n_bad0, rows0, _ = run_scan("before")
    _sample_report(store, codes)

    if not args.apply:
        print("[H2] dry-run:未写盘。加 --apply 执行 备份→回补→复核。")
        return 1 if rows0 else 0

    if _in_blocked_window() and not args.force_window:
        print("[H2] 拒绝执行:当前处于收盘闭环运行窗 15:40~17:00。请改期或加 --force-window。")
        return 2

    # ——— 备份 ———
    backup_dir = Path(args.backup_dir) if args.backup_dir else \
        store._MASTER_DIR / f"kline_backup_{dt.date.today():%Y%m%d}_H2"
    n_files = backup(store, codes, backup_dir)
    print(f"[H2·backup] 已备份 {n_files} 个文件 → {backup_dir}")

    # ——— baostock 全量重算 ———
    from tools.collectors import baostock_src
    ok, failed = 0, []
    with baostock_src.session():
        for i, c in enumerate(codes, 1):
            try:
                r = backfill_one(store, c, adjust)
                ok += 1
                if i % 100 == 0 or i == len(codes):
                    print(f"[H2·backfill] {i}/{len(codes)} 最新 {c}: {r['rows_old']}→{r['rows_new']} 行 (start={r['start']})")
            except Exception as ex:  # noqa: BLE001
                failed.append((c, f"{type(ex).__name__}: {ex}"))
    print(f"[H2·backfill] 完成 ok={ok} failed={len(failed)}")
    if failed:
        print("[H2·backfill] 失败票(原始值保留,未改):")
        for c, why in failed[:50]:
            print(f"    {c}: {why}")

    # ——— 复核 ———
    n_bad1, rows1, reps = run_scan("after")
    _sample_report(store, codes)
    remaining = [r for r in reps if r["bad"] > 0]
    if remaining:
        print("[H2·after] 仍异常票清单(前 30,不强改):")
        for r in remaining[:30]:
            print(f"    {r['code']}  异常{r['bad']}/{r['judged']} 行  日期示例 {r['bad_dates'][:5]}")
    print(f"[H2] 汇总:回补前异常行 {rows0} → 回补后 {rows1};备份 {backup_dir}")
    return 1 if rows1 else 0


def _sample_report(store, codes: list[str]) -> None:
    """抽样打印 688110/688802/688795 的 09-17 volume/(amount/close)。"""
    for c in ("688110", "688802", "688795"):
        if c not in codes:
            continue
        try:
            df = store.get_master_kline(c)
            d = df[pd.to_datetime(df["date"]) >= "2026-09-17"].head(1)
            if len(d):
                row = d.iloc[0]
                shares = row["amount"] / row["close"] if row["close"] else float("nan")
                ratio = row["volume"] / shares if shares else float("nan")
                print(f"[H2·sample] {c} 09-17 volume={row['volume']:.4g} amount={row['amount']:.4g} "
                      f"close={row['close']:.4g} → volume/(amount/close)={ratio:.4g}")
        except Exception as ex:  # noqa: BLE001
            print(f"[H2·sample] {c} 读取失败: {ex}")


if __name__ == "__main__":
    sys.exit(main())
