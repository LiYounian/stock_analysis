"""指标条件化情景预测(计划文档1 F3 + F4)。

思路:把"无条件历史频率"(predict.scenarios,已自证≈掷硬币)升级为**指标条件化**——
用当前 4 指标+KDJ 的离散状态,在**全A横截面池**(所有票×所有历史日的状态+前瞻收益)里
筛出"当下状态相似"的样本,用它们的 N 日前瞻收益经验分布给出方向概率 + 区间 + 期望。

核心约束(统筹审定):
  · A1 全A横截面池化(非单票史);向量化预计算 state_pool,只写 worktree。
  · 无未来函数命门:样本仅当其**前瞻结局日 od_N ≤ 预测日 t** 才入池(结局在 t 前已实现)。
  · A2 相似度量只用 3 主维度(趋势方向×动量×BOLL位置);匹配阶梯 精确→放宽BOLL→放宽动量→退回。
  · min 相似样本数不足 → 优雅退回无条件分布并标 是否退回=True;输出带**放宽层级**供 F6 按匹配质量分层。
  · r_N 未实现(历史末端/停牌)→ 该样本该 horizon **丢弃,不填 0/前值**(防污染分布)。

非投资建议。参数真源 THRESHOLDS['指标条件化'] / ['指标状态'] / ['BOLL'] / ['预测']['情景分位']。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from tools.analysis import indicator_state as ist
from tools.analysis import technical as ta
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.conditional_predict")

POOL_LOCAL = "data/backtest_local/state_pool.parquet"   # 建池产物,gitignore、只写 worktree
_HORIZONS = (1, 5, 10)


# ————————————————————————— 建池(向量化,每股一次)—————————————————————————
def _pool_labels(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """向量化算 3 主维度(趋势方向/动量/BOLL位置),口径与 technical.compute + state_vector 严格一致。"""
    close = df["close"]
    cfg = THRESHOLDS["指标状态"]
    tb = THRESHOLDS["BOLL"]

    ma5, ma10, ma20, ma60 = (ta.ma(close, w) for w in (5, 10, 20, 60))
    valid_ma = ma5.notna() & ma10.notna() & ma20.notna() & ma60.notna()
    up = (ma5 >= ma10) & (ma10 >= ma20) & (ma20 >= ma60)
    dn = (ma5 <= ma10) & (ma10 <= ma20) & (ma20 <= ma60)
    # 标签必须与 technical._ma_arrangement 严格一致(多头排列/空头排列/纠缠/数据不足)
    trend = pd.Series(np.select([~valid_ma, up, dn], ["数据不足", "多头排列", "空头排列"], "纠缠"),
                      index=df.index)

    md = ta.macd(close)
    bar = md["macd"]
    prev = bar.shift(1)
    macd_state = pd.Series(np.select([(prev <= 0) & (bar > 0), (prev >= 0) & (bar < 0), bar > 0],
                                     ["金叉", "死叉", "多头"], "空头"), index=df.index)
    rsi12 = ta.rsi(close, 12)
    bull = macd_state.isin(["金叉", "多头"])
    bear = macd_state.isin(["死叉", "空头"])
    mom = pd.Series(np.select([bull & (rsi12 >= cfg["动量RSI强"]), bear & (rsi12 <= cfg["动量RSI弱"])],
                              ["强", "弱"], "中"), index=df.index)

    pb = ta.boll(close)["percent_b"]
    boll = pd.Series(np.select(
        [pb.isna(), pb > 1, pb >= tb["触轨上_percentB"], pb < 0, pb <= tb["触轨下_percentB"]],
        ["数据不足", "破上轨", "触上轨", "破下轨", "触下轨"], "中性"), index=df.index)
    return trend, mom, boll


def build_state_pool(codes, warmup: int = None, horizons=_HORIZONS, save: bool = False) -> pd.DataFrame:
    """建全A横截面状态池:每 (code,date) 的 3 主维度 + 前瞻收益 r_N + 结局日 od_N。

    r_N/od_N 未实现(历史末端/停牌无对应 bar)→ 该行该 horizon 为 NaN/NaT,查询时按 horizon 丢弃。
    只保留主维度有效(非"数据不足")的行。前瞻收益用主档 qfq 一致口径。
    """
    from tools.collectors import market
    warmup = warmup or THRESHOLDS["指标条件化"]["池预热根数"]
    frames = []
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < warmup + 5:
            continue
        df = df.reset_index(drop=True)
        close = df["close"]
        trend, mom, boll = _pool_labels(df)
        cols = {"code": code, "date": pd.to_datetime(df["date"]),
                "trend": trend, "mom": mom, "boll": boll}
        for N in horizons:
            cols[f"r{N}"] = (close.shift(-N) / close - 1) * 100
            cols[f"od{N}"] = pd.to_datetime(df["date"]).shift(-N)
        out = pd.DataFrame(cols).iloc[warmup:]
        out = out[(out["trend"] != "数据不足") & (out["boll"] != "数据不足")]
        frames.append(out)
    pool = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if save and not pool.empty:
        from pathlib import Path
        Path(POOL_LOCAL).parent.mkdir(parents=True, exist_ok=True)
        pool.to_parquet(POOL_LOCAL, index=False)
        logger.info("state_pool 落盘 %s:%d 行 / %d 票", POOL_LOCAL, len(pool), pool["code"].nunique())
    return pool


_POOL_CACHE = None


def load_state_pool(path: str = POOL_LOCAL) -> pd.DataFrame | None:
    """载入建好的池(带进程内缓存)。缺失返回 None(conditional_scenarios 会全退回无条件)。"""
    global _POOL_CACHE
    if _POOL_CACHE is None:
        try:
            _POOL_CACHE = pd.read_parquet(path)
            for N in _HORIZONS:
                _POOL_CACHE[f"od{N}"] = pd.to_datetime(_POOL_CACHE[f"od{N}"])
        except Exception:
            return None
    return _POOL_CACHE


# ————————————————————————— 无条件退回 —————————————————————————
def _percentiles(r: np.ndarray, ql, qm, qh) -> dict:
    se = float(r.std(ddof=1) / np.sqrt(len(r))) if len(r) > 1 else None
    return {"上涨概率%": round(float((r > 0).mean() * 100), 1),
            f"悲观%(q{ql})": round(float(np.percentile(r, ql)), 2),
            f"中位%(q{qm})": round(float(np.percentile(r, qm)), 2),
            f"乐观%(q{qh})": round(float(np.percentile(r, qh)), 2),
            "期望%": round(float(r.mean()), 2),
            "期望标准误%": round(se, 3) if se is not None else None,
            "相似样本数": int(len(r))}


def _fallback(kline: pd.DataFrame, N: int, ql, qm, qh) -> dict:
    """退回无条件:用本票自身 ≤t 的 N 日前瞻收益分布(与 predict.scenarios 同口径)。"""
    fwd = (kline["close"].shift(-N) / kline["close"] - 1).dropna() * 100
    if len(fwd) < 20:
        return {"上涨概率%": None, "相似样本数": int(len(fwd)), "放宽层级": "退回", "是否退回": True}
    return {**_percentiles(fwd.to_numpy(), ql, qm, qh), "放宽层级": "退回", "是否退回": True}


# ————————————————————————— F3 核心 —————————————————————————
def conditional_scenarios(kline: pd.DataFrame, tech: dict, pool: pd.DataFrame | None,
                          as_of, horizons=_HORIZONS, min_samples: int = None) -> dict:
    """指标条件化情景预测。每 horizon 给 上涨概率/区间(q7/q50/q93)/期望/相似样本数/放宽层级/是否退回。

    无未来函数:池样本仅当 od_N ≤ as_of 才入。匹配阶梯 精确→放宽BOLL→放宽动量→退回(min样本兜底)。
    """
    P = THRESHOLDS["指标条件化"]
    ql, qm, qh = THRESHOLDS["预测"]["情景分位"]
    min_samples = min_samples or P["min相似样本数"]
    as_of = pd.Timestamp(as_of)

    sv = ist.state_vector(kline, tech)
    trend, mom, boll = ist.primary_key(sv)
    no_key = "数据不足" in (trend, boll)

    out = {}
    for N in horizons:
        if pool is None or pool.empty or no_key:
            out[f"{N}日"] = _fallback(kline, N, ql, qm, qh)
            continue
        odcol, rcol = f"od{N}", f"r{N}"
        base = pool[pool[odcol].notna() & (pool[odcol] <= as_of) & pool[rcol].notna()]
        levels = [
            ("精确", (base["trend"] == trend) & (base["mom"] == mom) & (base["boll"] == boll)),
            ("放宽1", (base["trend"] == trend) & (base["mom"] == mom)),
            ("放宽2", (base["trend"] == trend)),
        ]
        chosen = None
        for name, mask in levels:
            m = base[mask]
            if len(m) >= min_samples:
                chosen = (name, m[rcol].to_numpy())
                break
        if chosen is None:
            out[f"{N}日"] = _fallback(kline, N, ql, qm, qh)
            continue
        name, r = chosen
        out[f"{N}日"] = {**_percentiles(r, ql, qm, qh), "放宽层级": name, "是否退回": False}
    return out


# ————————————————————————— F4 方向映射 —————————————————————————
def direction_view(cond: dict) -> dict:
    """把条件化上涨概率映射为 看涨/看跌/中性 + 置信度(阈值 THRESHOLDS['指标条件化']['方向阈值'])。

    置信度看匹配质量:退回→低;精确→高;放宽1→中;放宽2→低。方向为倾向非承诺,非投资建议。
    """
    th = THRESHOLDS["指标条件化"]["方向阈值"]
    conf_map = {"精确": "高", "放宽1": "中", "放宽2": "低", "退回": "低"}
    out = {}
    for k, v in cond.items():
        p = v.get("上涨概率%")
        if p is None:
            out[k] = {"方向": "数据不足", "置信度": "低", "上涨概率%": None, "放宽层级": v.get("放宽层级")}
            continue
        direction = "看涨" if p >= th["看涨"] else ("看跌" if p <= th["看跌"] else "中性")
        lvl = v.get("放宽层级")
        conf = "低" if v.get("是否退回") else conf_map.get(lvl, "低")
        out[k] = {"方向": direction, "置信度": conf, "上涨概率%": p, "放宽层级": lvl}
    return out
