"""次日入场→当日收盘 实盘口径 回测底座(walk-forward, 全A)。

回答 gate:"D 晚选股 → D+1 按估量入场价买 → 目标 D+1 收盘绝对为正"这套口径,
在真实历史上有没有可用的正胜率/正期望?哪种入场档最优?弱市避高 β 提多少胜率?

口径与防未来(硬红线):
  · 信号日 t 只用 ≤t 信息;入场发生在 D+1=t+1,成交/收益只用 t+1 已确定 K 线。
  · close[t+1] 仅作结果标签,绝不回喂入选/入场判定。
  · β 用 ≤t 的 close-to-close 收益 as-of 估;regime_asof 用 ≤t 的市场趋势。
  · k 网格预注册(见 docs/计划/..._回测底座计划与预注册.md),不 tune 到某几天。

入场成交模型:
  · marketable buy-limit(主口径):挂价 P → open[t+1]≤P 立即以 open 成交(价更优);
    否则 low[t+1]≤P 则以 P 成交;否则未触发。
  · doc-literal(设计 §3.1 对照):low≤P≤high 判成交、价取 P,否则未触发。

收益/α/β(每单):
  · 绝对收益 = close[t+1]/fill_price − 1(>0 记胜;未触发不计入胜率分母)。
  · α = 绝对收益 − mkt_ir[t+1](全A等权 open→close 同期)。
  · β 拖累致亏:m=mkt_ir[t+1],亏损单若 m<0 且 β·m<0 且 |β·m|≥0.5·|绝对收益|。

产物只写 worktree 本地 / --out-dir;--data-root 指主仓只读,不污染主检出。
⚠️ 测试环境研究模拟,非投资建议。

用法:
  python -m tools.backtest.nextday_entry --data-root <主仓> --out-dir DIR \
      [--start 2018-01-01] [--end 2026-09-14] [--stride 2] [--screen all|momentum|reversal] \
      [--cost-bps 10] [--fill marketable|doc|both] [--max-codes N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.nextday_entry")

_BJ_PREFIX = ("8", "4")

# ── 预注册入场档网格(动手前写死;跑完不改) ────────────────────────────────
LIMIT_PC_KS = (0.0, 0.01, 0.02, 0.03)      # P=close[t]*(1-k)
LIMIT_OPEN_KS = (0.005, 0.01, 0.02)        # P=open[t+1]*(1-k)
DIP_PC_KS = (0.03, 0.05)                    # 震荡低点 P=close[t]*(1-k2)

# β / regime 分桶
BETA_LO, BETA_HI = 0.8, 1.2                 # 低/中/高 β 边界
BETA_WIN, BETA_MIN = 60, 40                 # 滚动 β 窗口
REGIME_LABELS = ("普跌", "震荡", "普涨")    # regime code 0/1/2
BBUCKET_LABELS = ("低β", "中β", "高β")      # bbucket code 0/1/2
PERIOD_LABELS = ("早段", "晚段")            # OOS 两段
OOS_SPLIT = "2023-01-01"

# 累加器字段布局(每个 stratum cell 一行)
NF = 10
(F_TRIG, F_FILL, F_WIN_G, F_SUM_G, F_SQ_G, F_WIN_N, F_SUM_N, F_LOSS_G,
 F_BDRAG, F_SUM_MKT) = range(NF)
N_STRATA = len(PERIOD_LABELS) * len(REGIME_LABELS) * len(BBUCKET_LABELS)  # 18


def stratum_code(period: np.ndarray, regime: np.ndarray, bbucket: np.ndarray) -> np.ndarray:
    """(period 0/1, regime 0/1/2, bbucket 0/1/2) → 0..17。"""
    return period * 9 + regime * 3 + bbucket


# ────────────────────────────── 数据加载 ──────────────────────────────
def universe_codes(data_root: str, exclude_bj: bool = True) -> list[str]:
    kdir = os.path.join(data_root, "data", "master", "kline")
    codes = [fn[:-8] for fn in os.listdir(kdir) if fn.endswith(".parquet")]
    if exclude_bj:
        codes = [c for c in codes if c[:1] not in _BJ_PREFIX]
    return sorted(codes)


def load_kline(data_root: str, code: str) -> pd.DataFrame | None:
    path = os.path.join(data_root, "data", "master", "kline", f"{code}.parquet")
    try:
        df = pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        return None
    if df is None or "date" not in df.columns or len(df) == 0:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


# ────────────────────────────── 单票预算 ──────────────────────────────
def precompute(df: pd.DataFrame) -> dict:
    """单票 K线 → 回测数组。全部向量化,无前视(前向量只作标签)。"""
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    to = df["turnover"].to_numpy(float) if "turnover" in df.columns else np.full(len(c), np.nan)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    n = len(c)

    prev = np.empty(n); prev[0] = np.nan; prev[1:] = c[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ir = c / o - 1.0                       # 当日 open→close
        ret_cc = c / prev - 1.0                # 昨收→今收(算 β / regime)
    ir[~(o > 0)] = np.nan

    sc = pd.Series(c)
    ma20 = sc.rolling(20, min_periods=20).mean().to_numpy()
    ma60 = sc.rolling(60, min_periods=60).mean().to_numpy()
    # 近 N 日收益(as-of t,用 ≤t):r_k[t]=c[t]/c[t-k]-1
    def past_ret(k):
        r = np.full(n, np.nan)
        if n > k:
            r[k:] = c[k:] / c[:-k] - 1.0
        return r
    ret5, ret10, ret20 = past_ret(5), past_ret(10), past_ret(20)
    to_ma20 = pd.Series(to).rolling(20, min_periods=10).mean().to_numpy()

    return dict(dates=dates, o=o, h=h, lo=lo, c=c, prev=prev, ir=ir, ret_cc=ret_cc,
                turnover=to, to_ma20=to_ma20, ma20=ma20, ma60=ma60,
                ret5=ret5, ret10=ret10, ret20=ret20, n=n)


# ────────────────────────────── 市场基准(pass 1) ──────────────────────────────
def build_market(feats: dict) -> dict:
    """全A等权:mkt_ir[d](open→close)、mkt_ret_cc[d](昨收→今收)、breadth[d](c>ma20 占比)。"""
    acc_ir: dict[str, list] = {}
    acc_cc: dict[str, list] = {}
    acc_br: dict[str, list] = {}
    for f in feats.values():
        d = f["dates"]
        ir, cc, c, ma20 = f["ir"], f["ret_cc"], f["c"], f["ma20"]
        for j in np.nonzero(~np.isnan(ir))[0]:
            cell = acc_ir.get(d[j]);  acc_ir[d[j]] = [cell[0] + ir[j], cell[1] + 1] if cell else [float(ir[j]), 1]
        for j in np.nonzero(~np.isnan(cc))[0]:
            cell = acc_cc.get(d[j]);  acc_cc[d[j]] = [cell[0] + cc[j], cell[1] + 1] if cell else [float(cc[j]), 1]
        valid = ~np.isnan(ma20)
        for j in np.nonzero(valid)[0]:
            above = 1.0 if c[j] > ma20[j] else 0.0
            cell = acc_br.get(d[j]);  acc_br[d[j]] = [cell[0] + above, cell[1] + 1] if cell else [above, 1]
    mkt_ir = {d: s / n for d, (s, n) in acc_ir.items() if n}
    mkt_cc = {d: s / n for d, (s, n) in acc_cc.items() if n}
    breadth = {d: s / n for d, (s, n) in acc_br.items() if n}
    return dict(mkt_ir=mkt_ir, mkt_cc=mkt_cc, breadth=breadth)


def regime_asof_series(mkt_cc: dict, breadth: dict) -> dict:
    """as-of regime:用 ≤t 的市场趋势(等权 5 日累计收益)分 普跌/震荡/普涨(全局三分位)。

    只用 ≤t 信息(t 当日 close-to-close 已确定 = ≤t)。三分位阈值全局标定(描述性分层),
    不构成前视(regime 是分层条件,非入选/入场判定)。
    """
    days = sorted(mkt_cc.keys())
    cc = np.array([mkt_cc[d] for d in days])
    # 5 日累计(含当日),不足 5 日用已有
    cum5 = np.full(len(days), np.nan)
    for i in range(len(days)):
        lo = max(0, i - 4)
        cum5[i] = np.prod(1.0 + cc[lo:i + 1]) - 1.0
    valid = ~np.isnan(cum5)
    q1, q2 = np.quantile(cum5[valid], [1 / 3, 2 / 3])
    lab = np.where(cum5 <= q1, 0, np.where(cum5 >= q2, 2, 1))
    return {d: int(lab[i]) for i, d in enumerate(days)}


# ────────────────────────────── β(as-of) ──────────────────────────────
def rolling_beta(stock_cc: np.ndarray, mkt_cc_aligned: np.ndarray) -> np.ndarray:
    """β[t] = cov(stock, mkt)/var(mkt) over 窗口内 ≤t 的 close-to-close(as-of,不含 t+1)。

    向量化(rolling 和):cov=E[sm]−E[s]E[m], var=E[mm]−E[m]²,窗口内**成对有效**样本
    (s、m 同时非 NaN)才计入,min_periods=BETA_MIN。O(n)/票。
    """
    s = pd.Series(stock_cc, dtype=float)
    m = pd.Series(mkt_cc_aligned, dtype=float)
    good = s.notna() & m.notna()
    s = s.where(good); m = m.where(good)
    r = lambda x: x.rolling(BETA_WIN, min_periods=BETA_MIN)  # noqa: E731
    cnt = good.astype(float).where(good).rolling(BETA_WIN, min_periods=BETA_MIN).sum()
    mean_s = r(s).sum() / cnt
    mean_m = r(m).sum() / cnt
    mean_sm = r(s * m).sum() / cnt
    mean_mm = r(m * m).sum() / cnt
    cov = mean_sm - mean_s * mean_m
    var = mean_mm - mean_m * mean_m
    with np.errstate(invalid="ignore", divide="ignore"):
        beta = (cov / var).to_numpy().copy()
    beta[~(var.to_numpy() > 0)] = np.nan
    return beta


def bbucket_of(beta: np.ndarray) -> np.ndarray:
    """β → 桶 code:低(0,<0.8)/中(1)/高(2,>1.2)。NaN → -1(剔除)。"""
    b = np.full(len(beta), -1, dtype=int)
    valid = ~np.isnan(beta)
    b[valid & (beta < BETA_LO)] = 0
    b[valid & (beta >= BETA_LO) & (beta <= BETA_HI)] = 1
    b[valid & (beta > BETA_HI)] = 2
    return b


# ────────────────────────────── 入场档 → 挂价 ──────────────────────────────
def entry_prices(feat: dict, t_idx: np.ndarray) -> dict:
    """对信号索引 t_idx,给各入场档在 D+1 的挂价 P(未含成交判定)。返回 {rule_name: P_array}。"""
    c_t = feat["c"][t_idx]
    o_n = feat["o"][t_idx + 1]
    out = {"open": o_n.copy()}
    for k in LIMIT_PC_KS:
        out[f"limit_pc_{k}"] = c_t * (1.0 - k)
    for k in LIMIT_OPEN_KS:
        out[f"limit_open_{k}"] = o_n * (1.0 - k)
    for k in DIP_PC_KS:
        out[f"dip_pc_{k}"] = c_t * (1.0 - k)
    return out


def fill_and_return(P: np.ndarray, o_n, h_n, l_n, c_n, model: str):
    """给挂价 P 与 D+1 的 OHLC → (fill_price, ret, filled_mask)。

    marketable: open≤P→open;否则 low≤P→P;否则未触发。
    doc:        low≤P≤high→P;否则未触发。
    'open' 档(P==open)两模型都必成交于 open。
    """
    fill = np.full(len(P), np.nan)
    if model == "marketable":
        take_open = o_n <= P
        fill[take_open] = o_n[take_open]
        rest = ~take_open & (l_n <= P)
        fill[rest] = P[rest]
    elif model == "doc":
        ok = (l_n <= P) & (P <= h_n)
        fill[ok] = P[ok]
    else:
        raise ValueError(model)
    filled = ~np.isnan(fill)
    ret = np.full(len(P), np.nan)
    ret[filled] = c_n[filled] / fill[filled] - 1.0
    return fill, ret, filled


# ────────────────────────────── as-of 策略代理筛选 ──────────────────────────────
def screen_mask(feat: dict, screen: str) -> np.ndarray:
    """as-of 候选筛(只用 ≤t)。all=全体;momentum/reversal=可复现代理(非 live 选股)。"""
    n = feat["n"]
    if screen == "all":
        return np.ones(n, dtype=bool)
    c, ma20, ma60 = feat["c"], feat["ma20"], feat["ma60"]
    if screen == "momentum":            # 强势动量:close>MA20>MA60 且近 20 日为正
        with np.errstate(invalid="ignore"):
            m = (~np.isnan(ma20)) & (~np.isnan(ma60)) & (c > ma20) & (ma20 > ma60) & (feat["ret20"] > 0)
        return m
    if screen == "reversal":            # 超跌低换手:近 10 日跌 且 换手<20 日均
        with np.errstate(invalid="ignore"):
            m = (feat["ret10"] < -0.03) & (~np.isnan(feat["to_ma20"])) & (feat["turnover"] < feat["to_ma20"])
        return m
    raise ValueError(screen)


# ────────────────────────────── 累加 ──────────────────────────────
@dataclass
class Result:
    # rule_name → (fill_model → np.ndarray(N_STRATA, NF))
    acc: dict = field(default_factory=dict)
    # 配对(vs open marketable):rule_name → np.array([n, sum_diff, sumsq_diff, wins_diff])
    paired: dict = field(default_factory=dict)

    def cell(self, rule: str, model: str) -> np.ndarray:
        key = (rule, model)
        if key not in self.acc:
            self.acc[key] = np.zeros((N_STRATA, NF))
        return self.acc[key]

    def pair(self, rule: str) -> np.ndarray:
        if rule not in self.paired:
            self.paired[rule] = np.zeros(4)
        return self.paired[rule]


def accumulate(res: Result, rule: str, model: str, strat: np.ndarray,
               filled: np.ndarray, ret: np.ndarray, mkt: np.ndarray,
               beta: np.ndarray, cost: float) -> None:
    """把一票某档某成交模型的信号批量并入累加器。strat=stratum_code(每信号)。"""
    arr = res.cell(rule, model)
    # triggered(全部有效信号,不管成交)
    np.add.at(arr[:, F_TRIG], strat, 1.0)
    if not filled.any():
        return
    fs = strat[filled]
    r = ret[filled]
    m = mkt[filled]
    b = beta[filled]
    rn = (1.0 + r) * (1.0 - cost) - 1.0        # net of round-trip cost
    np.add.at(arr[:, F_FILL], fs, 1.0)
    np.add.at(arr[:, F_SUM_G], fs, r)
    np.add.at(arr[:, F_SQ_G], fs, r * r)
    np.add.at(arr[:, F_SUM_N], fs, rn)
    np.add.at(arr[:, F_SUM_MKT], fs, m)
    np.add.at(arr[:, F_WIN_G], fs, (r > 0).astype(float))
    np.add.at(arr[:, F_WIN_N], fs, (rn > 0).astype(float))
    loss = r < 0
    np.add.at(arr[:, F_LOSS_G], fs, loss.astype(float))
    bm = b * m
    bdrag = loss & (m < 0) & (bm < 0) & (np.abs(bm) >= 0.5 * np.abs(r))
    np.add.at(arr[:, F_BDRAG], fs, bdrag.astype(float))


# ────────────────────────────── 主循环 ──────────────────────────────
def run(data_root: str, start: str | None, end: str | None, stride: int,
        screen: str, cost_bps: float, fill: str, max_codes: int | None) -> dict:
    codes = universe_codes(data_root)
    if max_codes:
        codes = codes[:max_codes]
    logger.info("加载 %d 票 K线 + 预算 ...", len(codes))
    feats: dict[str, dict] = {}
    for i, code in enumerate(codes):
        df = load_kline(data_root, code)
        if df is None or len(df) < 120:        # 需 ≥120 根(MA60+β 起步)
            continue
        feats[code] = precompute(df)
        if (i + 1) % 1000 == 0:
            logger.info("  ...%d/%d", i + 1, len(codes))
    logger.info("有效票 %d,建市场基准 ...", len(feats))

    mkt = build_market(feats)
    regime = regime_asof_series(mkt["mkt_cc"], mkt["breadth"])
    mkt_ir, mkt_cc = mkt["mkt_ir"], mkt["mkt_cc"]

    cost = cost_bps / 1e4
    models = ["marketable", "doc"] if fill == "both" else [fill]
    res = Result()

    n_trades = 0
    for ci, (code, feat) in enumerate(feats.items()):
        dates = feat["dates"]
        n = feat["n"]
        # 对齐市场 close-to-close 到本票日期,算 as-of β
        mkt_cc_al = np.array([mkt_cc.get(d, np.nan) for d in dates])
        beta_arr = rolling_beta(feat["ret_cc"], mkt_cc_al)
        bbkt = bbucket_of(beta_arr)

        # 候选信号 t:有 t+1、在 [start,end]、按 stride 抽样、过 screen、β 有效
        smask = screen_mask(feat, screen)
        t_all = np.arange(n - 1)               # 需要 t+1
        keep = smask[t_all] & (bbkt[t_all] >= 0)
        # 日期过滤 + stride(按全局交易日序?这里用本票 index stride,近似均匀抽样)
        if start is not None:
            keep &= dates[t_all] >= start
        if end is not None:
            keep &= dates[t_all] <= end        # 信号日 ≤end;t+1 可能=最后一天,允许
        t_idx = t_all[keep]
        if stride > 1:
            t_idx = t_idx[::stride]
        if len(t_idx) == 0:
            continue

        # D+1 OHLC
        o_n = feat["o"][t_idx + 1]; h_n = feat["h"][t_idx + 1]
        l_n = feat["lo"][t_idx + 1]; c_n = feat["c"][t_idx + 1]
        exec_dates = dates[t_idx + 1]
        # 需 D+1 有效价与市场 ir
        m_ir = np.array([mkt_ir.get(d, np.nan) for d in exec_dates])
        valid = (o_n > 0) & (c_n > 0) & (h_n > 0) & (l_n > 0) & ~np.isnan(m_ir)
        if not valid.any():
            continue
        t_idx = t_idx[valid]; o_n = o_n[valid]; h_n = h_n[valid]; l_n = l_n[valid]
        c_n = c_n[valid]; exec_dates = exec_dates[valid]; m_ir = m_ir[valid]

        # stratum:period(信号日 vs OOS_SPLIT)× regime(信号日 as-of)× bbucket(信号日 as-of β)
        sig_dates = dates[t_idx]
        period = (sig_dates >= OOS_SPLIT).astype(int)
        reg = np.array([regime.get(d, 1) for d in sig_dates])
        bb = bbkt[t_idx]
        strat = stratum_code(period, reg, bb)
        beta_sig = beta_arr[t_idx]

        Ps = entry_prices(feat, t_idx)
        for model in models:
            open_ret = None
            for rule, P in Ps.items():
                fillp, ret, filled = fill_and_return(P, o_n, h_n, l_n, c_n, model)
                accumulate(res, rule, model, strat, filled, ret, m_ir, beta_sig, cost)
                if model == "marketable" and rule == "open":
                    open_ret = ret
                    open_filled = filled
            # 配对:各档 vs open(marketable),仅两者都成交
            if model == "marketable" and open_ret is not None:
                for rule, P in Ps.items():
                    if rule == "open":
                        continue
                    _, ret, filled = fill_and_return(P, o_n, h_n, l_n, c_n, "marketable")
                    both = filled & open_filled
                    if both.any():
                        diff = ret[both] - open_ret[both]
                        p = res.pair(rule)
                        p[0] += both.sum(); p[1] += diff.sum()
                        p[2] += (diff * diff).sum(); p[3] += (diff > 0).sum()
        n_trades += len(t_idx)
        if (ci + 1) % 1000 == 0:
            logger.info("  回测 ...%d/%d 票, 累计信号 %d", ci + 1, len(feats), n_trades)

    logger.info("完成:%d 票, %d 信号(未去重成交)", len(feats), n_trades)
    return dict(res=res, n_codes=len(feats), n_signals=int(n_trades),
                cost_bps=cost_bps, screen=screen, fill=fill,
                start=start, end=end, stride=stride)


# ────────────────────────────── 汇总 → 表 ──────────────────────────────
def _agg_cells(arr: np.ndarray, mask: np.ndarray) -> dict:
    """把选中的 stratum 行汇总成指标 dict。"""
    a = arr[mask].sum(axis=0)
    trig, fill_n = a[F_TRIG], a[F_FILL]
    if fill_n <= 0:
        return dict(triggered=int(trig), filled=0, trigger_rate=np.nan,
                    win_gross=np.nan, win_net=np.nan, mean_gross=np.nan,
                    mean_net=np.nan, mean_alpha=np.nan, std_gross=np.nan,
                    beta_drag_share=np.nan, n_loss=0)
    mean_g = a[F_SUM_G] / fill_n
    var_g = max(a[F_SQ_G] / fill_n - mean_g ** 2, 0.0)
    return dict(
        triggered=int(trig), filled=int(fill_n),
        trigger_rate=fill_n / trig if trig else np.nan,
        win_gross=a[F_WIN_G] / fill_n,
        win_net=a[F_WIN_N] / fill_n,
        mean_gross=mean_g,
        mean_net=a[F_SUM_N] / fill_n,
        mean_alpha=(a[F_SUM_G] - a[F_SUM_MKT]) / fill_n,
        std_gross=np.sqrt(var_g),
        beta_drag_share=(a[F_BDRAG] / a[F_LOSS_G]) if a[F_LOSS_G] > 0 else np.nan,
        n_loss=int(a[F_LOSS_G]),
    )


def _mask(period=None, regime=None, bbucket=None) -> np.ndarray:
    """构造 stratum 选择掩码(None=该维不限)。"""
    m = np.ones(N_STRATA, dtype=bool)
    codes = np.arange(N_STRATA)
    p = codes // 9; r = (codes % 9) // 3; b = codes % 3
    if period is not None:
        m &= p == period
    if regime is not None:
        m &= r == regime
    if bbucket is not None:
        m &= b == bbucket
    return m


def summarize(payload: dict) -> dict:
    """产出结构化汇总:各档 ALL / 分 OOS 段 / 分 regime / 分 β + β脆弱性 + 配对。"""
    res: Result = payload["res"]
    out: dict = dict(meta={k: payload[k] for k in
                           ("n_codes", "n_signals", "cost_bps", "screen", "fill",
                            "start", "end", "stride")})
    model = "marketable" if payload["fill"] in ("marketable", "both") else payload["fill"]
    rules = [k[0] for k in res.acc if k[1] == model]

    # 各档总表(ALL)+ OOS 两段
    tbl = {}
    for rule in rules:
        arr = res.cell(rule, model)
        tbl[rule] = dict(
            ALL=_agg_cells(arr, _mask()),
            早段=_agg_cells(arr, _mask(period=0)),
            晚段=_agg_cells(arr, _mask(period=1)),
        )
    out["by_rule"] = tbl

    # 分 regime × β(ALL 段):用最优限价档(下面选)+ open + 最深 dip 展示
    out["regime_beta"] = {}
    for rule in rules:
        arr = res.cell(rule, model)
        grid = {}
        for ri, rlab in enumerate(REGIME_LABELS):
            for bi, blab in enumerate(BBUCKET_LABELS):
                grid[f"{rlab}/{blab}"] = _agg_cells(arr, _mask(regime=ri, bbucket=bi))
        out["regime_beta"][rule] = grid

    # β 脆弱性:弱市(普跌)内 高β vs 低β 的绝对胜率(用 open 与最优限价档)
    frag = {}
    for rule in rules:
        arr = res.cell(rule, model)
        wk_lo = _agg_cells(arr, _mask(regime=0, bbucket=0))
        wk_hi = _agg_cells(arr, _mask(regime=0, bbucket=2))
        st_lo = _agg_cells(arr, _mask(regime=2, bbucket=0))
        st_hi = _agg_cells(arr, _mask(regime=2, bbucket=2))
        # 简单两比例 z 检验(弱市 高β vs 低β 胜率差)
        z = _z_two_prop(wk_hi, wk_lo)
        frag[rule] = dict(弱市低β=wk_lo, 弱市高β=wk_hi, 强市低β=st_lo, 强市高β=st_hi,
                          弱市_高减低_胜率=(wk_hi["win_gross"] - wk_lo["win_gross"])
                          if (wk_hi["filled"] and wk_lo["filled"]) else np.nan,
                          z=z)
    out["beta_fragility"] = frag

    # 配对(各档 vs open marketable)
    paired = {}
    for rule, p in res.paired.items():
        n = p[0]
        if n <= 0:
            continue
        md = p[1] / n
        var = max(p[2] / n - md ** 2, 0.0)
        se = np.sqrt(var / n) if n > 1 else np.nan
        paired[rule] = dict(n=int(n), mean_diff=md, win_diff_rate=p[3] / n,
                            t=md / se if se and se > 0 else np.nan)
    out["paired_vs_open"] = paired
    return out


def _z_two_prop(a: dict, b: dict) -> float:
    """两比例 z 检验(a 胜率 vs b 胜率)。"""
    na, nb = a["filled"], b["filled"]
    if not na or not nb:
        return np.nan
    pa, pb = a["win_gross"], b["win_gross"]
    p = (pa * na + pb * nb) / (na + nb)
    se = np.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    return (pa - pb) / se if se > 0 else np.nan


# ────────────────────────────── CLI ──────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--screen", default="all", choices=["all", "momentum", "reversal"])
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--fill", default="both", choices=["marketable", "doc", "both"])
    ap.add_argument("--max-codes", type=int, default=None)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    payload = run(args.data_root, args.start, args.end, args.stride, args.screen,
                  args.cost_bps, args.fill, args.max_codes)
    summ = summarize(payload)
    os.makedirs(args.out_dir, exist_ok=True)
    tag = args.tag or f"{args.screen}_{args.fill}"
    out = os.path.join(args.out_dir, f"nextday_entry_{tag}.json")
    with open(out, "w") as fh:
        json.dump(summ, fh, ensure_ascii=False, indent=2, default=_json_default)
    logger.info("写出 %s", out)
    print(out)
    return 0


def _json_default(o):
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
