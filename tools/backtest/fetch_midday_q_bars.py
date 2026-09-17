"""午盘 Q · M3.b 分时数据采集(baostock 5min · 真 14:30/14:50 价)。

## 为什么有这个脚本(2026-09-17)

`_数据源实测.md`(09-07)结论是"个股价格分时·历史回测 = ❌ 无历史源",
当时测了 akshare / 东财 push2 / mootdx 三条路,**漏测了 baostock 的分钟频**。
09-17 复测发现 baostock `frequency="5"` 可用且历史至少回溯到 2023 年,
**14:30 与 14:50 两个时刻精确存在**(正是午盘Q首判/复核判定点)。

→ M3.b 分时回测**不必等 60 个交易日未来采样**,Q1/Q2 立刻可做真回测。
   (Q3 仍需等:东财 fflow/kline 只返当天资金流,`lmt` 给多大都拿不到历史。)

## 落盘格式

`data/analysis/midday_q/m3b_bars/<code>.parquet`,列:
    date, time(HHMM), open, high, low, close, volume, amount

只保留回测实际会用到的时刻(见 KEEP_TIMES),把 12000 行/年/票 压到 ~7 行/日/票,
避免几百 MB 中间文件 —— baostock 不支持按时刻过滤,必须整天拉回来再裁。

## 用法

    python -m tools.backtest.fetch_midday_q_bars --start 2026-03-01 --end 2026-09-10
    python -m tools.backtest.fetch_midday_q_bars --limit 20      # 先小规模验证

幂等:已有 parquet 且覆盖请求区间的票直接跳过(--force 覆盖重拉)。

⚠️ 测试环境研究用,非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import pandas as pd

from tools.config import midday_q_universe as UNIV
from tools.config import settings

logger = logging.getLogger("backtest.fetch_midday_q_bars")

OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "m3b_bars"

# 回测需要的时刻(HHMM):
#   0935 当日首根收盘 ≈ 开盘后第一个5min → 作"当日 open"锚
#   1030 Q1 AmPmRatio 的上午量能基准
#   1425/1430 首判(1425 备用:若某日缺 1430 可回退)
#   1445/1450 复核 + T+1 卖出价(方案 v0.3:次日 14:30-14:50 尾盘卖)
#   1500 收盘(对照 M3.a 的收盘代理口径,量化降级偏差)
KEEP_TIMES = ("0935", "1030", "1425", "1430", "1445", "1450", "1500")


def _bs_code(code: str) -> str:
    """6位代码 → baostock 格式(sh./sz.)。沪(6/9)= sh,深(0/2/3)= sz。"""
    return f"sh.{code}" if code[0] in ("6", "9") else f"sz.{code}"


def fetch_one(bs, code: str, start: str, end: str) -> pd.DataFrame:
    """拉单票 5min 线并裁到 KEEP_TIMES。空/失败 → 空 DataFrame。"""
    rs = bs.query_history_k_data_plus(
        _bs_code(code),
        "date,time,open,high,low,close,volume,amount",
        start_date=start, end_date=end, frequency="5", adjustflag="3")
    if rs.error_code != "0":
        logger.warning("%s 查询失败: %s", code, rs.error_msg)
        return pd.DataFrame()
    rows = []
    while rs.next():
        d, t, o, h, l, c, v, a = rs.get_row_data()
        hhmm = t[8:12]                      # time 形如 20260910145500000
        if hhmm not in KEEP_TIMES:
            continue
        try:
            rows.append({"date": d, "time": hhmm,
                          "open": float(o), "high": float(h), "low": float(l),
                          "close": float(c),
                          "volume": float(v) if v else 0.0,
                          "amount": float(a) if a else 0.0})
        except (TypeError, ValueError):
            continue                        # 停牌等异常行:跳过,不填 0 伪装
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["date", "time"]).reset_index(drop=True)


def _covers(path: Path, start: str, end: str) -> bool:
    """已落盘文件是否已覆盖请求区间(幂等判据)。"""
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    return str(df["date"].min()) <= start and str(df["date"].max()) >= end


def run(start: str, end: str, *, limit: int | None = None,
        force: bool = False) -> int:
    import baostock as bs

    codes = UNIV.get_focus_codes()
    if limit:
        codes = codes[:limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lg = bs.login()
    if lg.error_code != "0":
        logger.error("baostock 登录失败: %s", lg.error_msg)
        return 1

    ok = skipped = failed = 0
    t0 = time.time()
    try:
        for i, code in enumerate(codes, 1):
            out = OUT_DIR / f"{code}.parquet"
            if not force and out.exists() and _covers(out, start, end):
                skipped += 1
                continue
            df = fetch_one(bs, code, start, end)
            if df.empty:
                failed += 1
                logger.warning("[%d/%d] %s 无数据", i, len(codes), code)
                continue
            df.to_parquet(out, index=False)
            ok += 1
            if i % 10 == 0 or i == len(codes):
                el = time.time() - t0
                logger.info("[%d/%d] 已落 %d 只 (跳过%d 失败%d) 用时 %.0fs",
                            i, len(codes), ok, skipped, failed, el)
    finally:
        bs.logout()

    logger.info("完成:新落 %d / 跳过 %d / 失败 %d,共 %d 只,用时 %.0fs",
                ok, skipped, failed, len(codes), time.time() - t0)
    return 0 if (ok + skipped) > 0 else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="午盘Q M3.b 分时数据采集(baostock 5min)")
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-10")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只(验证用)")
    ap.add_argument("--force", action="store_true", help="忽略幂等,重拉覆盖")
    a = ap.parse_args()
    return run(a.start, a.end, limit=a.limit, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
