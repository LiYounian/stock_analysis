"""回放回测轨:对确定性/纯技术策略,用本地 kline 历史复现历史预测 → 统一预测记录表(source=replay)。

复用各策略**已是纯 as-of 的**逐票筛选函数 `screen_*.signal_at(kdf, t)`(只用 ≤t 数据,防未来函数,
等价性已被各自 backtest_* 单测锁死),按回放日历逐 (票, 日) 判 SELECT。给方向型策略补长样本
(近5日/近1月/近1季/近1年及全史),弥补 live 观测仅约 14 交易日的统计力不足。

**不可回放的策略**(策略0多专家含新闻/LLM/情绪、策略9最强选股含 Tushare 筹码)不在此——它们的
signal 依赖历史无快照的外部数据,强行回放会引入未来函数或数据缺口,故只走 live 观测轨。

覆盖策略(全部纯技术、long-only、direction=+1):
  S01 趋势深跌反包 / S02 放量后缩量回踩 / 3 箱体形态 / S03 最大范围选股 / S04 量价放量
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
