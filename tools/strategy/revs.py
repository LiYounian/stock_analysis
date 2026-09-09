"""REVS 四因子模型 · 阶段1:E盈利 + V估值 + S情绪 横截面选股腿(候选策略)。

来源:B站「Kara说量化」REVS(Regime/Earnings/Valuation/Sentiment)四因子。
架构(拆两轨,见 docs/计划/2026-09-09_REVS四因子模型_设计与价值论证.md §2):
  · 本文件 = 轨道一「E/V/S 横截面选股核心」——每只股票各有自己的 E/V/S 值,可排序选票。
  · R 宏观 regime 择时闸门(轨道二)= 全市场月频序列、单独控总仓位,不进本横截面块
    (全市场同值加进个股截面只是给所有票加同一常数,不改排序,是空转)。

分层(与 reversal_turnover / semi_factor 一致):
  · 纯因子函数(本文件上半):单票时序/财报/估值 → 各维原始子因子值;可脱 IO 独测。
    - S 维:读 kline 尾部窗口(防未来函数);E 维:读财报 periods 按披露日 PIT;V 维:读估值快照。
  · 选股策略(本文件下半 revs_screen):已预算各维原始子因子的中心记录 → 每子因子横截面
    winsorize+zscore→按方向→组内均值成维分 → 维间按权重合成(缺维**重归一**不塌缩) → top_k。
  · 薄管线(tools/pipeline/screen_revs.py):逐票装载 kline/财报/估值、算原始子因子、建 record、落 view。

因子方向约定(值越大越看好;方向在合成时以 ±1 施加):
  · E 盈利(+1):归母净利增速 / 营收增速 / ROE —— 越高越好。
  · V 估值(-1):PE_TTM / PB / 市值分位 —— 越低(便宜/小市值)越好。市值分位方向可配。
  · S 情绪(默认 -1):动量N日收益 / 换手率 / 波动率 —— A股短窗实证反转/低换手/低波占优;
    动量方向以回测 IC 符号为准(设计文档 §10 未决①)。

防未来函数:S 因子只读序列尾部窗口(回测按 series[:t+1] 切片天然不泄露);E 因子只纳入
  disclosure_date<=as_of 的报告期(analyzer 同红线);V 因子取 as_of 当日快照/横截面。

命名:候选策略,@strategy「REVS四因子」;回测达标+评测放行后再授面板编号。⚠️ 非投资建议。
"""
from __future__ import annotations

import math
from typing import Optional

from tools.analysis.financial.metrics import compute_derived
from tools.config.strategy import THRESHOLDS
from tools.strategy._factor_util import winsorize_med, zscore
from tools.strategy.registry import strategy

# —— 默认参数(单一真源在 THRESHOLDS["REVS四因子"];此处取值,缺键兜底)——
_CFG = THRESHOLDS.get("REVS四因子", {})
_TOP_K = int(_CFG.get("top_k", 20))
_DIM_W = _CFG.get("维度权重", {"E盈利": 0.40, "V估值": 0.33, "S情绪": 0.27})
_WINSOR = float(_CFG.get("winsor_scale", 3.0))
_E_CFG = _CFG.get("E盈利", {})
_V_CFG = _CFG.get("V估值", {})
_S_CFG = _CFG.get("S情绪", {})
_LIQ = _CFG.get("流动性", {})
_EXCLUDE_HEADS = tuple(str(h) for h in _CFG.get("剔除代码头", ["68", "8", "4", "9"]))
_LIMIT_PCT = float(_CFG.get("涨跌停触板%", 9.7))
_MIN_AMOUNT_WAN = float(_LIQ.get("最小成交额_万元", 5000))
_MIN_FLOAT_YI = float(_LIQ.get("最小流通市值_亿", 20))
_EXCLUDE_ST = bool(_LIQ.get("剔除ST", True))
_MIN_DIMS = 2                       # 至少几个维度有值才算有效候选(缺维重归一,但太少不选)
_FIELD = "REVS"                     # record 里存放各维原始子因子的命名空间键

_MOM_N = int(_S_CFG.get("动量窗口", 20))
_VOL_N = int(_S_CFG.get("波动窗口", 20))
_TURN_N = int(_S_CFG.get("换手窗口", 20))


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


# ————————————————————————————————————————————————————————————————
# 一、纯因子函数
# ————————————————————————————————————————————————————————————————
# —— S 情绪(纯量价,读 kline 尾部窗口;返回原始值,方向在合成时施加)——
def momentum_factor(closes, n: int = _MOM_N) -> Optional[float]:
    """动量:N 日收益率 = close[-1]/close[-1-n] - 1。只用最后 N+1 根。

    原始值(未定方向):正=近 N 日上涨。合成时按 S情绪.方向.动量(默认 -1=反转)施加。
    不足 N+1 根 / 基准价 ≤0 / 含 NaN → None。
    """
    if closes is None or len(closes) < n + 1:
        return None
    c_now, c_base = closes[-1], closes[-1 - n]
    if not _finite(c_now) or not _finite(c_base) or c_base <= 0:
        return None
    return float(c_now) / float(c_base) - 1.0


def turnover_mean(turnovers, n: int = _TURN_N,
                  min_valid: Optional[int] = None) -> Optional[float]:
    """换手:近 N 日**有效**换手率均值(原始值,正数)。只用最后 N 根。

    近端常有采集滞后(末几根 NaN),故对窗口内有效值取均值,需有效点 ≥ min_valid
    (默认 max(1, N//2))否则 None。合成时按方向(默认 -1=低换手好)施加。
    """
    if turnovers is None or len(turnovers) == 0:
        return None
    window = turnovers[-n:]
    valid = [float(v) for v in window if _finite(v)]
    need = min_valid if min_valid is not None else max(1, n // 2)
    if len(valid) < need:
        return None
    return sum(valid) / len(valid)


def volatility_factor(closes, n: int = _VOL_N) -> Optional[float]:
    """波动率:近 N 日日收益率的标准差(原始值,正数)。只用最后 N+1 根。

    合成时按方向(默认 -1=低波好)施加。不足 / 含 NaN / 无法算收益 → None。
    """
    if closes is None or len(closes) < n + 1:
        return None
    tail = [float(x) for x in closes[-(n + 1):]]
    if any(not _finite(x) for x in tail):
        return None
    rets = [tail[i] / tail[i - 1] - 1.0 for i in range(1, len(tail)) if tail[i - 1] != 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return var ** 0.5


# —— E 盈利(读财报 periods,按披露日 PIT;纯函数,periods 由管线从 store 取)——
def earnings_subfactors(periods_raw: dict, as_of: Optional[str] = None) -> Optional[dict]:
    """从财报多期三大表取 as_of 可见最新一期的 {归母净利增速, 营收增速, ROE}。

    PIT 防未来函数:as_of 非空时只纳入 disclosure_date<=as_of 的报告期(未披露不可见);
      披露日缺失的期在 as_of 模式下按"不可见"处理(保守)。as_of=None → 用全部(实盘取最新)。
    三项全 None → 返回 None(该维缺失,合成时重归一)。
    """
    if not periods_raw:
        return None
    derived_all = compute_derived(periods_raw)
    if as_of is None:
        visible = list(periods_raw.keys())
    else:
        visible = [p for p, rec in periods_raw.items()
                   if (rec.get("disclosure_date") is not None
                       and rec.get("disclosure_date") <= as_of)]
    if not visible:
        return None
    p = max(visible)
    d = derived_all.get(p) or {}
    out = {k: d.get(k) for k in ("归母净利增速", "营收增速", "ROE")}
    if all(v is None for v in out.values()):
        return None
    out["_报告期"] = p
    return out


# —— V 估值(读估值快照;纯函数,快照由管线从 store 取。市值分位在横截面里算)——
def valuation_subfactors(fund_snapshot: dict) -> Optional[dict]:
    """从基本面快照取 {PE_TTM, PB, 总市值}(市值分位需横截面,故此处只带原始总市值)。

    PE/PB ≤0(亏损/负净资产)记 None——负 PE 不是"便宜"而是无意义,交由重归一。
    三项全 None → None。
    """
    if not fund_snapshot:
        return None
    pe = fund_snapshot.get("PE_TTM")
    pb = fund_snapshot.get("PB")
    mv = fund_snapshot.get("总市值")
    out = {
        "PE_TTM": float(pe) if (_finite(pe) and pe > 0) else None,
        "PB": float(pb) if (_finite(pb) and pb > 0) else None,
        "总市值": float(mv) if (_finite(mv) and mv > 0) else None,
    }
    if all(v is None for v in out.values()):
        return None
    return out


# ————————————————————————————————————————————————————————————————
# 二、横截面选股(多维·多子因子·方向·缺维重归一)
# ————————————————————————————————————————————————————————————————
def _code_head_excluded(code: str, heads: tuple = _EXCLUDE_HEADS) -> bool:
    """按代码头剔除(默认剔科创68/北交8·4/B·退9,保留创业板30,与'剔ST/科创/北交'字面一致)。"""
    return code.startswith(heads)


def _standardize_directional(pairs: list[tuple[str, float]], direction: int,
                             scale: float) -> dict[str, float]:
    """对一个子因子的横截面 present 值做 winsor+zscore 再乘方向,返回 {code: z*dir}。

    pairs 只含该子因子有限值的票(缺失的不进);<1 → 空;全同值 → 全 0(zscore 内保证)。
    """
    if not pairs:
        return {}
    codes = [c for c, _ in pairs]
    vals = [v for _, v in pairs]
    z = zscore(winsorize_med(vals, scale=scale))
    return {codes[i]: z[i] * direction for i in range(len(codes))}


def _dimension_scores(scoped_codes: list[str], sub_raw: dict[str, dict[str, float]],
                      subweights: dict[str, float], directions: dict[str, int],
                      scale: float) -> dict[str, Optional[float]]:
    """一个维度的截面分:每子因子标准化+方向 → 逐票按可得子因子加权均值(缺子因子重归一)。

    sub_raw: {子因子名: {code: 原始值}};directions: {子因子名: ±1};subweights: {子因子名: w}。
    返回 {code: 维度分 or None};某票该维全部子因子缺失 → None(维缺失,上层重归一)。
    """
    # 每个子因子:present 值截面标准化
    z_by_sub: dict[str, dict[str, float]] = {}
    for sub, raw_map in sub_raw.items():
        pairs = [(c, raw_map[c]) for c in scoped_codes
                 if c in raw_map and _finite(raw_map[c])]
        z_by_sub[sub] = _standardize_directional(
            pairs, int(directions.get(sub, 1)), scale)
    # 逐票:可得子因子加权均值(重归一)
    out: dict[str, Optional[float]] = {}
    for c in scoped_codes:
        num = den = 0.0
        for sub, zmap in z_by_sub.items():
            if c in zmap:
                w = float(subweights.get(sub, 1.0))
                num += w * zmap[c]
                den += w
        out[c] = (num / den) if den > 0 else None
    return out


def _rank_pct(pairs: list[tuple[str, float]]) -> dict[str, float]:
    """横截面分位(0~1,越大值越高):市值分位用。相同值取平均秩。"""
    if not pairs:
        return {}
    srt = sorted(pairs, key=lambda x: x[1])
    n = len(srt)
    out: dict[str, float] = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and srt[j + 1][1] == srt[i][1]:
            j += 1
        # 组内平均秩(1..n)→ 归一 (avg_rank-0.5)/n
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[srt[k][0]] = (avg_rank - 0.5) / n
        i = j + 1
    return out


_SCHEMA = {
    "records": "dict[code, 中心记录];每条含 record['REVS']={E:{...},V:{...},S:{...}} + snapshot",
    "top_k": f"目标持仓数(默认 {_TOP_K})",
    "dim_weights": "维度权重 {E盈利,V估值,S情绪}(默认取 config)",
    "min_dims": f"最少有值维度数(默认 {_MIN_DIMS};不足则跳过,缺维重归一但太少不选)",
    "include_dims": "参与合成的维度子集(默认三维;回测 1a 可传 ['E盈利','S情绪'] 只验 E+S)",
}


@strategy("REVS四因子", "选股", params_schema=_SCHEMA)
def revs_screen(
    records: dict[str, dict],
    top_k: int = _TOP_K,
    dim_weights: Optional[dict] = None,
    min_dims: int = _MIN_DIMS,
    include_dims: Optional[list[str]] = None,
    winsor_scale: float = _WINSOR,
    min_amount_wan: float = _MIN_AMOUNT_WAN,
    min_float_yi: float = _MIN_FLOAT_YI,
    exclude_st: bool = _EXCLUDE_ST,
    exclude_heads: tuple = _EXCLUDE_HEADS,
    limit_pct: float = _LIMIT_PCT,
) -> dict:
    """REVS 四因子(阶段1 E/V/S)横截面选股。

    每票读预算好的原始子因子 record['REVS']={E,V,S};业务过滤(代码头/ST/停牌/涨跌停/低流动性)
    后:每维每子因子横截面 winsorize+zscore→按方向→组内加权均值成维分(缺子因子重归一);维间按
    dim_weights 加权合成(缺维**重归一**不塌缩成 0);present 维度数 < min_dims 的票不选。降序取 top_k。
    include_dims 可只用维度子集(回测 1a 用 E+S:V 目前无历史面板,见设计 §数据关口)。
    样本 <2 无法横截面标准化 → 空 + note。
    """
    dim_weights = dict(dim_weights or _DIM_W)
    dims = include_dims or ["E盈利", "V估值", "S情绪"]
    dim_weights = {d: float(w) for d, w in dim_weights.items() if d in dims}

    skip: dict[str, int] = {}

    def _skip(reason: str):
        skip[reason] = skip.get(reason, 0) + 1

    # —— 业务过滤,收集 scoped 票 + 各维原始子因子 ——
    scoped: list[str] = []
    e_raw: dict[str, dict] = {}
    v_raw: dict[str, dict] = {}
    s_raw: dict[str, dict] = {}
    mktcap: dict[str, float] = {}          # 总市值(算市值分位 + 流通市值门用总市值兜底)
    snap_map: dict[str, dict] = {}
    for code, rec in (records or {}).items():
        if _code_head_excluded(code, exclude_heads):
            _skip("剔除代码头")
            continue
        snap = (rec or {}).get("snapshot")
        if not snap:
            _skip("停牌或无快照")
            continue
        if exclude_st and snap.get("is_st"):
            _skip("ST")
            continue
        pct = snap.get("pct_chg")
        if isinstance(pct, (int, float)) and abs(pct) >= limit_pct:
            _skip("涨跌停")
            continue
        amt = snap.get("amount_wan")
        if not _finite(amt) or amt < min_amount_wan:
            _skip("低流动性(成交额)")
            continue
        f = (rec or {}).get(_FIELD) or {}
        e, v, s = f.get("E"), f.get("V"), f.get("S")
        mv = (v or {}).get("总市值")
        # 流通市值门(缺流通用总市值/亿);无市值不判该门(避免误杀)
        if _finite(mv) and (mv / 1e8) < min_float_yi:
            _skip("低流动性(市值)")
            continue
        scoped.append(code)
        snap_map[code] = snap
        if e:
            e_raw[code] = e
        if v:
            v_raw[code] = v
        if s:
            s_raw[code] = s
        if _finite(mv):
            mktcap[code] = float(mv)

    if len(scoped) < 2:
        return {"codes": [], "candidates": [], "top_k": top_k,
                "有效样本": len(scoped), "跳过": skip, "因子明细": [],
                "维度权重": dim_weights,
                "note": "有效样本 <2,无法做横截面标准化(全A闭环采集后才有足量样本)"}

    # —— 市值分位(横截面秩,进 V 维)——
    mv_pct = _rank_pct([(c, mktcap[c]) for c in scoped if c in mktcap])

    # —— 各维截面分 ——
    dim_score: dict[str, dict[str, Optional[float]]] = {}
    if "E盈利" in dims:
        subw = _E_CFG.get("子权重", {"归母净利增速": 1.0, "营收增速": 1.0, "ROE": 1.0})
        subs = {k: {c: e_raw[c].get(k) for c in scoped if c in e_raw}
                for k in ("归母净利增速", "营收增速", "ROE")}
        dim_score["E盈利"] = _dimension_scores(
            scoped, subs, subw, {k: 1 for k in subw}, winsor_scale)
    if "V估值" in dims:
        subw = _V_CFG.get("子权重", {"PE_TTM": 1.0, "PB": 1.0, "市值分位": 1.0})
        vdir = _V_CFG.get("方向", {"PE_TTM": -1, "PB": -1, "市值分位": -1})
        subs = {
            "PE_TTM": {c: v_raw[c].get("PE_TTM") for c in scoped if c in v_raw},
            "PB": {c: v_raw[c].get("PB") for c in scoped if c in v_raw},
            "市值分位": {c: mv_pct.get(c) for c in scoped if c in mv_pct},
        }
        dim_score["V估值"] = _dimension_scores(scoped, subs, subw, vdir, winsor_scale)
    if "S情绪" in dims:
        subw = _S_CFG.get("子权重", {"动量": 1.0, "换手": 1.0, "波动率": 1.0})
        sdir = _S_CFG.get("方向", {"动量": -1, "换手": -1, "波动率": -1})
        subs = {
            "动量": {c: s_raw[c].get("动量") for c in scoped if c in s_raw},
            "换手": {c: s_raw[c].get("换手") for c in scoped if c in s_raw},
            "波动率": {c: s_raw[c].get("波动率") for c in scoped if c in s_raw},
        }
        dim_score["S情绪"] = _dimension_scores(scoped, subs, subw, sdir, winsor_scale)

    # —— 维间合成(缺维重归一);present 维度 < min_dims 跳过 ——
    rows = []
    for c in scoped:
        num = den = 0.0
        present = []
        dvals = {}
        for d, w in dim_weights.items():
            sc = (dim_score.get(d) or {}).get(c)
            dvals[d] = sc
            if sc is not None:
                num += w * sc
                den += w
                present.append(d)
        if len(present) < min_dims:
            _skip(f"有效维度<{min_dims}")
            continue
        comp = num / den if den > 0 else None
        if comp is None:
            _skip("综合分缺失")
            continue
        rows.append({"code": c, "综合分": round(comp, 4),
                     "维度分": {d: (round(dvals[d], 4) if dvals[d] is not None else None)
                                for d in dim_weights},
                     "present维度": present,
                     "总市值_亿": round(mktcap[c] / 1e8, 2) if c in mktcap else None})

    if not rows:
        return {"codes": [], "candidates": [], "top_k": top_k,
                "有效样本": len(scoped), "跳过": skip, "因子明细": [],
                "维度权重": dim_weights,
                "note": f"无票满足有效维度≥{min_dims}(E/V/S 数据覆盖不足)"}

    rows.sort(key=lambda r: r["综合分"], reverse=True)
    picked = [r["code"] for r in rows][:top_k]
    return {
        "codes": picked,
        "candidates": picked,
        "top_k": top_k,
        "有效样本": len(scoped),
        "打分票数": len(rows),
        "跳过": skip,
        "因子明细": rows,
        "维度权重": dim_weights,
        "参与维度": dims,
        "参数": {"min_dims": min_dims, "winsor_scale": winsor_scale,
                 "min_amount_wan": min_amount_wan, "min_float_yi": min_float_yi},
    }
