"""午盘 Q · M3.b 分时数据采集(baostock 5min · 全A 排北交所 · 可断点续跑)。

## 为什么有这个脚本(2026-09-17)

`_数据源实测.md`(09-07)结论是"个股价格分时·历史回测 = ❌ 无历史源",
当时测了 akshare / 东财 push2 / mootdx 三条路,**漏测了 baostock 的分钟频**。
09-17 复测发现 baostock `frequency="5"` 可用且历史至少回溯到 2023 年,
**14:30 与 14:50 两个时刻精确存在**(正是午盘Q首判/复核判定点)。

→ M3.b 分时回测**不必等 60 个交易日未来采样**,Q1/Q2 立刻可做真回测。
   (Q3 仍需等:东财 fflow/kline 只返当天资金流,`lmt` 给多大都拿不到历史。)

## 三个必须处理的 baostock 坑(v1 踩过,实测数据)

**① 长会话会被掐断** —— v1 用"单次 login 跑完 126 只"的写法,跑到第 51 只
(约 18 分钟)后**连续 73 只全报无数据**;那些票单独用新会话测完全正常
(n=1392)。→ 本版**每票独立 login/logout**。

**② 并发过高会静默截断** —— 6 进程并行实测失败率 33%~67%,且失败模式是
`error_code=10001001` 配 **n=2000 的不完整数据**(不抛异常!)。
若不校验,坏数据会静默混进回测。→ 本版**按预期行数校验**,不足即判失败重试。
实测最优并发:P=3 → 12/12 成功、2.0s/只;P=5 → 11/12、2.5s/只(反而更慢)。

**③ 幂等必须校验内容而非仅存在性** —— 文件存在 ≠ 内容完整(见坑②)。
→ `_ok_on_disk()` 同时校验区间覆盖与每日 bar 数。

## 落盘格式

`data/analysis/midday_q/m3b_bars/<code>.parquet`,列:
    date, time(HHMM), open, high, low, close, volume, amount

只保留回测实际会用到的时刻(KEEP_TIMES),把 12000 行/年/票压到 ~7 行/日/票
—— baostock 不支持按时刻过滤,必须整天拉回来再裁。

## 用法

    # 焦点池(126 只,约 5 分钟)
    python -m tools.backtest.fetch_midday_q_bars --start 2026-01-02 --end 2026-09-10

    # 全A 排北交所(约 5200 只,P=3 下约 3 小时)
    python -m tools.backtest.fetch_midday_q_bars --universe fullA \
        --start 2026-01-02 --end 2026-09-10 --procs 3

幂等/可续跑:已完整落盘的票直接跳过 → 中断后重跑同一条命令即可续。

⚠️ 测试环境研究用,非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import time
from multiprocessing import Pool
from pathlib import Path

import pandas as pd

from tools.config import midday_q_universe as UNIV
from tools.config import settings

logger = logging.getLogger("backtest.fetch_midday_q_bars")

OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis" / "midday_q" / "m3b_bars"
CODE_NAME_JSON = settings.PROJECT_ROOT / "config" / "code_name.json"

# 回测需要的时刻(HHMM):
#   0935 当日首根收盘 ≈ 开盘后第一个5min → 作"当日 open"锚
#   1030 Q1 AmPmRatio 的上午量能基准
#   1425/1430 首判(1425 备用:若某日缺 1430 可回退)
#   1445/1450 复核 + T+1 卖出价(方案 v0.3:次日 14:30-14:50 尾盘卖)
#   1500 收盘(对照 M3.a 的收盘代理口径,量化降级偏差)
KEEP_TIMES = ("0935", "1030", "1425", "1430", "1445", "1450", "1500")

# 北交所代码段:8xx/4xx(老)+ 92x(新)。午盘Q票池口径排北交所(流动性/涨跌幅规则不同)。
_BJ_PREFIX = ("4", "8", "92")

MAX_ATTEMPTS = 4            # 每票最多尝试次数(应对坑①②的瞬时失败)
_RETRY_BASE_SEC = 1.5       # 退避基数:第 k 次失败后睡 1.5*k 秒


def _bs_code(code: str) -> str:
    """6位代码 → baostock 格式(sh./sz.)。沪(6/9)= sh,深(0/2/3)= sz。"""
    return f"sh.{code}" if code[0] in ("6", "9") else f"sz.{code}"


def fullA_codes() -> list[str]:
    """全A代码(排北交所),取自 config/code_name.json(离线,不打 akshare)。

    刻意不用 `backtest.screen_forward_common.universe_codes()` —— 那个依赖
    已落地主档(data/master/kline),本机主档只有焦点池 126 只,拿不到全A。
    """
    with open(CODE_NAME_JSON, encoding="utf-8") as f:
        data = json.load(f)
    codes = list(data) if isinstance(data, dict) else list(data)
    return sorted(c for c in map(str, codes)
                   if len(c) == 6 and not c.startswith(_BJ_PREFIX))


def resolve_codes(universe: str, limit: int | None = None) -> list[str]:
    codes = fullA_codes() if universe == "fullA" else UNIV.get_focus_codes()
    return codes[:limit] if limit else codes


def _parse_rows(rs) -> list[dict]:
    """baostock 结果集 → 只含 KEEP_TIMES 的行。"""
    rows: list[dict] = []
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
    return rows


def _ok_on_disk(path: Path, start: str, end: str) -> bool:
    """已落盘文件是否可信(幂等判据)。

    只判"存在"不够 —— 并发截断会留下**看似正常的半份文件**(坑②)。
    故同时校验:①区间已覆盖 ②每个交易日的 bar 数不少于 KEEP_TIMES 的一半
    (留松量:个别日确实可能缺某时刻,如停牌半天)。
    """
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    if not (str(df["date"].min()) <= start and str(df["date"].max()) >= end):
        return False
    per_day = df.groupby("date").size()
    return bool((per_day >= len(KEEP_TIMES) // 2).all())


def fetch_one(code: str, start: str, end: str) -> tuple[str, int, int, str]:
    """拉单票并落盘。返回 (code, 行数, 尝试次数, 状态)。

    **每次尝试都新建 login/logout 会话** —— 长会话会被服务端掐断(坑①)。
    """
    import baostock as bs

    out = OUT_DIR / f"{code}.parquet"
    rows: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            lg = bs.login()
            if lg.error_code != "0":
                time.sleep(_RETRY_BASE_SEC * attempt)
                continue
            rs = bs.query_history_k_data_plus(
                _bs_code(code),
                "date,time,open,high,low,close,volume,amount",
                start_date=start, end_date=end, frequency="5", adjustflag="3")
            err = rs.error_code
            rows = _parse_rows(rs)
            try:
                bs.logout()
            except Exception:
                pass                        # logout 失败不影响已取到的数据

            # 坑②:error_code 非 0 时数据可能被静默截断 → 一律重试,不落盘
            if err != "0":
                time.sleep(_RETRY_BASE_SEC * attempt)
                continue
            if not rows:
                # 真无数据(新股未上市/长期停牌)与瞬时失败无法区分 → 重试到上限
                time.sleep(_RETRY_BASE_SEC * attempt)
                continue

            df = pd.DataFrame(rows).sort_values(["date", "time"]).reset_index(drop=True)
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            df.to_parquet(out, index=False)
            return (code, len(df), attempt, "ok")
        except Exception as e:                # 网络/协议异常:退避重试
            logger.debug("%s 第%d次异常: %s", code, attempt, e)
            time.sleep(_RETRY_BASE_SEC * attempt)
    return (code, 0, MAX_ATTEMPTS, "fail")


_JOB: dict = {}


def _init(start: str, end: str) -> None:
    _JOB["start"] = start
    _JOB["end"] = end


def _work(code: str) -> tuple[str, int, int, str]:
    return fetch_one(code, _JOB["start"], _JOB["end"])


def run(start: str, end: str, *, universe: str = "focus",
        limit: int | None = None, procs: int = 3,
        force: bool = False) -> int:
    codes = resolve_codes(universe, limit)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    todo = codes if force else [c for c in codes
                                 if not _ok_on_disk(OUT_DIR / f"{c}.parquet", start, end)]
    done_already = len(codes) - len(todo)
    logger.info("票池=%s 共 %d 只;已完整 %d 只(跳过),待取 %d 只;并发 %d",
                universe, len(codes), done_already, len(todo), procs)
    if not todo:
        logger.info("全部已完整,无需采集")
        return 0

    ok = fail = retried = 0
    failed_codes: list[str] = []
    t0 = time.time()
    with Pool(procs, initializer=_init, initargs=(start, end)) as pool:
        for i, (code, n, attempts, status) in enumerate(
                pool.imap_unordered(_work, todo), 1):
            if status == "ok":
                ok += 1
                retried += (attempts > 1)
            else:
                fail += 1
                failed_codes.append(code)
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t0
                rate = el / i
                eta = rate * (len(todo) - i)
                logger.info("[%d/%d] 成功%d 失败%d (重试救回%d) | %.1fs/只 | "
                            "已用%.0f分 剩约%.0f分",
                            i, len(todo), ok, fail, retried, rate, el / 60, eta / 60)

    el = time.time() - t0
    logger.info("完成:成功 %d / 失败 %d / 跳过 %d,共 %d 只,用时 %.0f 分钟",
                ok, fail, done_already, len(codes), el / 60)
    if failed_codes:
        # 失败清单落盘,便于单独重跑(而不是在几千行日志里捞)
        fp = OUT_DIR.parent / "m3b_fetch_failed.json"
        fp.write_text(json.dumps({"universe": universe, "start": start, "end": end,
                                   "failed": sorted(failed_codes)},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning("失败 %d 只,清单见 %s(重跑同一命令会自动续)",
                       len(failed_codes), fp)
    return 0 if ok > 0 or done_already > 0 else 1


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="午盘Q M3.b 分时数据采集(baostock 5min)")
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--end", default="2026-09-10")
    ap.add_argument("--universe", choices=("focus", "fullA"), default="focus",
                    help="focus=焦点池126只;fullA=全A排北交所(约5200只)")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只(验证用)")
    ap.add_argument("--procs", type=int, default=3,
                    help="并发进程数。实测 3 最优(P=5 失败率上升反而更慢),勿轻易调高")
    ap.add_argument("--force", action="store_true", help="忽略幂等,重拉覆盖")
    a = ap.parse_args()
    return run(a.start, a.end, universe=a.universe, limit=a.limit,
               procs=a.procs, force=a.force)


if __name__ == "__main__":
    raise SystemExit(main())
