"""SEPA + VCP 监控 walk-forward 前瞻回测(按标签分层)。

方向性验证(看多型):技术合格池 = SEPA 三均线门;重点观察池按标签分层
(VCP进行中 / 接近枢纽 / 结构破坏)各自算前瞻收益(T+1/5/10/20),验证
「越靠近枢纽方向性越强、结构破坏最弱」的设计直觉,对照全 A 等权 baseline 与 HS300。

A/B 标定(占位阈值):pivot窗口 × 距前高% × 失效跌破%。效率:pivot 改变波段切分
→ 每 pivot 跑一遍 analyze_vcp,捕获原始量(硬收缩/进行中/量枯/距前高%/前轮低点/收盘),
距前高%、失效% 作为纯阈值 re-threshold(与 vcp.analyze_vcp 末段逻辑逐行对齐,test 锁定)。
无未来函数(只读 ≤t)。SEPA 门本身与 A/B 阈值无关,只算一遍。

注:`新候选` 标签需「首日入池+星标」的连日 pipeline 状态,稀疏测试日无法廉价复原,
本回测不单列(报告注明)。

CLI: python -m tools.backtest.backtest_sepa_vcp [--stride 15] [--out PATH]
⚠️ 非投资建议。产物只写 worktree。
"""
from __future__ import annotations

import json
import logging
import time

import pandas as pd

from tools.analysis.sepa_vcp import sepa, vcp
from tools.backtest import screen_forward_common as C
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("backtest.sepa_vcp")

_CFG = THRESHOLDS["SEPA_VCP"]
PIVOT_GRID = (2, 3, 4)
NEAR_GRID = (6.0, 8.0, 10.0)     # 距前高近%
FAIL_GRID = (1.0, 1.5, 2.0)      # 失效跌破%
PRIMARY = {"pivot": 3, "near": 8.0, "fail": 1.5}


def _vcp_capture(df: pd.DataFrame, t: int, pivot: int) -> dict | None:
    """跑一次 analyze_vcp(指定 pivot),捕获 re-threshold 所需原始量。"""
    cfg = dict(_CFG)
    cfg["pivot窗口"] = pivot
    va = vcp.analyze_vcp(df, t=t, cfg=cfg)
    rounds = va.get("轮次") or []
    n = len(rounds)
    if n == 0:
        return {"轮数": 0, "硬收缩": False, "ongoing": False, "dry": False,
                "距前高%": None, "close": float(df["close"].iloc[t]), "prev_low": None}
    last = rounds[-1]
    prev_low = rounds[-2]["low"] if n >= 2 else last["low"]
    pair = va.get("末对收缩")
    return {
        "轮数": n,
        "硬收缩": bool(pair and pair["硬收缩"]),
        "ongoing": bool(last.get("进行中")),
        "dry": bool(last.get("量枯")),
        "距前高%": last.get("距前高%"),
        "close": float(df["close"].iloc[t]),
        "prev_low": float(prev_low),
    }


def _labels(cap: dict, near_x: float, fail_f: float) -> dict:
    """由捕获量 re-derive 标签(与 vcp.analyze_vcp:184-208 逐行对齐)。"""
    if cap is None or cap["轮数"] == 0 or cap["prev_low"] is None:
        return {"VCP进行中": False, "接近枢纽": False, "结构破坏": False}
    broken = cap["close"] < cap["prev_low"] * (1.0 - fail_f / 100.0)
    near = cap["距前高%"] is not None and cap["距前高%"] <= near_x
    return {
        "VCP进行中": bool(cap["硬收缩"] and not broken),
        "接近枢纽": bool(cap["ongoing"] and cap["dry"] and near and not broken),
        "结构破坏": bool(broken),
    }


def _capture(klines, dmaps, test_days, min_bars):
    """逐 (code,测试日):SEPA 门(阈值无关) + 对合格票各 pivot 的 VCP 捕获。

    返回 (sepa_pass_pairs, vcapt, eligible):
      sepa_pass_pairs: set[(code, dstr)]  技术合格池成员
      vcapt[(code,dstr)][pivot] = capture dict
      eligible[code] = [合格测试日](全体,含未过 SEPA,供 baseline)
    """
    sepa_pass_pairs: set = set()
    vcapt: dict = {}
    eligible: dict[str, list] = {}
    for i, (day, _reg) in enumerate(test_days):
        dstr = str(day.date())
        for code, df in klines.items():
            t = dmaps[code].get(day)
            if t is None or t < min_bars - 1:
                continue
            eligible.setdefault(code, []).append(day)
            w, lt = C.window_at(df, t)
            sp = sepa.sepa_pass(w, t=lt, cfg=_CFG)
            if not sp.get("入池"):
                continue
            sepa_pass_pairs.add((code, dstr))
            caps = {p: _vcp_capture(w, lt, p) for p in PIVOT_GRID}
            vcapt[(code, dstr)] = caps
        if (i + 1) % 5 == 0:
            logger.info("  SEPA/VCP 进度 %d/%d 测试日", i + 1, len(test_days))
    return sepa_pass_pairs, vcapt, eligible


def _label_records(vcapt, cache, pivot, near_x, fail_f, which):
    recs = []
    for (code, dstr), caps in vcapt.items():
        lab = _labels(caps[pivot], near_x, fail_f)
        if lab.get(which):
            rec = cache.get((code, dstr))
            if rec is not None:
                recs.append(rec)
    return recs


def run(stride: int = 15, out: str | None = None, limit: int | None = None) -> dict:
    t0 = time.time()
    C.install_fast_vcp()                 # 向量化 pivot/_dates(等价,单测锁)→ analyze_vcp 提速
    min_bars = int(_CFG["最少历史根数"])
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
    logger.info("测试日 %d 个", len(tdays))

    sepa_pairs, vcapt, eligible = _capture(klines, dmaps, tdays, min_bars)
    cache = C.build_forward_cache(klines, eligible, hs)
    logger.info("SEPA 合格 %d (code,日),VCP 捕获 %d,前瞻缓存 %d",
                len(sepa_pairs), len(vcapt), len(cache))

    # 标签分层(默认口径)
    p, nx, ff = PRIMARY["pivot"], PRIMARY["near"], PRIMARY["fail"]
    tech_recs = [cache[k] for k in sepa_pairs if k in cache]
    layers = {
        "技术合格池": {"样本数": len(tech_recs), "前瞻": C.summarize_records(tech_recs)},
    }
    for which in ("VCP进行中", "接近枢纽", "结构破坏"):
        recs = _label_records(vcapt, cache, p, nx, ff, which)
        layers[which] = {"样本数": len(recs), "前瞻": C.summarize_records(recs)}

    # A/B 网格(聚焦 接近枢纽:样本量 vs 方向性 权衡)
    ab = []
    for pv in PIVOT_GRID:
        for nxx in NEAR_GRID:
            for fff in FAIL_GRID:
                recs = _label_records(vcapt, cache, pv, nxx, fff, "接近枢纽")
                ab.append({
                    "pivot窗口": pv, "距前高近%": nxx, "失效跌破%": fff,
                    "接近枢纽样本数": len(recs),
                    "前瞻": C.summarize_records(recs),
                    "是否默认口径": (pv, nxx, fff) == (p, nx, ff),
                })

    baseline = C.summarize_records(list(cache.values()))
    hs_self = C.hs300_self_forward(hs, [d for d, _ in tdays])

    # regime 分层(技术合格池)
    reg_buckets: dict[str, list] = {}
    for (code, dstr) in sepa_pairs:
        rec = cache.get((code, dstr))
        if rec is not None:
            reg_buckets.setdefault(day2reg[dstr], []).append(rec)
    regime_layer = {reg: {"样本数": len(rs), "前瞻": C.summarize_records(rs)}
                    for reg, rs in reg_buckets.items()}

    # 近期肉眼验收(最近测试日 SEPA 合格 + 标签)
    last_d = str(tdays[-1][0].date())
    recent_rows = []
    for (code, dstr), caps in vcapt.items():
        if dstr != last_d:
            continue
        rec = cache.get((code, dstr))
        if rec is None:
            continue
        lab = _labels(caps[p], nx, ff)
        recent_rows.append({"code": code, "标签": [k for k, v in lab.items() if v],
                            "前瞻": rec["前瞻"]})

    result = {
        "策略": "SEPA + VCP 监控",
        "入池口径": "技术合格池=SEPA 三均线门;观察池按 VCP 标签分层",
        "样本期": f"{str(tdays[0][0].date())} → {last_d}",
        "测试日数": len(tdays),
        "regime分布": {r: sum(1 for _, x in tdays if x == r) for r in ("牛", "熊", "震荡", "未知") if any(x == r for _, x in tdays)},
        "票池": f"全 A 排北交所,主档历史≥{min_bars}根,实际有效 {len(klines)} 只",
        "windows": list(C.WINDOWS),
        "标签分层_默认口径": layers,
        "AB网格_接近枢纽": ab,
        "baseline_全A等权": baseline,
        "HS300自身": hs_self,
        "技术合格池regime分层": regime_layer,
        "近期肉眼验收": {"测试日": last_d, "命中数": len(recent_rows), "样例": recent_rows[:15]},
        "备注": "新候选标签需连日 pipeline 状态,稀疏测试日不单列。",
        "耗时秒": round(time.time() - t0, 1),
        "免责声明": "方向性 sanity,非全 A alpha 保证;过滤器/人工翻图辅助。非投资建议。",
    }
    if out:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        logger.info("已写 %s", out)
    return result


def _main(argv=None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="SEPA+VCP 前瞻回测")
    ap.add_argument("--stride", type=int, default=15)
    ap.add_argument("--limit", type=int, default=None, help="限制票池只数(冒烟/测试用)")
    ap.add_argument("--out", default="data/backtest_local/sepa_vcp_backtest.json")
    a = ap.parse_args(argv)
    r = run(stride=a.stride, out=a.out, limit=a.limit)
    print(json.dumps({k: r[k] for k in ("策略", "样本期", "测试日数", "regime分布", "耗时秒")},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
