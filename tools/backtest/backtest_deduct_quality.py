"""扣非质量(#31)排序型回测 —— rank-IC(5/20/60 日)+ TopN 超额 + 分层单调。

为什么单独写(而非直接用 backtest_rank):`backtest_rank` 的 scorer 是**逐票** `scorer(kdf_slice, code)`,
只拿单票 kline,**看不到横截面**。而本策略是**横截面** winsorize→zscore→缺维重归一等权复合——
标准化必须在"同一天全体票"上做,逐票 scorer 无法表达(等权原始因子 ≠ zscore 复合的排序)。
故本模块补这层"横截面面板构造 glue":每个调仓日对全体票建 as-of 记录 → 调策略函数拿综合分 →
配 kline 前瞻收益,落成 `backtest_rank` 认识的长表 (date, code, score, liq, r_N),
再**复用** `backtest_rank.ic_metrics / decile_metrics / topk_metrics` 出 rank-IC / 分层 / TopN 超额。

达标闸门(设计 §四 / docs/评测方法论_多维评估.md):
  · rank-IC:5 / **20 / 60 日**(基本面因子 5 日多半偏弱,重点看 20/60 日);
    引擎给的是 **t 统计量**(t = ICIR·√有效交易日),|t| ≳ 1.645 ≈ 双侧 p<0.10。
  · TopN 超额:TopK 组合相对全 A 等权基准的超额,显著或绝对为正。
  · 样本下限 MIN_N=30:有效交易日不足 → 只给"观察",不下结论。
达标口径:20 或 60 日 rank-IC 显著为正 且 TopN 超额为正 → 达标(接生产)。

防未来函数:as-of 记录由 analyzer 按 disclosure_date ≤ 调仓日构造 + 跨期领先度锚披露日;
  前瞻收益取 close[t+N]/close[t](t = 调仓日在该票 kline 的位置),严格用未来价、只用于打标签。

⚠️ 非投资建议,研究模拟。数据依赖:历史多报告期 financial_report raw(带 disclosure_date)+ kline;
  缺 financial_report raw(本地 data/raw/financial_report 为空)→ 面板为空,回测阻塞(诚实降级,不硬造)。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import pandas as pd

from tools.backtest import backtest_rank as br
from tools.strategy.deduct_quality import combo_deduct_quality_screen

logger = logging.getLogger("backtest.deduct_quality")

MIN_N = 30            # 有效交易日下限(与 eval_v3 一致);不足只给"观察"
SIG_T = 1.645         # |t| ≥ 此 ≈ 双侧 p<0.10(引擎输出 t,非 p)
DEFAULT_HORIZONS = (5, 20, 60)


# ————————————————————————————————————————————————————————————————
# 一、默认 IO 装载器(可注入替换,便于离线/单测)
# ————————————————————————————————————————————————————————————————
def _default_record_builder(code: str, as_of: str) -> Optional[dict]:
    """默认 as-of 记录构造:financial.derived(analyzer 控披露日)+ 跨期质量领先度。

    缺 financial_report raw → derived 空 + 领先度 None → 该票"全维缺失"(策略侧自剔)。
    """
    from tools.analysis.financial import analyzer as fr_analyzer
    from tools.pipeline.screen_deduct_quality import quality_lead_asof
    try:
        block = fr_analyzer.build_financial_block(code, as_of=as_of)
    except Exception:                                 # noqa: BLE001
        block = None
    derived = (block or {}).get("derived") or {}
    lead = quality_lead_asof(code, as_of)
    return {"meta": {"code": code}, "financial": {"derived": derived},
            "扣非质量": {"质量领先度": lead}}


def _default_kline_loader(code: str):
    from tools.collectors import market
    try:
        return market.load_kline(code)
    except Exception:                                 # noqa: BLE001
        return None


# ————————————————————————————————————————————————————————————————
# 二、横截面面板构造(glue:每调仓日全体票 → 综合分 + 前瞻收益)
# ————————————————————————————————————————————————————————————————
def build_quality_panel(
    codes: list[str],
    rebalance_dates: list[str],
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    record_builder: Optional[Callable[[str, str], Optional[dict]]] = None,
    kline_loader: Optional[Callable[[str], object]] = None,
    winsor_scale: Optional[float] = None,
) -> pd.DataFrame:
    """逐调仓日构造横截面面板:综合分(策略函数,apply_liquidity=False 隔离因子)+ 前瞻收益。

    返回长表 (date, code, score, liq, r_5, r_20, r_60);无未来函数(as-of 记录 + 未来价仅作标签)。
    apply_liquidity=False:回测只测因子 IC/超额,不叠可交易性门(门是组合构造关注点,不改因子排序信息)。
    """
    record_builder = record_builder or _default_record_builder
    kline_loader = kline_loader or _default_kline_loader
    maxN = max(horizons)

    # 预载每票 kline → {code: (dates:list[str], close:np.ndarray, amt:np.ndarray)}
    kl: dict[str, tuple[list[str], np.ndarray, np.ndarray]] = {}
    for code in codes:
        df = kline_loader(code)
        if df is None or len(df) == 0 or "close" not in getattr(df, "columns", []):
            continue
        df = df.reset_index(drop=True)
        close = df["close"].to_numpy(float)
        vol = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
        dates = [str(x)[:10] for x in df["date"].tolist()]
        kl[code] = (dates, close, close * vol)

    rows = []
    kw = {"winsor_scale": winsor_scale} if winsor_scale is not None else {}
    for d in rebalance_dates:
        # 1) 全体票 as-of 记录 → 综合分
        records = {}
        for code in codes:
            rec = record_builder(code, d)
            if rec is not None:
                records[code] = rec
        if len(records) < 2:
            continue
        out = combo_deduct_quality_screen(records, top_k=len(records),
                                          apply_liquidity=False, **kw)
        scores = {row["code"]: row["综合分"] for row in out.get("因子明细", [])}
        if len(scores) < 2:
            continue
        # 2) 配前瞻收益(t = 调仓日在该票 kline 的位置;取 date ≤ d 的最后一根)
        for code, score in scores.items():
            if code not in kl or score is None:
                continue
            dates, close, amt = kl[code]
            t = _pos_asof(dates, d)
            if t is None or t + maxN >= len(close) or close[t] <= 0:
                continue
            liq = float(np.mean(amt[max(0, t - 19): t + 1]))
            row = {"date": d, "code": code, "score": float(score), "liq": liq}
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["dates"] = len(rebalance_dates)
    return panel


def _pos_asof(dates: list[str], as_of: str) -> Optional[int]:
    """as_of 在已排序 dates 中的位置(最后一根 date ≤ as_of);无则 None。"""
    lo, hi, ans = 0, len(dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= as_of:
            ans = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return ans


# ————————————————————————————————————————————————————————————————
# 三、评测 + 达标裁决
# ————————————————————————————————————————————————————————————————
def evaluate(panel: pd.DataFrame, horizons: tuple[int, ...] = DEFAULT_HORIZONS,
             topk: int = 30, min_liq_pct: float = 0.0) -> dict:
    """对面板出 rank-IC / 分层 / TopN 超额(复用 backtest_rank 指标)+ 达标裁决。"""
    if panel is None or panel.empty:
        return {"present": False, "verdict": "阻塞",
                "note": "面板为空(缺历史 financial_report raw / kline 前瞻不足);回测阻塞,不下结论"}
    panel = br._liq_filter(panel, min_liq_pct)
    per_h = {}
    for N in horizons:
        ic = br.ic_metrics(panel, N)
        topn = br.topk_metrics(panel, N, k=topk)
        dec = br.decile_metrics(panel, N)
        per_h[N] = {"rank_IC": ic, "TopN": topn, "分层": dec}

    verdict, reason = _verdict(per_h, horizons)
    return {"present": True, "verdict": verdict, "reason": reason,
            "样本_观测行": int(len(panel)), "调仓日数": int(panel["date"].nunique()),
            "MIN_N": MIN_N, "SIG_T": SIG_T, "topk": topk, "分维度": per_h}


def _verdict(per_h: dict, horizons: tuple[int, ...]) -> tuple[str, str]:
    """达标口径:20 或 60 日 rank-IC 显著为正(|t|≥SIG_T)且 TopN 超额为正 → 达标。

    有效交易日 < MIN_N → 观察;显著为负 → 淘汰;否则 → 观察/迭代。
    """
    key_hs = [h for h in (20, 60) if h in horizons] or list(horizons)
    n_days = max((per_h[h]["rank_IC"].get("有效交易日") or 0) for h in horizons)
    if n_days < MIN_N:
        return "观察", f"有效交易日 {n_days} < MIN_N {MIN_N},样本不足不下结论"
    for h in key_hs:
        ic = per_h[h]["rank_IC"]
        topn = per_h[h]["TopN"]
        t = ic.get("t")
        mean = ic.get("IC均值")
        excess = topn.get("Top超额%")
        if (t is not None and mean is not None and excess is not None
                and mean > 0 and t >= SIG_T and excess > 0):
            return "达标", (f"{h}日 rank-IC={mean}(t={t}≥{SIG_T})显著为正 且 "
                            f"TopN 超额={excess}%>0 → 接生产")
    # 是否显著为负(淘汰)
    for h in key_hs:
        ic = per_h[h]["rank_IC"]
        t, mean = ic.get("t"), ic.get("IC均值")
        if t is not None and mean is not None and mean < 0 and t <= -SIG_T:
            return "淘汰", f"{h}日 rank-IC={mean}(t={t})显著为负"
    return "观察", "关键期(20/60日)rank-IC 未达显著为正 + TopN 超额为正,留观察/迭代因子权重"


def run(codes: list[str], rebalance_dates: list[str],
        horizons: tuple[int, ...] = DEFAULT_HORIZONS, topk: int = 30,
        min_liq_pct: float = 0.0, json_path: Optional[str] = None) -> dict:
    """端到端:建面板 → 评测 → 裁决。返回报告 dict(缺数据时 present=False·阻塞)。"""
    panel = build_quality_panel(codes, rebalance_dates, horizons)
    report = evaluate(panel, horizons=horizons, topk=topk, min_liq_pct=min_liq_pct)
    report["codes数"] = len(codes)
    report["日期数"] = len(rebalance_dates)
    if json_path:
        import json
        from pathlib import Path
        Path(json_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    logger.info("扣非质量回测:裁决=%s;%s", report.get("verdict"),
                report.get("reason") or report.get("note"))
    return report


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="扣非质量(#31)排序型回测 rank-IC + TopN 超额")
    ap.add_argument("--codes", help="逗号分隔代码(不传=主档全A)")
    ap.add_argument("--sample", type=int, help="从主档随机抽 N 只")
    ap.add_argument("--dates", help="逗号分隔调仓日 YYYY-MM-DD")
    ap.add_argument("--horizon", default="5,20,60", help="前瞻期(默认 5,20,60)")
    ap.add_argument("--topk", type=int, default=30)
    ap.add_argument("--min-liq-pct", type=float, default=0.0)
    ap.add_argument("--json", help="报告落盘路径")
    a = ap.parse_args(argv)

    from tools.store import repo as store
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        codes = list(store.list_master_codes())
        if a.sample:
            import random
            random.seed(42)
            codes = random.sample(codes, min(a.sample, len(codes)))
    if not a.dates:
        logger.error("需 --dates(调仓日列表);基本面回测按报告披露节奏取月末/季末较合适")
        return 2
    dates = [d.strip() for d in a.dates.split(",") if d.strip()]
    horizons = tuple(int(x) for x in a.horizon.split(","))
    rep = run(codes, dates, horizons=horizons, topk=a.topk,
              min_liq_pct=a.min_liq_pct, json_path=a.json)
    print(f"裁决={rep.get('verdict')} | {rep.get('reason') or rep.get('note')}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
