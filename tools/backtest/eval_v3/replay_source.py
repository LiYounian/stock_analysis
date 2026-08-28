"""回放回测轨:对确定性/纯技术策略,用本地 kline 历史复现历史预测 → 统一预测记录表(source=replay)。

复用各策略**已是纯 as-of 的**逐票筛选函数 `screen_*.signal_at(kdf, t)`(只用 ≤t 数据,防未来函数,
等价性已被各自 backtest_* 单测锁死),按回放日历逐 (票, 日) 判 SELECT。给方向型策略补长样本
(近5日/近1月/近1季/近1年及全史),弥补 live 观测仅约 14 交易日的统计力不足。

**不可回放的策略**(策略0多专家含新闻/LLM/情绪、策略9最强选股含 Tushare 筹码)不在此——它们的
signal 依赖历史无快照的外部数据,强行回放会引入未来函数或数据缺口,故只走 live 观测轨。

覆盖策略:
  · 逐票 as-of 型(per-(票,日) signal_at):
      S01 趋势深跌反包 / S02 放量后缩量回踩 / 3 箱体形态 / S03 最大范围选股 / S04 量价放量
  · 截面 TopK 型(策略4 动量组合·A腿):每交易日对全宇宙打「动量分」→ R²+拉普拉斯闸门 →
      按动量分降序取 TopK。与逐票 screener 不同,需**跨票截面**才能定 TopK,故单列
      `replay_momentum_predictions`。所有策略均 long-only、direction=+1;动量4额外带 rank_score
      =动量分(供 Top-N / rank-IC)。
"""
from __future__ import annotations

import logging

import numpy as np

from tools.collectors import market
from tools.pipeline import (screen_box, screen_max_range, screen_s01,
                            screen_s02, screen_volume)
from tools.store import repo as store

from . import schema

logger = logging.getLogger("backtest.eval_v3.replay")


def _sel_s01(kdf, t, code):
    return bool(screen_s01.signal_at(kdf, t).get("SELECT"))


def _sel_s02(kdf, t, code):
    return bool(screen_s02.signal_at(kdf, t).get("SELECT"))


def _sel_box(kdf, t, code):
    return bool(screen_box.signal_at(kdf, t).get("SELECT"))


def _sel_maxrange(kdf, t, code):
    return bool(screen_max_range.signal_at(kdf, t, code=code).get("SELECT"))


def _sel_volume(kdf, t, code):
    return bool(screen_volume.signal_at(kdf, t).get("SELECT"))


# strategy_id → (展示名, SELECT 判定函数)。均为方向型 long-only,可回放。
REPLAY_SCREENERS: dict[str, tuple] = {
    "S01": ("趋势深跌反包", _sel_s01),
    "S02": ("放量后缩量回踩", _sel_s02),
    "3": ("箱体形态", _sel_box),
    "S03": ("最大范围选股", _sel_maxrange),
    "S04": ("量价放量", _sel_volume),
}


def sample_universe(n: int | None, seed: int = 20260828) -> list[str]:
    """从全 master 均匀抽 n 票作回放宇宙(n=None→全量)。均匀抽样 → 无选择偏差、可复现。"""
    codes = sorted(store.list_master_codes())
    if not n or n >= len(codes):
        return codes
    idx = np.linspace(0, len(codes) - 1, n).round().astype(int)
    return [codes[i] for i in sorted(set(idx))]


def build_calendar(codes, ref_codes=("000001", "600000", "600519", "000002")) -> list[str]:
    """用高流动参考票 kline date 列并集近似交易日历(升序 YYYY-MM-DD)。"""
    dates: set[str] = set()
    for c in ref_codes:
        try:
            df = market.load_kline(c)
            dates.update(str(x)[:10] for x in df["date"].tolist())
        except Exception:   # noqa: BLE001
            continue
    return sorted(dates)


def replay_predictions(strategy_ids=None, universe_n: int | None = 800,
                       lookback_days: int | None = 250, stride: int = 1,
                       seed: int = 20260828):
    """生成回放预测记录表(source=replay)。

    · strategy_ids:默认全部 REPLAY_SCREENERS;
    · universe_n:回放宇宙抽样票数(控制运行时长;None=全A);
    · lookback_days:回放最近多少个交易日(None=全史);stride:交易日步长(>1 稀疏采样,仍供聚类)。
    只用 ≤t 数据,天然无未来函数。返回统一预测记录表 + 回放元信息 dict。
    """
    sids = list(strategy_ids or REPLAY_SCREENERS.keys())
    universe = sample_universe(universe_n, seed)
    calendar = build_calendar(universe)
    replay_dates = calendar[-lookback_days:] if lookback_days else calendar
    if stride > 1:
        replay_dates = replay_dates[::stride]
    date_set = set(replay_dates)

    records = []
    scanned = 0
    for code in universe:
        try:
            kdf = market.load_kline(code).reset_index(drop=True)
        except Exception:   # noqa: BLE001
            continue
        if "date" not in kdf.columns or len(kdf) < 60:
            continue
        dmap = {str(x)[:10]: i for i, x in enumerate(kdf["date"].tolist())}
        hit_ts = [(d, dmap[d]) for d in replay_dates if d in dmap]
        if not hit_ts:
            continue
        scanned += 1
        for sid in sids:
            name, fn = REPLAY_SCREENERS[sid]
            for d, t in hit_ts:
                try:
                    if fn(kdf, t, code):
                        records.append({
                            "strategy_id": sid, "strategy": name, "pred_date": d,
                            "code": code, "direction": 1, "rank_score": np.nan,
                            "source": "replay", "stype": schema.DIRECTIONAL,
                            "replayable": True})
                except Exception as e:   # noqa: BLE001
                    logger.debug("replay %s %s@%s 失败: %s", sid, code, d, str(e)[:50])
    meta = {"宇宙抽样票数": len(universe), "有效扫描票数": scanned,
            "回放交易日数": len(date_set), "stride": stride,
            "回放日范围": [replay_dates[0], replay_dates[-1]] if replay_dates else None,
            "覆盖策略": sids, "命中记录数": len(records)}
    logger.info("回放完成: %s", meta)
    return schema.make_frame(records), meta


# ───────────────────────── 策略4 动量组合(A腿·截面 TopK 型)─────────────────────────
# 登记信息与 schema.STRATEGY_META["动量组合"] 一致:ID=4,展示名,方向型(可回放)。
MOMENTUM_SID = "4"
MOMENTUM_NAME = "动量组合(A腿)"


def replay_momentum_predictions(universe_n: int | None = 800,
                                lookback_days: int | None = 250, stride: int = 1,
                                top_k: int | None = None, seed: int = 20260828):
    """策略4 动量组合(A腿)历史回放预测(source=replay),**带 rank_score=动量分**。

    与逐票 screener 不同,动量是**截面 TopK 型**:每交易日 T 对回放宇宙每票算「加权对数动量」
    分,过 R²≥R2_MIN + 拉普拉斯末根='买' 双闸门,再按动量分降序取 TopK 作当日选股。复用
    `screen_momentum` 的默认参数与 `tools.strategy.momentum` 的同一批算子,**口径与生产
    `combo_momentum_screen` 完全一致**(r2 过滤 → 拉普拉斯买 → 排序取 TopK),只是额外保留分数。

    防未来函数:动量分只用 closes[:t+1](尾部=当日 T);拉普拉斯为**因果 EMA**,L[t] 仅依赖 ≤t,
    故对全序列一次性算 L 与逐日截断 [:t+1] 再算 L 数值等价——可安全预算一次省时。历史 <lookback+1
    的 (票,日) 跳过。

    返回统一预测记录表(schema)+ 回放元信息 dict。rank_score=动量分、direction=+1、
    stype=directional(与既有 replay screener 同口径,走命中/收益/超额;分数同时供 rank-IC)。
    """
    from tools.pipeline import screen_momentum as _sm
    from tools.strategy.momentum import (_laplace_filter, weighted_log_momentum)

    lb, r2_min = _sm.LOOKBACK_DAYS, _sm.R2_MIN
    s_lap, min_slope = _sm.LAPLACE_S, _sm.MIN_SLOPE
    k = top_k or _sm.DEFAULT_TOP_K

    universe = sample_universe(universe_n, seed)
    calendar = build_calendar(universe)
    replay_dates = calendar[-lookback_days:] if lookback_days else calendar
    if stride > 1:
        replay_dates = replay_dates[::stride]
    date_set = set(replay_dates)

    per_date: dict[str, list[tuple[str, float]]] = {d: [] for d in replay_dates}
    scanned = 0
    for code in universe:
        try:
            kdf = market.load_kline(code).reset_index(drop=True)
        except Exception:   # noqa: BLE001
            continue
        if "date" not in kdf.columns or "close" not in kdf.columns or len(kdf) < lb + 1:
            continue
        closes = kdf["close"].astype(float).to_numpy()
        if len(closes) < 3:
            continue
        lap = _laplace_filter(closes, s_lap)   # 因果 EMA,一次算全序列(L[t] 只依赖 ≤t)
        dmap = {str(x)[:10]: i for i, x in enumerate(kdf["date"].tolist())}
        hit_ts = [(d, dmap[d]) for d in replay_dates if d in dmap]
        if not hit_ts:
            continue
        scanned += 1
        for d, t in hit_ts:
            if t < lb:                          # 历史不足 lookback+1 根 → 动量分不可算,跳过
                continue
            # 拉普拉斯末根='买'闸门(只用 ≤t):P[t]>L[t] 且 L[t]-L[t-1]>min_slope
            if not (closes[t] > lap[t] and (lap[t] - lap[t - 1]) > min_slope):
                continue
            mom = weighted_log_momentum(closes[:t + 1], lookback_days=lb)
            if mom.get("r_squared", 0.0) < r2_min:     # R² 闸门
                continue
            per_date[d].append((code, float(mom["score"])))

    records = []
    for d in replay_dates:
        picks = sorted(per_date[d], key=lambda kv: kv[1], reverse=True)[:k]   # 截面 TopK
        for code, score in picks:
            records.append({
                "strategy_id": MOMENTUM_SID, "strategy": MOMENTUM_NAME, "pred_date": d,
                "code": code, "direction": 1, "rank_score": score,
                "source": "replay", "stype": schema.DIRECTIONAL, "replayable": True})
    meta = {"宇宙抽样票数": len(universe), "有效扫描票数": scanned,
            "回放交易日数": len(date_set), "stride": stride, "top_k": k,
            "回放日范围": [replay_dates[0], replay_dates[-1]] if replay_dates else None,
            "覆盖策略": [MOMENTUM_SID], "命中记录数": len(records),
            "参数": {"lookback": lb, "r2_min": r2_min, "laplace_s": s_lap,
                     "min_slope": min_slope},
            "口径": "截面 TopK·A腿(加权对数动量→R²+拉普拉斯买闸门→按动量分TopK);rank_score=动量分"}
    logger.info("动量4回放完成: %s", meta)
    return schema.make_frame(records), meta


def run_momentum_replay(universe_n: int | None = 800, lookback_days: int | None = 250,
                        stride: int = 1, horizons=(1, 5), top_k: int | None = None,
                        seed: int = 20260828):
    """动量4回放**当前口径**评测:预测→统一打分→六维聚合。返回 (agg, meta, scored)。

    复用 v3 现有打分层(scoring/aggregate/prices),与 5 个 screener 同一套指标口径。产物 json
    由调用方落盘。抽样宇宙同时作等权基准(与 cli._run_replay 一致)。
    """
    from . import aggregate as _agg
    from . import prices as _prices
    from . import scoring as _scoring

    preds, meta = replay_momentum_predictions(universe_n, lookback_days, stride, top_k, seed)
    book = _prices.PriceBook()
    scored = _scoring.score_predictions(preds, book, horizons)
    if scored.empty:
        return {"窗口": {}}, meta, scored
    pred_dates = sorted(scored["pred_date"].unique().tolist())
    uni = sample_universe(universe_n, seed)
    ur = _scoring.universe_returns(pred_dates, horizons, uni, book)
    cal = build_calendar(uni)
    agg = _agg.aggregate(scored, ur, cal, horizons, track="replay")
    agg["宇宙"] = f"回放抽样 {len(uni)} 票(动量4·A腿)"
    return agg, meta, scored
