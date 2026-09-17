"""午盘 Q · M3.b 分时数据采集(baostock 5min · 全A 排北交所 · 可断点续跑)。

## 为什么有这个脚本(2026-09-17)

`_数据源实测.md`(09-07)结论是"个股价格分时·历史回测 = ❌ 无历史源",
当时测了 akshare / 东财 push2 / mootdx 三条路,**漏测了 baostock 的分钟频**。
09-17 复测发现 baostock `frequency="5"` 可用且历史至少回溯到 2023 年,
**14:30 与 14:50 两个时刻精确存在**(正是午盘Q首判/复核判定点)。

→ M3.b 分时回测**不必等 60 个交易日未来采样**,Q1/Q2 立刻可做真回测。
   (Q3 仍需等:东财 fflow/kline 只返当天资金流,`lmt` 给多大都拿不到历史。)

## 四个必须处理的 baostock 坑(实测数据)

**① 长会话会被掐断** —— v1 用"单次 login 跑完 126 只"的写法,跑到第 51 只
(约 18 分钟)后**连续 73 只全报无数据**;那些票单独用新会话测完全正常
(n=1392)。→ 本版**每票独立 login/logout**。

**② 并发过高会静默截断** —— 6 进程并行实测失败率 33%~67%,且失败模式是
`error_code=10001001` 配 **n=2000 的不完整数据**(不抛异常!)。
若不校验,坏数据会静默混进回测。→ 本版**按预期行数校验**,不足即判失败重试。
实测最优并发:P=3 → 12/12 成功、2.0s/只;P=5 → 11/12、2.5s/只(反而更慢)。

**③ 幂等必须校验内容而非仅存在性** —— 文件存在 ≠ 内容完整(见坑②)。
→ `_ok_on_disk()` 同时校验区间覆盖(带 10 天容差)与每日 bar 数。

**④ 单次返回约 2000 行硬上限 → 长区间被静默截断**
一次性请求 2026-01-02→09-10(168 交易日)时,**658/3210 只票**的交易日数精确
聚集在 **42 / 84 / 125**(= 168 的 1/4、1/2、3/4),起始日全部正确(2026-01-05)
→ 典型"从正确起点取到一部分就断",且 `error_code` 仍返 0。
换算:我们请求的是**整天 48 根**(baostock 不支持按时刻过滤,落盘前才裁成
7 个 KEEP_TIMES),42 日 × 48 ≈ **2016 行** ≈ 压测时见过的 n=2000 截断值。
剔除率跨板块均匀(创16%/沪主17%/深主25%/科创28%)→ 不是某类票的问题。
→ 本版**按 1.5 个月分段拉取**(≈31 交易日≈1500 行,留 25% 余量)再 concat 去重。

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

    ⚠️ ①的"区间已覆盖"用 `max(date) >= end - 容差`:end 常落在非交易日/
    该票停牌日,严格相等会把正常票误判成不完整。容差 10 个自然日足够跨周末+小长假,
    同时仍能拦住 42/84/125 日那种**整段尾部缺失**(坑④,差几十天)。
    """
    if not path.exists():
        return False
    try:
        df = pd.read_parquet(path)
    except Exception:
        return False
    if df.empty:
        return False
    if str(df["date"].min()) > start:
        return False
    end_floor = (pd.Timestamp(end) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
    if str(df["date"].max()) < end_floor:
        return False
    per_day = df.groupby("date").size()
    return bool((per_day >= len(KEEP_TIMES) // 2).all())



def _month_chunks(start: str, end: str, months: float = 1.5) -> list[tuple[str, str]]:
    """把 [start, end] 切成每段约 `months` 个月的子区间。

    ## 为什么必须分段(2026-09-17 实测的坑④)

    baostock 单次返回有**约 2000 行硬上限**,超出就静默截断(`error_code` 仍返 0)。
    我们请求的是**整天 48 根** 5min bar(baostock 不支持按时刻过滤,落盘前才裁成
    7 个 KEEP_TIMES),所以 2000 行 ÷ 48 ≈ **42 个交易日**就到顶。

    实证:2026-01-02→09-10(168 交易日)一次性拉,658/3210 只票的交易日数精确
    聚集在 **42 / 84 / 125**(= 168 的 1/4、1/2、3/4),起始日全部正确
    (2026-01-05)→ 典型"从正确起点取到一部分就断"。

    ## 段长选 1.5 个月(不是 2 个月)

    A 股约 每月 21 个交易日 → 2 个月 ≈ 42 交易日 × 48 根 ≈ **2020 行,正好踩线**。
    1.5 个月 ≈ 31 交易日 ≈ **1500 行**,留 25% 安全余量。
    (别为了少几次请求把段长调大 —— 踩线的代价是静默截断,比多跑几次严重得多。)
    """
    out: list[tuple[str, str]] = []
    cur = pd.Timestamp(start)
    last = pd.Timestamp(end)
    step = pd.Timedelta(days=int(round(months * 30.44)))
    while cur <= last:
        seg_end = min(cur + step - pd.Timedelta(days=1), last)
        out.append((cur.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        cur = seg_end + pd.Timedelta(days=1)
    return out


def _query_segment(bs, code: str, start: str, end: str) -> tuple[list[dict], str]:
    """拉单段。返回 (rows, error_code)。调用方负责会话与重试。"""
    rs = bs.query_history_k_data_plus(
        _bs_code(code),
        "date,time,open,high,low,close,volume,amount",
        start_date=start, end_date=end, frequency="5", adjustflag="3")
    return _parse_rows(rs), rs.error_code


def fetch_one(code: str, start: str, end: str) -> tuple[str, int, int, str]:
    """拉单票(分段)并落盘。返回 (code, 行数, 尝试次数, 状态)。

    三层防护对应三个已实证的 baostock 坑:
      坑① 长会话被掐断      → **每段都新建 login/logout**
      坑② 并发静默截断      → error_code 非 0 一律重试,不落盘
      坑④ 单次约2000行上限  → **按 1.5 个月分段**,段内远离上限;分段后 concat 去重
    """
    import baostock as bs

    out = OUT_DIR / f"{code}.parquet"
    segments = _month_chunks(start, end)
    all_rows: list[dict] = []
    total_attempts = 0

    for seg_start, seg_end in segments:
        seg_rows: list[dict] | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            total_attempts += 1
            try:
                lg = bs.login()
                if lg.error_code != "0":
                    time.sleep(_RETRY_BASE_SEC * attempt)
                    continue
                rows, err = _query_segment(bs, code, seg_start, seg_end)
                try:
                    bs.logout()
                except Exception:
                    pass                    # logout 失败不影响已取到的数据
                if err != "0":
                    time.sleep(_RETRY_BASE_SEC * attempt)
                    continue
                seg_rows = rows             # 空 list 也算有效(该段可能真无交易日)
                break
            except Exception as e:
                logger.debug("%s [%s~%s] 第%d次异常: %s",
                             code, seg_start, seg_end, attempt, e)
                time.sleep(_RETRY_BASE_SEC * attempt)
        if seg_rows is None:
            # 某段彻底失败 → 整票判失败(宁可重取,不落"缺一段"的半份数据)
            return (code, 0, total_attempts, "fail")
        all_rows.extend(seg_rows)

    if not all_rows:
        # 全段皆空:新股未上市/长期停牌/退市 —— 与瞬时失败已由上面的重试区分开
        return (code, 0, total_attempts, "empty")

    df = (pd.DataFrame(all_rows)
            .drop_duplicates(subset=["date", "time"])      # 段边界可能重叠
            .sort_values(["date", "time"])
            .reset_index(drop=True))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    return (code, len(df), total_attempts, "ok")


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

    ok = fail = empty = retried = 0
    failed_codes: list[str] = []
    empty_codes: list[str] = []
    t0 = time.time()
    with Pool(procs, initializer=_init, initargs=(start, end)) as pool:
        for i, (code, n, attempts, status) in enumerate(
                pool.imap_unordered(_work, todo), 1):
            if status == "ok":
                ok += 1
                # 分段后每票至少 len(segments) 次尝试 → 用"超出段数"判是否真重试过
                retried += (attempts > len(_month_chunks(start, end)))
            elif status == "empty":
                # 真无数据(新股未上市/退市/长期停牌)—— 与取数失败分开计,
                # 混在一起会让失败率虚高、掩盖真问题
                empty += 1
                empty_codes.append(code)
            else:
                fail += 1
                failed_codes.append(code)
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t0
                rate = el / i
                eta = rate * (len(todo) - i)
                logger.info("[%d/%d] 成功%d 失败%d 无数据%d (重试救回%d) | %.1fs/只 | "
                            "已用%.0f分 剩约%.0f分",
                            i, len(todo), ok, fail, empty, retried,
                            rate, el / 60, eta / 60)

    el = time.time() - t0
    logger.info("完成:成功 %d / 失败 %d / 无数据 %d / 跳过 %d,共 %d 只,用时 %.0f 分钟",
                ok, fail, empty, done_already, len(codes), el / 60)
    if failed_codes or empty_codes:
        # 清单落盘,便于单独重跑(而不是在几千行日志里捞)
        fp = OUT_DIR.parent / "m3b_fetch_failed.json"
        fp.write_text(json.dumps({"universe": universe, "start": start, "end": end,
                                   "failed": sorted(failed_codes),
                                   "empty_no_data": sorted(empty_codes)},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning("失败 %d 只 / 无数据 %d 只,清单见 %s(重跑同一命令会自动续)",
                       len(failed_codes), len(empty_codes), fp)
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
