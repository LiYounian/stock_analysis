"""看多/趋势型筛选器的 walk-forward 前瞻回测公共层。

给「趋势模板」「SEPA+VCP」两个筛选策略补方向性验证共用:
  · 全 A 排北交所票池 + 全历史 K 线加载(离线,读主档)
  · 跨行情段测试日抽样(固定步长 + 每 regime 段保底覆盖)
  · regime 打标(自洽:HS300 相对 MA200 + 60 日收益 → 牛/熊/震荡)
  · **前瞻价格路径缓存**:与 A/B 阈值无关,每 (code, 测试日) 只算一次,
    A/B 各配置只是从缓存里挑成员 → summarize。复用 event_study,不造轮子。

无未来函数:测试日 t 的入池只读 ≤t;前瞻收益取 t 之后价(event_study 保证)。
⚠️ 非投资建议。产物只写 worktree 本地,不写主检出。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from tools.backtest import event_study
from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.screen_forward")

WINDOWS = (1, 5, 10, 20)
# 回测喂给策略函数的近史尾部根数 = 与线上 screen_sepa_vcp/日筛一致(load_kline_recent,
# settings.DAILY_KLINE_ROWS=500)。策略在 t 只回看 ≤250 根 → 结果与全历史等价,但快得多。
TAIL_WIN = 500
_BJ_PREFIX = ("8", "4")
HS300_LOCAL = "data/backtest_local/hs300.parquet"


def window_at(df: pd.DataFrame, t: int, win: int = TAIL_WIN) -> tuple[pd.DataFrame, int]:
    """取 df 截至第 t 根(含)的近 win 根尾部窗口 + 该窗口内的局部下标。

    策略函数在 t 只回看有限根(趋势 ≤250、SEPA/VCP ≤~250),win=500 冗余充足,
    结果与喂全历史一致(等价性由单测锁),但省掉全历史数组转换/扫描开销。
    """
    lo = max(0, t - win + 1)
    return df.iloc[lo:t + 1].reset_index(drop=True), t - lo


def _fast_confirmed_pivots(high, low, t: int, n: int):
    """向量化版 vcp._confirmed_pivots(左右各 n 根确认的高/低点,索引 ≤ t-n)。

    与原实现逐点等价(单测锁):同根既高又低跳过;高取 high[i]==窗口最大、低同理。
    用 numpy sliding_window_view 把 O(bars×window) 的 Python max/min 降为向量化 O(bars)。
    """
    last = t - n
    if last < n:
        return []
    hh = np.asarray(high[:t + 1], dtype=float)
    ll = np.asarray(low[:t + 1], dtype=float)
    W = 2 * n + 1
    if len(hh) < W:
        return []
    wmax = sliding_window_view(hh, W).max(axis=1)      # 窗口 j 中心 = j+n
    wmin = sliding_window_view(ll, W).min(axis=1)
    centers = np.arange(n, n + len(wmax))
    in_range = centers <= last
    ish = (hh[centers] == wmax) & in_range
    isl = (ll[centers] == wmin) & in_range
    both = ish & isl
    ish &= ~both
    isl &= ~both
    out = []
    for j in np.where(ish | isl)[0]:
        c = int(centers[j])
        out.append((c, "H", float(hh[c])) if ish[j] else (c, "L", float(ll[c])))
    return out


_DATES_CACHE: dict = {}


def _fast_dates(kline):
    """向量化 + 记忆化版 vcp._dates。

    原实现 `[str(d)[:10] for d in kline["date"]]` 每轮重算、逐元素迭代 pandas datetime
    (极慢,分析里 90% 时间在此)。这里用 strftime 向量化(输出等价)并按对象记忆化——
    同一次 analyze_vcp 内多轮共享同一 kline,只算一次。仅回测装配用,不改 production。
    """
    cached = _DATES_CACHE.get(id(kline))
    if cached is not None and cached[0] is kline:
        return cached[1]
    ds = kline["date"].dt.strftime("%Y-%m-%d").tolist()
    _DATES_CACHE.clear()               # 只留最近一个,防 id 复用串味 + 控内存
    _DATES_CACHE[id(kline)] = (kline, ds)
    return ds


def install_fast_vcp() -> None:
    """把 vcp 内 _confirmed_pivots / _dates 换成等价加速版(模块全局重绑即生效)。
    不改 production 源码;等价性由 test_screen_forward_backtest 锁定。"""
    from tools.analysis.sepa_vcp import vcp
    vcp._confirmed_pivots = _fast_confirmed_pivots
    vcp._dates = _fast_dates


# ---------- 票池与 K 线 ----------
def universe_codes(exclude_bj: bool = True) -> list[str]:
    """全 A 主档代码(排北交所 8/4 头)。用已落地主档而非重新拉 universe。"""
    codes = list(store.list_master_codes())
    if exclude_bj:
        codes = [c for c in codes if c[:1] not in _BJ_PREFIX]
    return sorted(codes)


def load_klines(codes: list[str], min_bars: int) -> dict[str, pd.DataFrame]:
    """离线加载全历史 K 线;丢弃历史不足 min_bars 的票。返回 {code: df(升序)}。"""
    out: dict[str, pd.DataFrame] = {}
    skipped = 0
    for c in codes:
        try:
            df = market.load_kline(c)
        except FileNotFoundError:
            skipped += 1
            continue
        if df is None or len(df) < min_bars:
            skipped += 1
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        out[c] = df
    logger.info("加载 K 线 %d 只(跳过历史不足/缺失 %d 只,min_bars=%d)",
                len(out), skipped, min_bars)
    return out


def date_index_maps(klines: dict[str, pd.DataFrame]) -> dict[str, dict[pd.Timestamp, int]]:
    """每票 {日期 -> 行下标},供 O(1) 定位测试日。"""
    return {c: {d: i for i, d in enumerate(df["date"])} for c, df in klines.items()}


# ---------- 测试日与 regime ----------
def all_trading_days(klines: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    """并集交易日(用 HS300 或票池并集)。这里取票池并集,升序。"""
    days: set[pd.Timestamp] = set()
    for df in klines.values():
        days.update(df["date"].tolist())
    return sorted(days)


def _hs300_regime_series(hs: pd.DataFrame) -> pd.DataFrame:
    """给 HS300 加 MA200 与 60 日收益,供 regime 打标。"""
    h = hs.sort_values("date").reset_index(drop=True).copy()
    h["ma200"] = h["close"].rolling(200).mean()
    h["ret60"] = h["close"] / h["close"].shift(60) - 1.0
    return h


def regime_tag(hs_feat: pd.DataFrame, day: pd.Timestamp) -> str:
    """自洽 regime:HS300 收盘 vs MA200 且 60 日收益方向 → 牛/熊/震荡。

    牛:close>MA200 且 ret60>+5%;熊:close<MA200 且 ret60<-5%;其余 震荡。
    透明、可复现;仅用于分层展示,不参与入池判定。
    """
    sub = hs_feat[hs_feat["date"] <= day]
    if len(sub) == 0:
        return "未知"
    row = sub.iloc[-1]
    close, ma200, ret60 = row["close"], row["ma200"], row["ret60"]
    if pd.isna(ma200) or pd.isna(ret60):
        return "未知"
    if close > ma200 and ret60 > 0.05:
        return "牛"
    if close < ma200 and ret60 < -0.05:
        return "熊"
    return "震荡"


def pick_test_days(trading_days: list[pd.Timestamp], hs_feat: pd.DataFrame, *,
                   stride: int = 15, max_forward: int = 20,
                   per_regime_min: int = 3) -> list[tuple[pd.Timestamp, str]]:
    """固定步长抽测试日 + 保证每 regime 段 ≥ per_regime_min。

    只取有足够前瞻余量(距最后交易日 ≥ max_forward 根)的日子,避免 T+20 大量越界。
    返回 [(day, regime)] 升序。
    """
    if not trading_days:
        return []
    usable = trading_days[:-max_forward] if len(trading_days) > max_forward else []
    if not usable:
        return []
    picked = usable[::stride]
    tagged = [(d, regime_tag(hs_feat, d)) for d in picked]

    # 保底:每个 regime 段至少 per_regime_min 个
    from collections import Counter
    cnt = Counter(r for _, r in tagged)
    picked_set = set(picked)
    for reg in ("牛", "熊", "震荡"):
        need = per_regime_min - cnt.get(reg, 0)
        if need <= 0:
            continue
        for d in usable:
            if need <= 0:
                break
            if d in picked_set:
                continue
            if regime_tag(hs_feat, d) == reg:
                tagged.append((d, reg))
                picked_set.add(d)
                need -= 1
    tagged.sort(key=lambda x: x[0])
    return tagged


# ---------- 前瞻价格路径缓存(与阈值无关,只算一次)----------
def build_forward_cache(klines: dict[str, pd.DataFrame],
                        eligible: dict[str, list[pd.Timestamp]],
                        hs: pd.DataFrame,
                        windows=WINDOWS) -> dict[tuple[str, str], dict]:
    """每票一次 event_study.forward_returns(该票所有合格测试日)→ {(code, 'YYYY-MM-DD'): rec}。

    eligible: {code: [该票参与的测试日]}(已保证 t 在票内且历史足够)。
    rec = {"前瞻": {n: ret|None}, "alpha": {n: 值}}(alpha = 个股前瞻 − HS300 前瞻)。
    """
    cache: dict[tuple[str, str], dict] = {}
    for code, days in eligible.items():
        if not days:
            continue
        recs = event_study.forward_returns([str(d.date()) for d in days],
                                            klines[code], windows=windows,
                                            benchmark_df=hs)
        for r in recs:
            cache[(code, r["事件日"])] = {"前瞻": r["前瞻"], "alpha": r.get("alpha", {})}
    return cache


def summarize_records(records: list[dict], windows=WINDOWS) -> dict:
    """复用 event_study.summarize 的口径(样本数/平均收益/胜率/平均Alpha)。"""
    return event_study.summarize(records, windows=windows)


def hs300_self_forward(hs: pd.DataFrame, test_days: list[pd.Timestamp],
                       windows=WINDOWS) -> dict:
    """HS300 自身在各测试日的前瞻分布(作为绝对基准行)。"""
    recs = event_study.forward_returns([str(d.date()) for d in test_days],
                                       hs, windows=windows)
    return event_study.summarize(recs, windows=windows)


def load_hs300() -> pd.DataFrame:
    """读本地缓存的 HS300 全史(akshare 一次性抓;不触主检出)。"""
    df = pd.read_parquet(HS300_LOCAL)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def fmt_pct(x) -> str:
    return "—" if x is None else f"{x * 100:+.2f}%"


def fmt_rate(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"
