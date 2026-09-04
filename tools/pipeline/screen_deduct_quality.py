"""扣非质量主筛(#31)全 A 规则型 Screener —— 纯扣非质量横截面排序召回。

复用 `tools.strategy.deduct_quality.combo_deduct_quality_screen`(策略「扣非质量」)——
5 维(扣非增速/扣非占归母/现金含量/毛利率/质量领先度)winsor+zscore、缺维重归一等权复合、
可交易性过滤、取 TopK。设计文档:docs/计划/2026-09-04_扣非质量主筛_设计.md。⚠️ 非投资建议。

数据依赖(仿 screen_semi_factor,但全 A、不限池):
  · financial.derived(4 核心维):analysis.financial.analyzer.build_financial_block(code, as_of).derived
    (读 financial_report 三大表 raw + metrics.compute_derived;disclosure_date ≤ as_of 才可见)
  · 质量领先度(跨期 N 期均值):本管线读多期 raw(load_financial)现算 = mean(扣非增速 − 归母增速),
    **防未来函数锚 disclosure_date**(只取披露日 ≤ as_of 的报告期)→ 注入 record['扣非质量']['质量领先度']
  · snapshot(pct_chg / amount / close):kline 最后一根(成交额门 + 停牌判定)
  · valuation(mktcap_yi):fundamental.load_fundamental(code)['总市值'](亿元;流通市值下限的兜底口径)
  · 次新过滤:kline 根数 < 次新门槛 → 剔(管线侧,策略函数看不到 kline)

设计权衡:全 A 逐票读三大表成本高,故 skip-if-cached 补采(幂等,只在新披露季实际触网);
  缺 financial_report raw 的票 → derived 全空 → 策略侧"全维缺失"跳过(诚实降级,不硬造)。

入口:`python -m tools.pipeline.screen_deduct_quality
       [--codes ...|--universe N] [--date D] [--no-fetch] [--top-k K]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.financial import analyzer as fr_analyzer
from tools.analysis.financial import metrics as fr_metrics
from tools.collectors import financial as fin
from tools.collectors import fundamental as fd
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy.deduct_quality import combo_deduct_quality_screen

logger = logging.getLogger("pipeline.screen_deduct_quality")

_CFG = THRESHOLDS.get("扣非质量", {})
DEFAULT_TOP_K = int(_CFG.get("top_k", 30))
N_PERIODS = int(_CFG.get("多期N", 5))
MIN_LISTING_DAYS = int((_CFG.get("流动性") or {}).get("次新_上市最少天数", 120))


def quality_lead_asof(code: str, as_of: str | None, n: int = N_PERIODS) -> float | None:
    """跨期"扣非快于归母"领先度 = 近 n 个**已披露**报告期 (扣非增速 − 归母增速) 的均值。

    读多期 financial_report raw → metrics.compute_derived(全期,YoY 需全期上下文)→
    只保留 disclosure_date ≤ as_of 的报告期(防未来函数)→ 按报告期降序取 n 期 → 均值。
    缺 raw / 无可用期 / 全期两增速都缺 → None(不塌缩成 0,交策略侧当缺维重归一)。
    """
    try:
        raw = fin.load_financial(code)
    except FileNotFoundError:
        return None
    periods_raw = (raw or {}).get("periods") or {}
    if not periods_raw:
        return None
    derived_all = fr_metrics.compute_derived(periods_raw)
    leads: list[tuple[str, float]] = []
    for p, rec in periods_raw.items():
        disc = rec.get("disclosure_date")
        if as_of is not None and disc is not None and disc > as_of:
            continue                                  # 未披露不可见
        d = derived_all.get(p) or {}
        kf, gm = d.get("扣非净利增速"), d.get("归母净利增速")
        if isinstance(kf, (int, float)) and isinstance(gm, (int, float)):
            leads.append((p, float(kf) - float(gm)))
    if not leads:
        return None
    leads.sort(key=lambda x: x[0], reverse=True)      # 报告期降序,取最近 n 期
    recent = [v for _, v in leads[:max(1, n)]]
    return sum(recent) / len(recent)


def _load_kline(code: str, fetch: bool):
    """读近端 kline(缺则可选补采);返回 DataFrame | None。"""
    try:
        kdf = market.load_kline_recent(code)
    except FileNotFoundError:
        kdf = None
    except Exception:                                 # noqa: BLE001
        kdf = None
    if (kdf is None or len(kdf) == 0) and fetch:
        try:
            kdf = market.fetch_kline([code]).get(code)
        except Exception:                             # noqa: BLE001
            kdf = None
    return kdf


def _snapshot_from_kline(kdf) -> dict | None:
    """kline 最后一根 → snapshot{pct_chg, amount, close}。无 kline → None(停牌/无快照)。"""
    if kdf is None or len(kdf) == 0:
        return None
    last = kdf.iloc[-1]

    def _g(col):
        if col in kdf.columns:
            v = last[col]
            return float(v) if pd.notna(v) else None
        return None

    return {"pct_chg": _g("pct_chg"), "amount": _g("amount"), "close": _g("close")}


def _build_record(code: str, as_of: str, fetch: bool) -> tuple[dict | None, str | None]:
    """拼装最小 record;返回 (record | None, 跳过原因 | None)。

    record = {meta{code,name}, snapshot{pct_chg,amount,close}, valuation{mktcap_yi},
              financial{derived}, 扣非质量{质量领先度}}。
    次新(kline 根数不足)在此剔除;缺财报 derived 交策略侧判"全维缺失"。
    """
    kdf = _load_kline(code, fetch)
    if kdf is None or len(kdf) == 0:
        return None, "无K线"
    if len(kdf) < MIN_LISTING_DAYS:
        return None, "次新(历史不足)"
    snap = _snapshot_from_kline(kdf)

    # financial.derived(4 核心维)
    try:
        fin_block = fr_analyzer.build_financial_block(code, as_of=as_of)
    except Exception:                                 # noqa: BLE001
        fin_block = None
    derived = (fin_block or {}).get("derived") or {}

    # 质量领先度(跨期均值,防未来函数)
    lead = quality_lead_asof(code, as_of)

    # valuation(总市值,亿元)——缺则留空,可交易性门用成交额兜底
    mktcap_yi = None
    try:
        fund = fd.load_fundamental(code)
    except FileNotFoundError:
        fund = None
    if fund is None and fetch:
        try:
            fund = (fd.fetch_fundamental([code]) or {}).get(code)
        except Exception:                             # noqa: BLE001
            fund = None
    if fund and isinstance(fund.get("总市值"), (int, float)) and fund["总市值"] > 0:
        mktcap_yi = float(fund["总市值"])

    name = (fin_block or {}).get("name")
    rec = {
        "meta": {"code": code, "name": name, "n_bars": len(kdf)},
        "snapshot": snap,
        "valuation": {"mktcap_yi": mktcap_yi},
        "financial": {"derived": derived},
        "扣非质量": {"质量领先度": lead},
    }
    return rec, None


def run_deduct_quality_screen(codes: list[str], as_of: str | None = None,
                              fetch: bool = True,
                              top_k: int = DEFAULT_TOP_K) -> dict:
    """扫 codes(全 A)→ 补财报/建 record → 调策略「扣非质量」→ 落 view「扣非质量」。返回 view。

    fetch=True:缺 financial_report 三大表 skip-if-cached 补采;False 只读缓存(离线复算,不触网)。
    次新 / 无 kline 的票在管线侧剔(记跳过);缺财报 derived 的票由策略侧"全维缺失"跳过。诚实降级。
    """
    if as_of:
        store.set_active_date(as_of)
    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")

    # —— 先自采财报三大表(skip-if-cached,幂等;缺则策略侧全维缺失跳过)——
    if fetch and codes:
        need_fin = []
        for code in codes:
            try:
                fin.load_financial(code)
            except FileNotFoundError:
                need_fin.append(code)
        if need_fin:
            logger.info("扣非质量:补采财报三大表 %d 只(缓存命中 %d 只跳过)",
                        len(need_fin), len(codes) - len(need_fin))
            try:
                fin.fetch_financial(need_fin)
            except Exception as e:                    # noqa: BLE001
                logger.warning("财报三大表补采失败(降级逐票判): %s", e)

    records: dict[str, dict] = {}
    skip_pre: dict[str, int] = {}
    for code in codes:
        rec, reason = _build_record(code, as_of=as_of, fetch=fetch)
        if rec is None:
            skip_pre[reason] = skip_pre.get(reason, 0) + 1
            continue
        records[code] = rec

    result = combo_deduct_quality_screen(records, top_k=top_k)

    # 合并管线侧 + 策略侧跳过
    skip_all = dict(skip_pre)
    for k, v in (result.get("跳过") or {}).items():
        skip_all[k] = skip_all.get(k, 0) + v

    selected: list[dict] = []
    picks = set(result.get("codes") or [])
    for d in result.get("因子明细", []):
        if d["code"] not in picks:
            continue
        selected.append({
            "code": d["code"],
            "name": (records.get(d["code"], {}).get("meta") or {}).get("name"),
            "组合": ["扣非质量"],
            "明细": d,
        })

    view = {
        "as_of": as_of,
        "策略": "扣非质量(#31·纯扣非质量横截面排序主筛)",
        "present": len(selected) > 0,
        "口径": ("全 A 按利润质量召回(与位置型正交):扣非增速/扣非占归母/现金含量/毛利率/"
                 "质量领先度(扣非增速−归母增速·近%d期均值),各维 winsor+zscore、缺维重归一等权复合、"
                 "取 Top%d;可交易性门=剔 ST/停牌/次新 + 成交额或流通市值下限。⚠️非投资建议。"
                 % (N_PERIODS, top_k)),
        "扫描数": len(codes),
        "有效样本": result.get("有效样本", len(records)),
        "跳过": skip_all,
        "入选数": len(selected),
        "top_k": top_k,
        "权重": result.get("权重"),
        "参数": result.get("参数"),
        "入选清单": selected,
        "复用": "tools.strategy.deduct_quality.combo_deduct_quality_screen(records)",
        "防未来函数": ("financial.derived / 质量领先度 均基于已披露报告期(disclosure_date ≤ as_of);"
                     "次新按 kline 根数(<%d 剔)" % MIN_LISTING_DAYS),
    }
    if result.get("note"):
        view["note"] = result["note"]
    p = store.put_view("扣非质量", view)
    logger.info("扣非质量:扫描 %d / 有效 %d / 入选 %d → %s",
                len(codes), view["有效样本"], view["入选数"], p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="扣非质量(#31)全A横截面排序主筛")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"取前 K 只(默认 {DEFAULT_TOP_K})")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("扣非质量 扫描:%d 只(日期 %s,fetch=%s,top_k=%d)",
                len(codes), as_of, not a.no_fetch, a.top_k)
    v = run_deduct_quality_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
