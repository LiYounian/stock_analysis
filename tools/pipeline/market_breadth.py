"""全市场收盘口径节点(分析师流水线的**确定性节点**:收盘后把当日全A横截面口径落盘)。

## 为什么有这个脚本(2026-09-03)

盘尾复盘要用 **α 口径**给选股记分(个股涨跌 − 全市场基准),但"当日收盘的全市场口径"没有
现成产出:09-03 那次是盘尾任务**现场发约 105 个批请求**对 5214 只票临时复算 mean_pct /
中位数 / 净广度 / 分位;而 `data/analysis/<日>/market_forecast.json` 要等 18:36 选股任务之后
才有,且其中的广度是"**当日之前**"的特征,不是当日收盘实况。

治本:与 10:30 盘中快照同一模式的第二个确定性节点——收盘后 15:05 由 launchd 批量拉全A
收盘价、算聚合口径落盘,盘尾任务直接读,不再现场复算(见 docs/计划/09-03复盘反哺排期.md §2)。

## 口径单一真源(最重要的约束)

"全A等权(mean_pct = 大盘预测的 proxy)"只有一个定义处:
`tools.analysis.market_forecast.breadth.cross_section_stats`(2026-09-03 从历史广度聚合器里抽出的
纯函数)。历史回测侧 `breadth.compute_breadth` → `features.build_proxy_index` 与本节点**调的是
同一个函数**,不许各写一份;涨跌停家数同理走 `breadth.is_limit_hit`(同一板块限价+封板启发式)。
理由:若"全A等权"出现两个互相漂移的定义,大盘预测的 β 基准与盘尾 α 记分的基准会对不上账,
比没有这个节点更糟。

## 契约

输入:
  · 票池 = `store.list_master_codes()`(即 `data/master/kline` 全A主档,与历史广度聚合器同源)
    **默认含北交所**(与历史 `compute_breadth` 票池完全一致);`--exclude-bj` 可排除。
  · 行情源 = 腾讯 gtimg,复用 `tools.collectors.gtimg_quote`(采集层单一真源,批量);
    指数抓取复用 `tools.pipeline.intraday_snapshot.fetch_indices`(不二次实现)。
  · 历史补跑(`--date` 非今天)= **只读本地 `data/master/kline`**,绝不拿今天的实时价冒充那天的收盘。
输出:`data/breadth/<YYYY-MM-DD>.json`,见 `build_payload()` 的结构说明。

纪律:
  · **防未来函数**:`captured_at` 写**真实抓取时刻**;`--date` 只用于补跑历史且强制走本地 K线。
  · **幂等**:文件已存在 → 不覆盖、退出 0(除 `--force`)。
  · **显式降级**:取样率 < `MIN_COVERAGE` → `degraded=true` + `degrade_reasons`,**不悄悄给
    一个不可信的均值**;抓取时点早于收盘同样标注。
  · **全挂不落盘**:一只都没取到 → 不写文件、非 0 退出(下游按"缺文件"降级)。
  · **非交易日**:launchd 只认周一~周五,节假日照样触发 → 本脚本自判交易日,跳过退 0。

用法:
    python -m tools.pipeline.market_breadth --slot 1505
    python -m tools.pipeline.market_breadth --slot 1505 --force
    python -m tools.pipeline.market_breadth --slot testrun            # 联调:名义时刻未知→drift=None
    python -m tools.pipeline.market_breadth --date 2026-09-02         # 历史补跑(读本地K线)

⚠️ 测试环境研究用,非投资建议;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

from tools.analysis.market_forecast import breadth as B
from tools.collectors import calendar as cal
from tools.collectors import gtimg_quote
from tools.config import settings
from tools.pipeline.intraday_snapshot import (
    _write_atomic,          # 原子落盘(先 .tmp 再 rename)——与盘中快照共用,不二次实现
    fetch_indices,          # 指数抓取(前缀路由 + errors 口径)——同上
    slot_nominal_dt,        # slot 名义时刻解析(HHMM;其余名视为"名义时刻未知")
)

logger = logging.getLogger("pipeline.market_breadth")

SCRIPT_VERSION = "1.0.0"
SOURCE_REALTIME = "qt.gtimg.cn"
SOURCE_KLINE = "data/master/kline"

#: 风格背离对照用的指数(6 位代码 → 名称)。比盘中快照多一个中证1000(小盘对照)。
INDEX_CODES: dict[str, str] = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
}

#: 取样率下限:低于此值不再当"全市场口径"用,产出显式标 degraded(诚实降级,不静默)。
MIN_COVERAGE = 0.90

#: 收盘时刻(本地):抓取时点早于此 → 标注"非收盘口径"。
CLOSE_HHMM = (15, 0)

OUT_ROOT = settings.PROJECT_ROOT / "data" / "breadth"
LOG_PATH = settings.PROJECT_ROOT / "logs" / "market_breadth.log"


# ────────────────────────────── 票池 ──────────────────────────────

def resolve_universe(include_bj: bool = True) -> tuple[list[str], dict]:
    """全A票池 = `store.list_master_codes()`(= data/master/kline 主档),返回 (codes, 来源明细)。

    与历史广度聚合器 `breadth.compute_breadth` **同一票池取法**(它也用 list_master_codes),
    **默认含北交所,与历史侧票池完全一致**——这是"单一真源"的必要条件:同一个 `cross_section_stats`
    函数若喂两个不同票池,等权基准仍是两条互相漂移的序列,α 记分与大盘预测就对不上账。
    (首版曾默认排除,理由是 gtimg 前缀推断把 920xxx 判成 sh 取不到数;该 bug 已在
    `gtimg_quote.market_prefix` 修掉——920 段先判为 bj,故不再需要靠排除来规避。)
    `include_bj=False` 仅用于对账历史上"排除北交所"口径的旧数字。

    (历史遗留说明)口径差已消除:历史侧 `compute_breadth` 的票池含北交所,本节点默认亦含 → 当日
    mean_pct 在票池上差约 6%(338/5553)。这是既有实现的口径,本节点不擅自改历史;
    差异写在 meta.universe.note 里,别当"两套定义"。
    """
    from tools.store import repo as store
    all_codes = store.list_master_codes()
    bj = [c for c in all_codes if B.board_of(c) == "北交所"]
    codes = all_codes if include_bj else [c for c in all_codes if B.board_of(c) != "北交所"]
    meta = {
        "source": "tools.store.repo.list_master_codes",
        "master_n": len(all_codes),
        "bj_n": len(bj),
        "include_bj": bool(include_bj),
        "n": len(codes),
        "note": ("含北交所(默认;与 compute_breadth / 大盘预测 proxy 同一票池)" if include_bj else
                 "排除北交所(仅用于对账 2026-09-03 及更早的旧口径数字);"
                 "历史侧 compute_breadth 票池含北交所,差 %d/%d 只"
                 % (len(bj), len(all_codes))),
        # 口径切换留痕:防止下次复盘把 0.1pp 量级的口径差当成策略效应
        "口径变更": ("2026-09-04 起票池含北交所(%d 只 = %.1f%% 票池)。2026-09-03 及更早的盘尾现场复算"
                     "因 920 段前缀 bug 整段丢数、实为'排除北交所'口径(09-03 实测 mean_pct -0.305%% vs "
                     "含北交所 -0.422%%,差 0.117pp)。**跨这条线的 α 数字不可直接相减**——对账旧口径请用 "
                     "--exclude-bj 重算,或明确标注口径切换点。"
                     % (len(bj), 100.0 * len(bj) / max(len(all_codes), 1))),
    }
    return codes, meta


def _symbols_for(codes: list[str]) -> list[str]:
    """代码 → gtimg symbol。北交所(92/83/87/43…)强制 bj 前缀,其余走采集层的前缀推断。"""
    out = []
    for c in codes:
        pre = "bj" if B.board_of(c) == "北交所" else gtimg_quote.market_prefix(c)
        out.append(f"{pre}{c}")
    return out


# ────────────────────────────── 取数(两种源) ──────────────────────────────

def fetch_universe_realtime(codes: list[str]) -> tuple[dict[str, dict], list[dict]]:
    """实时报价(收盘后 = 当日收盘价)→ ({code: 字段}, errors)。

    分批与重试由 `gtimg_quote.fetch_symbols` 负责(单一真源);**单票缺失只进 errors**,
    整批异常也只记 errors(由上层判"全挂"),不抛。
    """
    if not codes:
        return {}, []
    try:
        got = gtimg_quote.fetch_symbols(_symbols_for(codes))
    except Exception as e:
        logger.error("全A报价抓取失败(整批):%s: %s", type(e).__name__, e)
        return {}, [{"scope": "code", "code": c, "reason": f"{type(e).__name__}: {e}"}
                    for c in codes]
    out: dict[str, dict] = {}
    errors: list[dict] = []
    for c in codes:
        q = got.get(c)
        if not q or q.get("price") is None:
            errors.append({"scope": "code", "code": c,
                           "reason": "源方无数据(停牌/退市/代码异常)" if not q else "现价缺失"})
            continue
        out[c] = {"code": c, **q}
    return out, errors


def fetch_universe_kline(codes: list[str], date: str) -> tuple[dict[str, dict], list[dict]]:
    """历史补跑:从本地 `data/master/kline` 取 `date` 那天的收盘行,拼成与实时同构的字段。

    **防未来函数**:历史日期绝不允许拿当前实时价冒充,故补跑只走这条路径。
    该票当天无 K线行(停牌/未上市)→ 进 errors。
    """
    from tools.store import repo as store
    out: dict[str, dict] = {}
    errors: list[dict] = []
    for c in codes:
        try:
            df = store.get_master_kline(c)
        except Exception as e:
            errors.append({"scope": "code", "code": c, "reason": f"主档不可读: {e}"})
            continue
        try:
            d = df[df["date"].astype(str).str.slice(0, 10) == date]
        except Exception as e:                          # 列缺失等异常票,不阻断整体
            errors.append({"scope": "code", "code": c, "reason": f"K线异常: {e}"})
            continue
        if d.empty:
            errors.append({"scope": "code", "code": c, "reason": "该日无K线(停牌/未上市/已退市)"})
            continue
        row = d.iloc[-1]
        def _g(k):
            v = row.get(k)
            try:
                return None if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
            except Exception:
                return None
        out[c] = {"code": c, "name": None, "price": _g("close"), "prev_close": None,
                  "open": _g("open"), "high": _g("high"), "low": _g("low"),
                  "volume": _g("volume"), "pct_chg": _g("pct_chg"),
                  "quote_time": date}
    return out, errors


def _index_from_kline(date: str) -> tuple[dict[str, dict], list[dict]]:
    """历史补跑的指数口径:尽力从 `data/raw/*/index_kline/<code>.parquet` 取该日涨跌。

    本地只常备沪深300 → 其余指数进 errors(下游按"缺该指数"降级,不静默补 0)。
    """
    # 局部 import:只有历史补跑才用到 glob/pandas,常规收盘路径不付这份代价
    import glob

    import pandas as pd
    out: dict[str, dict] = {}
    errors: list[dict] = []
    for code, alias in INDEX_CODES.items():
        paths = sorted(glob.glob(str(settings.PROJECT_ROOT / "data" / "raw" / "*" /
                                     "index_kline" / f"{code}.parquet")))
        row = None
        for p in reversed(paths):                       # 从最新快照往回找
            try:
                df = pd.read_parquet(p)
            except Exception:
                continue
            d = df[df["date"].astype(str).str.slice(0, 10) == date]
            if not d.empty:
                row = d.iloc[-1]
                break
        if row is None:
            errors.append({"scope": "index", "code": code, "reason": "本地无该日指数K线"})
            continue
        out[code] = {"code": code, "alias": alias,
                     "price": float(row.get("close")) if row.get("close") is not None else None,
                     "pct_chg": (float(row["pct_chg"]) if "pct_chg" in row
                                 and row["pct_chg"] is not None else None),
                     "quote_time": date}
    return out, errors


# ────────────────────────────── 聚合(走单一真源) ──────────────────────────────

def quote_pcts(quotes: dict[str, dict]) -> tuple[np.ndarray, int]:
    """报价 dict → 涨幅(%)数组 + "由现价/昨收推算"的只数。

    优先照抄源方 `pct_chg`(与 web 盯盘/盘中快照同口径,不二次加工);源方缺 pct_chg 时才用
    (price/prev_close − 1)×100 兜底,并计数(进 meta,让下游知道有多少只是推算的)。
    """
    vals: list[float] = []
    derived = 0
    for q in quotes.values():
        p = q.get("pct_chg")
        if p is None:
            pc, pv = q.get("prev_close"), q.get("price")
            if pc and pv is not None:
                p = (float(pv) / float(pc) - 1.0) * 100.0
                derived += 1
            else:
                p = float("nan")
        vals.append(float(p))
    return np.asarray(vals, dtype=float), derived


def count_limits(quotes: dict[str, dict]) -> tuple[int, int, str]:
    """涨停/跌停家数(启发式)+ 判据说明。逐票调 `breadth.is_limit_hit`(与历史聚合同一函数)。"""
    lu = ld = 0
    for c, q in quotes.items():
        close, high, low, pct = q.get("price"), q.get("high"), q.get("low"), q.get("pct_chg")
        if pct is None and q.get("prev_close"):
            pct = (float(q["price"]) / float(q["prev_close"]) - 1.0) * 100.0
        if B.is_limit_hit(c, pct, close, high, low, up=True):
            lu += 1
        elif B.is_limit_hit(c, pct, close, high, low, up=False):
            ld += 1
    rule = ("启发式:板块限价路由(主板10%/创业·科创20%/北交所30%/主板ST 5%)+ 封板"
            "(涨停 close≥high×(1−封板容差),跌停 close≤low×(1+封板容差))+ |pct∓限价|≤涨停容差;"
            "阈值取 config THRESHOLDS['大盘预测'];实现 = breadth.is_limit_hit(与历史广度同一函数)")
    return lu, ld, rule


def aggregate(quotes: dict[str, dict], universe_n: int) -> dict:
    """横截面聚合。**全A等权 mean_pct 由 `breadth.cross_section_stats` 唯一定义**,本函数不自算。

    注意分母口径:`total` 传**实际取到的只数**(len(quotes)),不是票池只数——取不到的票
    (停牌/退市)在历史聚合里也不进当日 `listed`,与 `compute_breadth` 一致。
    """
    pct, derived = quote_pcts(quotes)
    stats = B.cross_section_stats(pct, total=len(quotes))
    lu, ld, rule = count_limits(quotes)
    return {
        "universe_n": universe_n,
        "sampled_n": len(quotes),
        "missing_n": universe_n - len(quotes),
        "coverage": (len(quotes) / universe_n) if universe_n else 0.0,
        "mean_pct": stats["mean_pct"],
        "median_pct": stats["median_pct"],
        "up_count": stats["adv"],
        "down_count": stats["dec"],
        "flat_count": stats["flat"],
        "net_breadth": stats["net_adv"],
        "pct_quantiles": stats["quantiles"],
        "limit_up_n": lu,
        "limit_down_n": ld,
        "limit_rule": rule,
        "pct_derived_n": derived,
    }


# ────────────────────────────── 组装与落盘 ──────────────────────────────

def breadth_path(date: str) -> Path:
    """产出路径 data/breadth/<date>.json(一天一份;slot 写在文件内容里)。"""
    return OUT_ROOT / f"{date}.json"


def _degrade_reasons(agg: dict, captured_at: datetime, mode: str,
                     min_coverage: float) -> list[str]:
    """降级理由清单(空 = 不降级)。任何降级都必须显式落在产出里,不许静默失效。"""
    out: list[str] = []
    if agg["coverage"] < min_coverage:
        out.append("取样率 %.1f%% < 阈值 %.0f%%:mean_pct/中位数/净广度不可当全市场口径用"
                   % (agg["coverage"] * 100, min_coverage * 100))
    if mode == "realtime" and (captured_at.hour, captured_at.minute) < CLOSE_HHMM:
        out.append("抓取时点 %s 早于收盘 %02d:%02d:这是盘中截面,不是收盘口径"
                   % (captured_at.strftime("%H:%M"), *CLOSE_HHMM))
    if agg["sampled_n"] and agg["pct_derived_n"] / agg["sampled_n"] > 0.05:
        out.append("涨幅推算占比 %.1f%%>5%%(源方 pct_chg 缺失偏多)"
                   % (agg["pct_derived_n"] / agg["sampled_n"] * 100))
    return out


def build_payload(date: str, slot: str, captured_at: datetime, mode: str,
                  agg: dict, indices: dict[str, dict], errors: list[dict],
                  universe_meta: dict, min_coverage: float = MIN_COVERAGE) -> dict:
    """产出 JSON。

    结构:
      date/slot                 —— 口径日与时点槽位(1505=收盘后 5 分钟)
      captured_at               —— **真实抓取时刻**(ISO 带时区),防未来函数的锚
      nominal_at/drift_seconds  —— slot 名义时刻与偏差秒(正=晚到);slot 非 HHMM → None
      universe_n/sampled_n/missing_n/coverage —— 票池只数 / 实际取到 / 缺样 / 取样率
      mean_pct                  —— **全A等权(= 大盘预测 proxy 的 mean_pct)**,单位 %
      median_pct                —— 全市场中位涨幅 %
      up_count/down_count/flat_count/net_breadth —— 涨跌平家数与净广度 (涨−跌)/取到只数
      pct_quantiles             —— P10/P25/P75/P90 涨幅分位 %
      limit_up_n/limit_down_n/limit_rule —— 涨跌停家数(启发式)与判据
      indices                   —— 上证/深成/创业板/沪深300/中证500/中证1000 收盘涨跌(风格背离对照)
      degraded/degrade_reasons  —— 显式降级标记(取样率不足 / 非收盘时点 / 推算占比过高)
      errors                    —— 失败明细 [{scope, code, reason}](单票失败只到这里)
      meta                      —— source/script_version/**proxy 定义出处**/票池口径/阈值
    """
    nominal = slot_nominal_dt(date, slot)
    drift = int(round((captured_at - nominal).total_seconds())) if nominal else None
    reasons = _degrade_reasons(agg, captured_at, mode, min_coverage)
    return {
        "date": date,
        "slot": slot,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "nominal_at": nominal.isoformat(timespec="seconds") if nominal else None,
        "drift_seconds": drift,
        "universe_n": agg["universe_n"],
        "sampled_n": agg["sampled_n"],
        "missing_n": agg["missing_n"],
        "coverage": round(agg["coverage"], 6),
        "mean_pct": agg["mean_pct"],
        "median_pct": agg["median_pct"],
        "up_count": agg["up_count"],
        "down_count": agg["down_count"],
        "flat_count": agg["flat_count"],
        "net_breadth": agg["net_breadth"],
        "pct_quantiles": agg["pct_quantiles"],
        "limit_up_n": agg["limit_up_n"],
        "limit_down_n": agg["limit_down_n"],
        "limit_rule": agg["limit_rule"],
        "indices": indices,
        "degraded": bool(reasons),
        "degrade_reasons": reasons,
        "errors": errors,
        "meta": {
            "source": SOURCE_REALTIME if mode == "realtime" else SOURCE_KLINE,
            "mode": mode,
            "script": "tools.pipeline.market_breadth",
            "script_version": SCRIPT_VERSION,
            # ⚠️ 全A等权(proxy)的**唯一定义处**:历史回测与本节点调同一函数
            "proxy_definition": B.CROSS_SECTION_SOURCE,
            "proxy_definition_note": (
                "mean_pct = 横截面涨幅算术平均(等权),与 features.build_proxy_index 累乘成的"
                "全A等权代理指数同源;涨跌停走 breadth.is_limit_hit 同一启发式"),
            "universe": universe_meta,
            "index_codes": dict(INDEX_CODES),
            "min_coverage": min_coverage,
            "pct_field": "源方 pct_chg 优先,缺失时用 (现价/昨收−1)×100 兜底并计数 pct_derived_n",
            "codes_err_n": len([e for e in errors if e.get("scope") == "code"]),
            "indices_ok_n": len(indices),
            "note": "当日全市场收盘口径,供盘尾 α 记分作基准;非投资建议",
        },
    }


def run(slot: str = "1505", *, date: str | None = None, force: bool = False,
        include_bj: bool = True, min_coverage: float = MIN_COVERAGE) -> int:
    """跑一次全市场口径。返回进程退出码(0=成功/跳过,1=全挂或不可用)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    date = date or today

    if not cal.is_trading_day(date):
        logger.info("跳过:%s 非 A 股交易日(slot=%s)", date, slot)
        return 0

    out = breadth_path(date)
    if out.exists() and not force:
        logger.info("跳过:口径文件已存在,不覆盖 %s(要重算加 --force)", out)
        return 0

    codes, universe_meta = resolve_universe(include_bj)
    if not codes:
        logger.error("票池为空(data/master/kline 无主档?),不落文件、非 0 退出")
        return 1

    # 防未来函数:只有"目标日 = 今天"才允许用实时报价;历史补跑强制读本地 K线
    mode = "realtime" if date == today else "kline"
    logger.info("开始:date=%s slot=%s mode=%s 票池=%d 只(北交所 %s)",
                date, slot, mode, len(codes), "含" if include_bj else "排除")

    captured_at = datetime.now().astimezone()          # 真实抓取时刻(抓取前取,不事后编)
    if mode == "realtime":
        quotes, code_errors = fetch_universe_realtime(codes)
        indices, index_errors = fetch_indices(INDEX_CODES)
    else:
        quotes, code_errors = fetch_universe_kline(codes, date)
        indices, index_errors = _index_from_kline(date)
    errors = [*code_errors, *index_errors]

    if not quotes:
        logger.error("全部标的取数失败(%d 条错误),不落文件、非 0 退出", len(errors))
        return 1

    agg = aggregate(quotes, len(codes))
    payload = build_payload(date, slot, captured_at, mode, agg, indices,
                            errors, universe_meta, min_coverage)
    _write_atomic(out, payload)
    logger.info("落盘 %s:取样 %d/%d(%.2f%%)mean=%.4f%% 中位=%.4f%% 净广度=%.4f "
                "涨%d/跌%d/平%d 涨停%d/跌停%d 指数%d degraded=%s",
                out, agg["sampled_n"], agg["universe_n"], agg["coverage"] * 100,
                agg["mean_pct"], agg["median_pct"], agg["net_breadth"],
                agg["up_count"], agg["down_count"], agg["flat_count"],
                agg["limit_up_n"], agg["limit_down_n"], len(indices),
                payload["degraded"])
    if payload["degraded"]:
        for r in payload["degrade_reasons"]:
            logger.warning("降级:%s", r)
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def _setup_logging() -> None:
    """logs/market_breadth.log + stderr 双写(命名空间 pipeline.market_breadth;
    采集层为 collectors.gtimg_quote / collectors.retry)。"""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="全市场收盘口径(确定性节点):收盘后算当日全A等权/中位/广度/分位落盘")
    ap.add_argument("--slot", default="1505", help="时点槽位 HHMM(默认 1505);非 HHMM → 名义时刻 None")
    ap.add_argument("--date", default=None,
                    help="口径日 YYYY-MM-DD(默认今天);非今天 = 历史补跑,强制读本地 K线")
    ap.add_argument("--force", action="store_true", help="覆盖已有口径文件")
    ap.add_argument("--exclude-bj", action="store_true",
                   help="排除北交所(仅用于对账历史'排除北交所'口径的旧数字;默认含,与历史票池一致)")
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE,
                    help="取样率下限,低于则标 degraded(默认 %.2f)" % MIN_COVERAGE)
    args = ap.parse_args(argv)
    _setup_logging()
    try:
        return run(args.slot, date=args.date, force=args.force,
                   include_bj=not args.exclude_bj, min_coverage=args.min_coverage)
    except Exception as e:                              # 兜底:异常进日志,退出码非 0
        logger.exception("全市场口径任务异常退出:%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
