"""趋势模板(Minervini 8 条)walk-forward 前瞻回测。

方向性验证(看多型):入池 = 完整模式(A1–A8)。每测试日重算全 A 横截面 RPS,
逐票 evaluate,按 min_rps(A/B 70/80/90)与 A7 距 52 周高(0.75/0.85)分档汇总
前瞻收益(T+1/5/10/20),对照「全 A 等权 baseline」与「HS300」。

关键效率:前瞻价格路径与阈值无关 → 每 (code,测试日) 只算一次(build_forward_cache);
evaluate 也只跑一遍,捕获 A1–A6、close、high52、return250,A7/A8 用 min_rps/hi_mult 在
纯 Python 里 re-threshold,A/B 网格零重复扫描。无未来函数(只读 ≤t)。

CLI: python -m tools.backtest.backtest_trend_template [--stride 15] [--out PATH]
⚠️ 非投资建议。产物只写 worktree。
"""
from __future__ import annotations

import json
import logging
import time

import pandas as pd

from tools.analysis.trend_template import conditions, rps
from tools.backtest import screen_forward_common as C
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("backtest.trend_template")

_CFG = THRESHOLDS["趋势模板"]
MIN_RPS_GRID = (70.0, 80.0, 90.0)
HI_MULT_GRID = (0.75, 0.85)      # A7:距 52 周高 25% / 15%
PRIMARY = (70.0, 0.75)           # 上线默认口径


def _capture(klines, dmaps, test_days, min_bars):
    """逐 (code,测试日) 跑 evaluate,捕获阈值无关特征。

    返回 (feats, eligible):
      feats[(code, dstr)] = {"a16": bool, "close": float, "high52": float, "ret250": float|None}
      eligible[code] = [该票合格测试日]  (有效数据 + 历史足够)
    """
    feats: dict[tuple[str, str], dict] = {}
    eligible: dict[str, list] = {}
    for i, (day, _reg) in enumerate(test_days):
        dstr = str(day.date())
        for code, df in klines.items():
            t = dmaps[code].get(day)
            if t is None or t < min_bars - 1:
                continue
            w, lt = C.window_at(df, t)
            r = conditions.evaluate(w, t=lt, rps250=None, cfg=_CFG)
            if r["异常"] is not None:
                continue
            cd, v = r["conditions"], r["values"]
            a16 = all(cd[f"a{k}"] for k in range(1, 7))
            feats[(code, dstr)] = {
                "a16": bool(a16), "close": v["close"],
                "high52": v["highest_high_250"], "ret250": v["return250"],
            }
            eligible.setdefault(code, []).append(day)
        if (i + 1) % 5 == 0:
            logger.info("  evaluate 进度 %d/%d 测试日", i + 1, len(test_days))
    return feats, eligible


def _rps_by_day(feats, test_days) -> dict[str, dict[str, float]]:
    """每测试日:全 A 横截面 Return250 → RPS250。返回 {dstr: {code: rps}}。"""
    out: dict[str, dict[str, float]] = {}
    for day, _reg in test_days:
        dstr = str(day.date())
        rets = {code: f["ret250"] for (code, d), f in feats.items()
                if d == dstr and f["ret250"] is not None}
        out[dstr] = rps.rps_from_returns(rets)
    return out


def _pool_records(feats, rps_day, cache, min_rps, hi_mult):
    """给定 (min_rps, hi_mult) 选出命中 (code,测试日) → 前瞻记录列表。"""
    recs = []
    for (code, dstr), f in feats.items():
        if not f["a16"] or f["high52"] is None:
            continue
        a7 = f["close"] >= f["high52"] * hi_mult
        if not a7:
            continue
        r = rps_day.get(dstr, {}).get(code)
        if r is None or r < min_rps:
            continue
        rec = cache.get((code, dstr))
        if rec is not None:
            recs.append(rec)
    return recs


def _pool_records_by_regime(feats, rps_day, cache, day2reg, min_rps, hi_mult):
    buckets: dict[str, list] = {}
    for (code, dstr), f in feats.items():
        if not f["a16"] or f["high52"] is None:
            continue
        if not (f["close"] >= f["high52"] * hi_mult):
            continue
        r = rps_day.get(dstr, {}).get(code)
        if r is None or r < min_rps:
            continue
        rec = cache.get((code, dstr))
        if rec is None:
            continue
        buckets.setdefault(day2reg[dstr], []).append(rec)
    return buckets


def _recent_acceptance(feats, rps_day, cache, test_days, min_rps, hi_mult, limit=12):
    """最近测试日入池票逐票前瞻(肉眼验收)。"""
    day = test_days[-1][0]
    dstr = str(day.date())
    rows = []
    for (code, d), f in feats.items():
        if d != dstr or not f["a16"] or f["high52"] is None:
            continue
        if not (f["close"] >= f["high52"] * hi_mult):
            continue
        r = rps_day.get(dstr, {}).get(code)
        if r is None or r < min_rps:
            continue
        rec = cache.get((code, dstr))
        if rec is None:
            continue
        rows.append({"code": code, "rps": r, "前瞻": rec["前瞻"]})
    rows.sort(key=lambda x: -x["rps"])
    return {"测试日": dstr, "命中数": len(rows), "样例": rows[:limit]}


def run(stride: int = 15, out: str | None = None, limit: int | None = None) -> dict:
    t0 = time.time()
    min_bars = int(_CFG["min_bars"])
    hs = C.load_hs300()
    hs_feat = C._hs300_regime_series(hs)

    codes = C.universe_codes(exclude_bj=True)
    if limit:
        codes = codes[:limit]
    logger.info("票池 %d 只(全 A 排北交所)", len(codes))
    klines = C.load_klines(codes, min_bars)
    dmaps = C.date_index_maps(klines)
    tdays = C.pick_test_days(C.all_trading_days(klines), hs_feat,
                             stride=stride, max_forward=max(C.WINDOWS))
    day2reg = {str(d.date()): r for d, r in tdays}
    logger.info("测试日 %d 个,regime 分布 %s", len(tdays),
                {r: sum(1 for _, x in tdays if x == r) for r in set(day2reg.values())})

    feats, eligible = _capture(klines, dmaps, tdays, min_bars)
    rps_day = _rps_by_day(feats, tdays)
    cache = C.build_forward_cache(klines, eligible, hs)
    logger.info("特征 %d 条,前瞻缓存 %d 条", len(feats), len(cache))

    # A/B 网格
    configs = []
    for mr in MIN_RPS_GRID:
        for hi in HI_MULT_GRID:
            recs = _pool_records(feats, rps_day, cache, mr, hi)
            configs.append({
                "min_rps": mr, "max_distance_from_52w_high": hi,
                "命中样本数": len(recs),
                "前瞻": C.summarize_records(recs),
                "是否默认口径": (mr, hi) == PRIMARY,
            })

    # baseline(全 A 等权,同测试日全体合格样本)+ HS300 自身
    baseline = C.summarize_records(list(cache.values()))
    hs_self = C.hs300_self_forward(hs, [d for d, _ in tdays])

    # 默认口径的 regime 分层 + 近期验收
    prim_reg = _pool_records_by_regime(feats, rps_day, cache, day2reg, *PRIMARY)
    regime_layer = {reg: {"命中样本数": len(rs), "前瞻": C.summarize_records(rs)}
                    for reg, rs in prim_reg.items()}
    recent = _recent_acceptance(feats, rps_day, cache, tdays, *PRIMARY)

    result = {
        "策略": "趋势模板(Minervini 8 条)",
        "入池口径": "完整模式 A1–A8(A8=RPS250≥min_rps)",
        "样本期": f"{str(tdays[0][0].date())} → {str(tdays[-1][0].date())}",
        "测试日数": len(tdays),
        "regime分布": {r: sum(1 for _, x in tdays if x == r) for r in ("牛", "熊", "震荡", "未知") if any(x == r for _, x in tdays)},
        "票池": f"全 A 排北交所,主档历史≥{min_bars}根,实际有效 {len(klines)} 只",
        "windows": list(C.WINDOWS),
        "AB网格": configs,
        "baseline_全A等权": baseline,
        "HS300自身": hs_self,
        "默认口径regime分层": regime_layer,
        "近期肉眼验收": recent,
        "耗时秒": round(time.time() - t0, 1),
        "免责声明": "方向性 sanity,非全 A alpha 保证;过滤器/研究工具。非投资建议。",
    }
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info("已写 %s", out)
    return result


def _main(argv=None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="趋势模板前瞻回测")
    ap.add_argument("--stride", type=int, default=15)
    ap.add_argument("--limit", type=int, default=None, help="限制票池只数(冒烟/测试用)")
    ap.add_argument("--out", default="data/backtest_local/trend_template_backtest.json")
    a = ap.parse_args(argv)
    r = run(stride=a.stride, out=a.out, limit=a.limit)
    print(json.dumps({k: r[k] for k in ("策略", "样本期", "测试日数", "regime分布", "耗时秒")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
