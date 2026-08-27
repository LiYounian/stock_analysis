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
from tools.config import settings
from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("analysis.conditional_predict")

POOL_LOCAL = "data/backtest_local/state_pool.parquet"   # 建池产物,gitignore、只写 worktree
_HORIZONS = (1, 5, 10)


# ————————————————————————— 建池(向量化,每股一次)—————————————————————————
def _labels_from_indicators(ma5, ma10, ma20, ma60, macd_bar, macd_bar_prev, rsi12, percent_b):
    """连续指标 → 3 主维度离散标签 (trend/mom/boll)。**全算 (_pool_labels) 与 O(1) 递推 (_labels_recur)
    共用的唯一离散化实现**——阈值/状态阶梯语义只此一份,从结构上杜绝双份实现漂移(设计文档 §3)。

    入参可为 pd.Series 或 np.ndarray(等长,一一对应);返回三个 np.ndarray(object,字符串标签)。
    口径与 technical._ma_arrangement / macd 状态 / boll._boll_state 位置严格一致。
    macd_bar_prev 为逐行"上一根 MACD 柱"(全算里即 bar.shift(1),首行 NaN → 判不出金叉/死叉)。
    """
    cfg = THRESHOLDS["指标状态"]
    tb = THRESHOLDS["BOLL"]
    ma5 = np.asarray(ma5, float); ma10 = np.asarray(ma10, float)
    ma20 = np.asarray(ma20, float); ma60 = np.asarray(ma60, float)
    bar = np.asarray(macd_bar, float); prev = np.asarray(macd_bar_prev, float)
    rsi12 = np.asarray(rsi12, float); pb = np.asarray(percent_b, float)

    with np.errstate(invalid="ignore"):   # NaN 比较返回 False(与 pandas 一致),仅抑制告警
        valid_ma = ~(np.isnan(ma5) | np.isnan(ma10) | np.isnan(ma20) | np.isnan(ma60))
        up = (ma5 >= ma10) & (ma10 >= ma20) & (ma20 >= ma60)
        dn = (ma5 <= ma10) & (ma10 <= ma20) & (ma20 <= ma60)
        # 标签必须与 technical._ma_arrangement 严格一致(多头排列/空头排列/纠缠/数据不足)
        trend = np.select([~valid_ma, up, dn], ["数据不足", "多头排列", "空头排列"], "纠缠")

        macd_state = np.select([(prev <= 0) & (bar > 0), (prev >= 0) & (bar < 0), bar > 0],
                               ["金叉", "死叉", "多头"], "空头")
        bull = np.isin(macd_state, ["金叉", "多头"])
        bear = np.isin(macd_state, ["死叉", "空头"])
        mom = np.select([bull & (rsi12 >= cfg["动量RSI强"]), bear & (rsi12 <= cfg["动量RSI弱"])],
                        ["强", "弱"], "中")

        boll = np.select(
            [np.isnan(pb), pb > 1, pb >= tb["触轨上_percentB"], pb < 0, pb <= tb["触轨下_percentB"]],
            ["数据不足", "破上轨", "触上轨", "破下轨", "触下轨"], "中性")
    return trend, mom, boll


def _pool_labels(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """向量化算 3 主维度(趋势方向/动量/BOLL位置),口径与 technical.compute + state_vector 严格一致。

    连续指标(MA/MACD/RSI/BOLL)在此全量算出,离散化统一委托 _labels_from_indicators(单一实现,防漂移)。
    """
    close = df["close"]
    ma5, ma10, ma20, ma60 = (ta.ma(close, w) for w in (5, 10, 20, 60))
    bar = ta.macd(close)["macd"]
    rsi12 = ta.rsi(close, 12)
    pb = ta.boll(close)["percent_b"]
    trend, mom, boll = _labels_from_indicators(ma5, ma10, ma20, ma60, bar, bar.shift(1), rsi12, pb)
    return (pd.Series(trend, index=df.index), pd.Series(mom, index=df.index),
            pd.Series(boll, index=df.index))


# EWM(MACD)/递归SMA(RSI)无限记忆:算新增 bar 标签用尾窗时需回看足够根数,使递归项
# (1-α)^n 衰减到 float ULP 之下 → 尾窗标签与全史逐值一致。MACD slow(26)/RSI(12) 最慢,768 根足矣。
_LABEL_CONVERGE = 768


def _pool_cols(df: pd.DataFrame, code, warmup: int, horizons) -> pd.DataFrame:
    """在整段 df 上产出该 code 的池行(标签 + 前瞻收益),跳过预热、丢"数据不足"。全算/尾窗共用。"""
    close = df["close"]
    date = pd.to_datetime(df["date"])          # 只转一次(重复 pd.to_datetime 会触发昂贵的 should_cache 扫描)
    trend, mom, boll = _pool_labels(df)
    cols = {"code": code, "date": date, "trend": trend, "mom": mom, "boll": boll}
    for N in horizons:
        cols[f"r{N}"] = (close.shift(-N) / close - 1) * 100
        cols[f"od{N}"] = date.shift(-N)
    out = pd.DataFrame(cols).iloc[warmup:]
    return out[(out["trend"] != "数据不足") & (out["boll"] != "数据不足")]


def _forward_returns(pos_idx: np.ndarray, close: np.ndarray, dates: np.ndarray,
                     horizons) -> dict:
    """按 position 计算给定行的前瞻收益/结局日(t+N 越界→NaN/NaT),口径与全算逐字一致、无未来函数。"""
    L = len(close)
    out = {}
    for N in horizons:
        tgt = pos_idx + N
        ok = tgt < L
        r = np.full(len(pos_idx), np.nan)
        r[ok] = (close[tgt[ok]] / close[pos_idx[ok]] - 1.0) * 100.0
        od = np.full(len(pos_idx), np.datetime64("NaT"), dtype="datetime64[ns]")
        od[ok] = dates[tgt[ok]]
        out[f"r{N}"] = r
        out[f"od{N}"] = od
    return out


def _incremental_code_frame(df: pd.DataFrame, code, old: pd.DataFrame,
                            warmup: int, horizons) -> pd.DataFrame | None:
    """对单 code 做增量:复用历史标签(冻结)+ 按新 kline 回填全部前瞻收益 + 全算新增 bar。

    失效兜底(结构变 / 除权 backfill 改写历史前复权价)→ 返回 None,调用方 fallback 到全算。
    值校验:对旧池每行按新 kline 的 position **重算全部前瞻收益**,凡旧已兑现的 r_N/od_N 必须逐值一致;
    任一不符即判历史价被改写(比 mtime / 抽样锚定更强:覆盖每一行,能捕获任意位置的非等比改写)。
    前瞻收益本就便宜(数组切片),重算不影响提速大头(标签复用)。
    """
    # 全程走 int64(ns)日期数组 + searchsorted,避免逐行 pd.Timestamp(百万级对象)/重复 to_datetime 拖慢增量。
    dates = pd.to_datetime(df["date"]).to_numpy("datetime64[ns]")
    close = df["close"].to_numpy(float)
    nd_i = dates.view("int64")                       # 新 kline 日期(升序,主档已排序)
    od_i = old["date"].to_numpy("datetime64[ns]").view("int64")   # 旧池 date 已在读盘时 to_datetime

    # 结构校验:旧池每行 date 必须仍在新 kline 中,且映射位置严格递增(否则历史被删/插/重排)
    opos = np.searchsorted(nd_i, od_i)
    if np.any(opos >= len(nd_i)) or not np.array_equal(nd_i[np.clip(opos, 0, len(nd_i) - 1)], od_i):
        return None
    if len(opos) > 1 and np.any(np.diff(opos) <= 0):
        return None

    # 按新 kline 重算旧行全部前瞻收益(向量化);凡旧已兑现的必须逐值一致,否则历史价被改写 → 失效
    fwd = _forward_returns(opos.astype("int64"), close, dates, horizons)
    for N in horizons:
        oldr = old[f"r{N}"].to_numpy(float)
        m = ~np.isnan(oldr)
        if m.any():
            if not np.allclose(oldr[m], fwd[f"r{N}"][m], rtol=1e-9, atol=1e-9):
                return None
            oldod = old[f"od{N}"].to_numpy("datetime64[ns]")   # 读盘时已 to_datetime
            if not np.array_equal(oldod[m], fwd[f"od{N}"][m]):
                return None

    reused = old.copy()                       # 复用历史标签(不重算 _pool_labels)
    for k, v in fwd.items():                  # 回填前瞻收益:已兑现原样、pending 到期兑现
        reused[k] = v
    parts = [reused]

    # 新增 bar:旧池无、position≥warmup 的新交易日 → 尾窗算标签(EWM 收敛到逐值一致)+ 全序前瞻收益
    all_pos = np.arange(len(nd_i))
    new_pos = all_pos[(all_pos >= warmup) & (~np.isin(nd_i, od_i))]
    if new_pos.size:
        start = max(0, int(new_pos.min()) - _LABEL_CONVERGE)
        sub = df.iloc[start:].reset_index(drop=True)
        new_frame = _pool_cols(sub, code, warmup=0, horizons=horizons)
        nf_i = new_frame["date"].to_numpy("datetime64[ns]").view("int64")
        new_frame = new_frame[np.isin(nf_i, nd_i[new_pos])]
        if not new_frame.empty:
            npos = np.searchsorted(nd_i, new_frame["date"].to_numpy("datetime64[ns]").view("int64"))
            f2 = _forward_returns(npos.astype("int64"), close, dates, horizons)   # 全序边界,与全算逐值一致
            for k, v in f2.items():
                new_frame[k] = v
            parts.append(new_frame)

    out = pd.concat(parts, ignore_index=True)
    return out.sort_values("date").reset_index(drop=True)


def build_state_pool(codes, warmup: int = None, horizons=_HORIZONS, save: bool = False,
                     rebuild: bool = False, pool_path: str = POOL_LOCAL) -> pd.DataFrame:
    """建全A横截面状态池:每 (code,date) 的 3 主维度 + 前瞻收益 r_N + 结局日 od_N。

    r_N/od_N 未实现(历史末端/停牌无对应 bar)→ 该行该 horizon 为 NaN/NaT,查询时按 horizon 丢弃。
    只保留主维度有效(非"数据不足")的行。前瞻收益用主档 qfq 一致口径。

    增量(save=True 且旧 pool_path 存在且非 rebuild):历史静态行冻结复用、末端 pending 只补前瞻收益、
    新增 bar 走全算;除权 backfill 改写历史价时靠廉价值校验捕获并对该 code 全量重算。产物列/格式/顺序
    与全量逐值一致(下游 screener 只读索引,不受影响)。rebuild=True 强制全量重建。
    """
    from tools.collectors import market
    warmup = warmup or THRESHOLDS["指标条件化"]["池预热根数"]

    old_index: dict = {}
    if save and not rebuild:
        try:
            old_pool = pd.read_parquet(pool_path)
            if not old_pool.empty:
                for N in horizons:
                    if f"od{N}" in old_pool.columns:
                        old_pool[f"od{N}"] = pd.to_datetime(old_pool[f"od{N}"])
                old_pool["date"] = pd.to_datetime(old_pool["date"])
                for c, g in old_pool.groupby("code", sort=False):
                    old_index[str(c)] = g
        except Exception:
            old_index = {}

    frames = []
    n_inc, n_full = 0, 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < warmup + 5:
            continue
        df = df.reset_index(drop=True)
        old = old_index.get(str(code))
        if old is not None and not old.empty:
            inc = _incremental_code_frame(df, code, old, warmup, horizons)
            if inc is not None:
                frames.append(inc)
                n_inc += 1
                continue
        frames.append(_pool_cols(df, code, warmup, horizons))
        n_full += 1

    pool = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if save and not pool.empty:
        from pathlib import Path
        Path(pool_path).parent.mkdir(parents=True, exist_ok=True)
        pool.to_parquet(pool_path, index=False)
        logger.info("state_pool 落盘 %s:%d 行 / %d 票 (增量 %d / 全算 %d)",
                    pool_path, len(pool), pool["code"].nunique(), n_inc, n_full)
    return pool


_POOL_CACHE = None
_INDEX_CACHE = None


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


def _build_index(pool: pd.DataFrame | None, horizons=_HORIZONS) -> dict:
    """把池预处理成查询索引:按 (趋势,动量,BOLL) 分格,每格每 horizon 存(结局日int升序, r_N对齐)。

    查询 O(log n):对目标格 bisect 出 od_N ≤ as_of 的前缀即无未来函数子集。放宽层级靠合并同 趋势(+动量) 的格。
    r_N/od_N 任一 NaN 的样本在此丢弃(不入索引)。
    """
    idx = {"cells": {}, "by_tm": {}, "by_t": {}}
    if pool is None or len(pool) == 0:
        return idx
    for cell, g in pool.groupby(["trend", "mom", "boll"], sort=False):
        per = {}
        for N in horizons:
            sub = g[[f"od{N}", f"r{N}"]].dropna()
            if len(sub) == 0:
                per[N] = (np.empty(0, dtype="int64"), np.empty(0))
                continue
            od = sub[f"od{N}"].values.astype("datetime64[ns]").astype("int64")
            r = sub[f"r{N}"].to_numpy(dtype=float)
            order = np.argsort(od, kind="stable")
            per[N] = (od[order], r[order])
        idx["cells"][tuple(cell)] = per
    for cell in idx["cells"]:
        t, m, _ = cell
        idx["by_tm"].setdefault((t, m), []).append(cell)
        idx["by_t"].setdefault(t, []).append(cell)
    return idx


def get_pool_index() -> dict:
    """载入池并建索引(进程内缓存),供 live predict / F6 回测快速查询。缺池返回空索引。"""
    global _INDEX_CACHE
    if _INDEX_CACHE is None:
        _INDEX_CACHE = _build_index(load_state_pool())
    return _INDEX_CACHE


def _gather(idx: dict, cells: list, N: int, as_of_int: int) -> np.ndarray:
    """合并给定格中 od_N ≤ as_of 的 r_N(bisect 前缀,无未来函数)。"""
    parts = []
    for c in cells:
        od, r = idx["cells"][c][N]
        if od.size == 0:
            continue
        k = int(np.searchsorted(od, as_of_int, side="right"))
        if k:
            parts.append(r[:k])
    return np.concatenate(parts) if parts else np.empty(0)


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
def conditional_scenarios(kline: pd.DataFrame, tech: dict, pool, as_of,
                          horizons=_HORIZONS, min_samples: int = None) -> dict:
    """指标条件化情景预测。每 horizon 给 上涨概率/区间(q7/q50/q93)/期望/相似样本数/放宽层级/是否退回。

    无未来函数:池样本仅当 od_N ≤ as_of 才入(索引内 bisect 前缀)。匹配阶梯 精确→放宽BOLL→放宽动量→退回。
    `pool` 可为建好的索引(dict,live/回测走 get_pool_index)或原始池 DataFrame(单测,内部即时建索引)。
    """
    P = THRESHOLDS["指标条件化"]
    ql, qm, qh = THRESHOLDS["预测"]["情景分位"]
    min_samples = min_samples or P["min相似样本数"]
    as_of_int = int(pd.Timestamp(as_of).value)

    idx = pool if isinstance(pool, dict) else _build_index(pool, horizons)
    sv = ist.state_vector(kline, tech)
    trend, mom, boll = ist.primary_key(sv)
    no_key = "数据不足" in (trend, boll)

    exact = [(trend, mom, boll)] if (trend, mom, boll) in idx["cells"] else []
    tm = idx["by_tm"].get((trend, mom), [])
    tr = idx["by_t"].get(trend, [])

    out = {}
    for N in horizons:
        if not idx["cells"] or no_key:
            out[f"{N}日"] = _fallback(kline, N, ql, qm, qh)
            continue
        chosen = None
        for name, cells in (("精确", exact), ("放宽1", tm), ("放宽2", tr)):
            r = _gather(idx, cells, N, as_of_int)
            if len(r) >= min_samples:
                chosen = (name, r)
                break
        if chosen is None:
            out[f"{N}日"] = _fallback(kline, N, ql, qm, qh)
            continue
        name, r = chosen
        out[f"{N}日"] = {**_percentiles(r, ql, qm, qh), "放宽层级": name, "是否退回": False}
    return out


# ————————————————————————— 激进版·后验倾斜信号(第二步)—————————————————————————
def _to_num(v):
    """把可能是 str 的强度/情绪值转 float;失败返回 None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def root_structural_signal(sentiment: dict | None) -> float | None:
    """激进版倾斜信号:根源(政策 + 公司公告)+ 近似结构性 的净信号 → [-1,1]。

    只用根源层,**剔除舆情噪声与顶层净情绪分**(舆情已被多轮回测钉死为噪声;顶层常被舆情单层污染)。
    · 政策:取 sentiment.三层.政策.净情绪(聚合值;政策专用条目已在 serialize 被过滤,不能数 events)。
    · 公司公告:events 中 层=='公司行为' 且 关系∈{直接,间接}。**真结构性门**:开关 SENTIMENT_PERSISTENCE_ON 且
      事件带 持续性 分类时,只认 持续性=='结构性持续'(短暂事件/中性=见光死,不进倾斜),方向取 持续性方向(退 影响方向);
      未分类/开关关 → 退回旧近似(影响强度≥根源强度门槛)。幅度始终用 影响强度/5。
    · 两根源按 根源权重 合成,缺层重归一。新鲜度='陈旧' → 该分量×0.5;='无数据' → 该分量不计。
    无根源信号 / sentiment 缺失 → 返回 None(不倾斜,等价纯技术,kill-switch 对齐点)。
    """
    if not sentiment:
        return None
    P = THRESHOLDS["指标条件化"]
    strong = P.get("根源强度门槛", 3)
    W = P.get("根源权重", {"公司公告": 0.6, "政策": 0.4})
    three = sentiment.get("三层") or {}

    # ① 政策根源(可靠,取聚合)
    pol = three.get("政策") or {}
    pol_sig, pol_ok = 0.0, False
    if (pol.get("样本数") or 0) > 0 and pol.get("新鲜度") != "无数据":
        v = _to_num(pol.get("净情绪"))
        if v is not None:
            pol_sig, pol_ok = max(-1.0, min(1.0, v)), True
            if pol.get("新鲜度") == "陈旧":
                pol_sig *= 0.5

    # ② 公司公告根源(真结构性门:优先用持续性分类 持续性=='结构性持续';缺失/开关关→退回旧强度近似)
    rel_w = {"直接": 1.0, "间接": 0.5}
    sign = {"利好": 1, "利空": -1}
    use_persist = getattr(settings, "SENTIMENT_PERSISTENCE_ON", True)
    news_fresh = (three.get("新闻") or {}).get("新鲜度")   # 公司事件源自新闻层,借其新鲜度
    comp_ok, num, den = False, 0.0, 0.0
    if news_fresh != "无数据":
        for e in sentiment.get("events") or []:
            if not isinstance(e, dict) or e.get("层") != "公司行为" or "error" in e:
                continue
            if e.get("与本股关系") not in rel_w:
                continue
            st = _to_num(e.get("影响强度"))
            persist = e.get("持续性") if use_persist else None
            if persist is not None:                        # 有真结构性分类
                if persist != "结构性持续":                # 短暂事件/中性 → 见光死,不进倾斜
                    continue
                s = sign.get(e.get("持续性方向")) or sign.get(e.get("影响方向"))  # 方向:持续性方向优先,退原影响方向
            else:                                          # 未分类/开关关 → 退回旧强度近似(≥门槛)
                if st is None or st < strong:
                    continue
                s = sign.get(e.get("影响方向"))
            if s is None or st is None:
                continue
            w = rel_w[e["与本股关系"]]
            num += s * (st / 5.0) * w                       # 幅度仍用影响强度;结构性只作方向门
            den += w
            comp_ok = True
    comp_sig = (num / den) if den else 0.0
    if comp_ok and news_fresh == "陈旧":
        comp_sig *= 0.5

    # ③ 合成 + 缺层重归一
    wc, wp = W.get("公司公告", 0.6), W.get("政策", 0.4)
    n = (wc if comp_ok else 0) + (wp if pol_ok else 0)
    if n == 0:
        return None                       # 无根源信号 → 不倾斜
    root = ((wc * comp_sig if comp_ok else 0) + (wp * pol_sig if pol_ok else 0)) / n
    return max(-1.0, min(1.0, root))


# ————————————————————————— F4 方向映射(+ 激进版倾斜)—————————————————————————
def direction_view(cond: dict, signal: float | None = None, k: float = 0.0,
                   tilt_horizons=("1日", "5日")) -> dict:
    """把条件化上涨概率映射为 看涨/看跌/中性 + 置信度(阈值 THRESHOLDS['指标条件化']['方向阈值'])。

    置信度看匹配质量:退回→低;精确→高;放宽1→中;放宽2→低。方向为倾向非承诺,非投资建议。

    激进版·后验倾斜(第二步):对 tilt_horizons(默认 1/5日)按 p_adj=clip(p+k·signal,0,100) 重判方向,
    结果放**新键** 方向_修正/上涨概率%_修正/是否倾斜(基线 方向/上涨概率% 原样保留,向后兼容)。
    signal∈[-1,1](根源结构性净信号)或 None;k=倾斜增益(pp/单位信号)。
    k=0 或 signal=None 或 该持有期不在 tilt_horizons 或 退回样本 → 不倾斜(方向_修正==方向,kill-switch)。
    """
    th = THRESHOLDS["指标条件化"]["方向阈值"]
    conf_map = {"精确": "高", "放宽1": "中", "放宽2": "低", "退回": "低"}
    tilt_on = (signal is not None) and (k != 0.0)

    def _dir(p):
        return "看涨" if p >= th["看涨"] else ("看跌" if p <= th["看跌"] else "中性")

    out = {}
    for key, v in cond.items():
        p = v.get("上涨概率%")
        lvl = v.get("放宽层级")
        if p is None:
            out[key] = {"方向": "数据不足", "置信度": "低", "上涨概率%": None, "放宽层级": lvl,
                        "方向_修正": "数据不足", "上涨概率%_修正": None, "是否倾斜": False}
            continue
        conf = "低" if v.get("是否退回") else conf_map.get(lvl, "低")
        base_dir = _dir(p)
        do_tilt = tilt_on and (key in tilt_horizons) and (not v.get("是否退回"))
        if do_tilt:
            p_adj = max(0.0, min(100.0, p + k * signal))
            adj_dir = _dir(p_adj)
        else:
            p_adj, adj_dir = p, base_dir
        out[key] = {"方向": base_dir, "置信度": conf, "上涨概率%": p, "放宽层级": lvl,
                    "方向_修正": adj_dir, "上涨概率%_修正": round(float(p_adj), 1),
                    "是否倾斜": bool(do_tilt)}
    return out


# ————————————————————————— CLI(建池)—————————————————————————
def _main(argv=None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="建/增量重建 state_pool(策略11 状态池)")
    ap.add_argument("--codes", help="逗号分隔代码(默认全A主档)")
    ap.add_argument("--rebuild", action="store_true", help="强制全量重建(忽略旧池,不走增量)")
    ap.add_argument("--out", default=POOL_LOCAL, help=f"落盘路径(默认 {POOL_LOCAL})")
    a = ap.parse_args(argv)
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        from tools.store import repo as store
        codes = sorted(store.list_master_codes())
    pool = build_state_pool(codes, save=True, rebuild=a.rebuild, pool_path=a.out)
    logger.info("完成:%d 行 / %d 票 → %s", len(pool), pool["code"].nunique() if not pool.empty else 0, a.out)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
