"""多因子·截面标准化 + 等权合成 + 排序分层 + 落库(F6)。

流程(需全票池):
  各票原始子指标(factor.raw_factors)
  → 截面标准化(I3 默认分位;可 zscore),按子指标方向定向使"高分=好"
  → 子指标→因子(组内均值)→ 因子→综合分(config 权重,I2 默认等权)
  → 综合分的截面分位 → 强度[-1,1](分位居中→0)、方向(看多/看空/中性)
  → 每票落 code_view "factor"(供「多因子」专家读)

置信度 = 因子齐全度(分层口径:有数据的核心因子数 / 核心因子总数,增强因子只加分不稀释,封顶 1.0)。
  核心必备 = 生产真落地的因子(质量/价值/低波/成长/股息);增强 = 采集未落地或稀疏(资金流北向/筹码/预期/主力)。
  修 PR#19 回归:此前分母含 4 个恒 None 的增强因子,把多因子专家置信度无端拉低 ~20-33%(见 config 注释)。
分层单调性检验(monotonicity):高分组前瞻收益应 ≥ 低分组(复用 BT.1 纪律,真回测在批次C)。

依赖:分析层。用 store 公开 API 落 code_view(不改 store);读记录/K线经 collectors/ store。
入口:`python -m tools.analysis.factor.score --universe N [--date D]`。
"""
from __future__ import annotations

import logging

from tools.analysis.factor import factor as fac
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.factor.score")

_CFG = THRESHOLDS["合议"]["多因子"]


def _midrank_pctiles(pairs: list[tuple]) -> dict:
    """[(key, value)] 非空 → {key: 截面分位(0,1)}。中位秩,平票取均秩,对肥尾稳健。"""
    vals = sorted(v for _, v in pairs)
    n = len(vals)
    out = {}
    for k, v in pairs:
        below = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        out[k] = (below + 0.5 * equal + 0.0) / n if n else 0.5
    return out


def _zscore_norm(pairs: list[tuple]) -> dict:
    """[(key,value)] → {key: 0~1}(zscore 后用 CDF 近似压到 (0,1))。备选标准化。"""
    import math
    vals = [v for _, v in pairs]
    n = len(vals)
    if n < 2:
        return {k: 0.5 for k, _ in pairs}
    mean = sum(vals) / n
    sd = (sum((v - mean) ** 2 for v in vals) / (n - 1)) ** 0.5
    if sd == 0:
        return {k: 0.5 for k, _ in pairs}
    return {k: 0.5 * (1 + math.erf((v - mean) / sd / math.sqrt(2))) for k, v in pairs}


def _standardize(pairs: list[tuple], method: str) -> dict:
    return _zscore_norm(pairs) if method == "zscore" else _midrank_pctiles(pairs)


def cross_section(raw_by_code: dict, cfg: dict = None) -> dict:
    """全票池截面打分。raw_by_code: {code: factor.raw_factors(...)}。

    返回 {code: {综合分, 综合分位, 强度, 方向, 因子齐全度, 数据充分度, 各因子分位, 依据}}。
    """
    cfg = cfg or _CFG
    method = cfg.get("标准化", "分位")
    factors = cfg["因子"]
    weights = cfg["权重"]
    codes = list(raw_by_code)

    # 1) 每个子指标 → 截面定向分位(高=好)
    sub_pctile: dict[str, dict] = {}          # {子指标: {code: 定向分位}}
    for fname, subs in factors.items():
        for sub, direction in subs.items():
            pairs = [(c, raw_by_code[c].get(sub)) for c in codes
                     if raw_by_code[c].get(sub) is not None]
            if not pairs:
                sub_pctile[sub] = {}
                continue
            pct = _standardize(pairs, method)
            if direction < 0:                 # 越小越好 → 翻转
                pct = {c: 1.0 - p for c, p in pct.items()}
            sub_pctile[sub] = pct

    # 2) 子指标 → 因子(组内均值);因子 → 综合分(config 权重,跳过缺失因子)
    factor_score: dict[str, dict] = {}        # {code: {因子: 分}}
    for c in codes:
        fs = {}
        for fname, subs in factors.items():
            vals = [sub_pctile[sub][c] for sub in subs
                    if c in sub_pctile.get(sub, {})]
            if vals:
                fs[fname] = sum(vals) / len(vals)
        factor_score[c] = fs

    # 齐全度分层(修 PR#19 稀释回归):分母只算「核心必备」因子;「增强」因子(采集未落地/稀疏,
    # 恒 None)不撑分母、不稀释置信度,有值时按 增强齐全度权重 加分,封顶 1.0(绝不下拉)。
    enh_set = set(cfg.get("增强因子") or [])
    core_names = [f for f in factors if f not in enh_set]     # 核心 = 非增强(单一真源,防漂移)
    core_total = len(core_names) or len(factors)               # 退化保护:无核心配置时回退全因子
    enh_w = float(cfg.get("增强齐全度权重", 0.5))

    composite = {}
    completeness = {}
    for c in codes:
        fs = factor_score[c]
        wsum = sum(weights.get(f, 1.0) for f in fs)
        composite[c] = (sum(fs[f] * weights.get(f, 1.0) for f in fs) / wsum) if wsum else None
        core_present = sum(1 for f in fs if f not in enh_set)
        enh_present = sum(1 for f in fs if f in enh_set)
        comp = (core_present + enh_w * enh_present) / core_total
        completeness[c] = round(min(1.0, comp), 4)             # 增强只加分,封顶 1.0

    # 3) 综合分的截面分位 → 强度/方向
    comp_pairs = [(c, composite[c]) for c in codes if composite[c] is not None]
    comp_pct = _midrank_pctiles(comp_pairs)

    out = {}
    for c in codes:
        if composite[c] is None:
            out[c] = {"综合分": None, "综合分位": None, "强度": 0.0, "方向": "中性",
                      "因子齐全度": 0.0, "数据充分度": "缺失", "各因子分位": {}, "依据": ["无因子数据"]}
            continue
        p = comp_pct[c]
        强度 = round(max(-1.0, min(1.0, (p - 0.5) * 2)), 4)
        方向 = "看多" if 强度 > 0 else ("看空" if 强度 < 0 else "中性")
        comp = completeness[c]
        充分 = "充分" if comp >= 1.0 else ("缺失" if comp <= 0 else "部分降级")
        fs = factor_score[c]
        依据 = [f"{f}分位{fs[f]:.2f}" for f in sorted(fs, key=fs.get, reverse=True)]
        out[c] = {"综合分": round(composite[c], 4), "综合分位": round(p, 4),
                  "强度": 强度, "方向": 方向, "因子齐全度": comp,
                  "数据充分度": 充分, "各因子分位": {f: round(v, 4) for f, v in fs.items()},
                  "依据": 依据}
    return out


def monotonicity(scores: dict, fwd_ret: dict, layers: int = None) -> dict:
    """分层单调性:按综合分分 layers 组,看各组平均前瞻收益是否单调递增。

    scores: {code: 综合分}, fwd_ret: {code: 前瞻收益%}。纯函数(前瞻收益由调用方给,防未来函数在批次C)。
    返回 {分层数, 各层平均收益[], 单调递增, 高低价差}。样本不足→单调=None。
    """
    layers = int(layers or _CFG["分层数"])
    common = [(c, scores[c]) for c in scores if c in fwd_ret and scores[c] is not None]
    if len(common) < layers:
        return {"分层数": layers, "各层平均收益": [], "单调递增": None, "高低价差": None,
                "说明": "样本不足"}
    common.sort(key=lambda kv: kv[1])                 # 综合分升序
    n = len(common)
    layer_ret = []
    for i in range(layers):
        lo, hi = i * n // layers, (i + 1) * n // layers
        grp = [fwd_ret[c] for c, _ in common[lo:hi]]
        layer_ret.append(round(sum(grp) / len(grp), 4) if grp else 0.0)
    mono = all(layer_ret[i] <= layer_ret[i + 1] for i in range(layers - 1))
    return {"分层数": layers, "各层平均收益": layer_ret, "单调递增": mono,
            "高低价差": round(layer_ret[-1] - layer_ret[0], 4)}


# ———————————————————— 落库编排(读记录/K线 → 截面 → code_view)————————————————————
def precompute(as_of: str | None = None, codes: list[str] | None = None,
               北向: dict | None = None) -> dict:
    """读全票池中心记录 + K线 → 截面打分 → 每票落 code_view "factor"。返回统计。

    北向: 可选 {code: 净流入趋势};缺 → 资金流因子降级缺失(I4)。
    记录/ K线缺失的票跳过(无因子输入)。
    """
    import pandas as pd

    from tools.analysis import serialize
    from tools.collectors import market
    from tools.store import repo as store

    as_of = as_of or pd.Timestamp.today().strftime("%Y-%m-%d")
    store.set_active_date(as_of)
    codes = codes or []
    北向 = 北向 or {}

    raw_by_code = {}
    for c in codes:
        try:
            rec = serialize.load_record(c, date=as_of)
        except FileNotFoundError:
            continue
        kdf = None
        try:
            kdf = market.load_kline_recent(c)
        except FileNotFoundError:
            pass
        raw_by_code[c] = fac.raw_factors(rec, kdf, 北向.get(c))

    scored = cross_section(raw_by_code)
    for c, r in scored.items():
        store.put_code_view("factor", c, r, date=as_of)
    avail = _availability(raw_by_code)
    logger.info("多因子截面:记录 %d / 打分 %d;因子可得性 %s", len(codes), len(scored), avail)
    return {"扫描数": len(codes), "打分数": len(scored), "因子可得性": avail, "as_of": as_of}


def _availability(raw_by_code: dict) -> dict:
    """各子指标非空占比(诚实报告数据可得性)。"""
    n = len(raw_by_code) or 1
    subs = {s for subs in _CFG["因子"].values() for s in subs}
    return {s: round(sum(1 for r in raw_by_code.values() if r.get(s) is not None) / n, 3)
            for s in subs}


def _main(argv: list[str] | None = None) -> int:
    import argparse

    import pandas as pd

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="多因子截面打分落库")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔指定代码")
    ap.add_argument("--date", help="日期 YYYY-MM-DD(默认今天)")
    a = ap.parse_args(argv)
    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        codes = universe.universe_codes(limit=a.universe)
    r = precompute(as_of=as_of, codes=codes)
    logger.info("完成:%s", r)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
