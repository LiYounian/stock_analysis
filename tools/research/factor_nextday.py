"""横截面因子回测(次日实盘口径, walk-forward, 全A) —— 新策略挖掘用。

对每个预注册因子:每交易日横截面按因子值排十分位(decile),在次日实盘口径下
(marketable 限价回踩 limit_pc_0.01 + 涨停买入过滤 + D+1收盘卖 + 扣成本)测每档的
净绝对收益 / 胜率 / α(vs 全A等权 open→close),看单调性、OOS、多市场态、增量。

口径与防未来(硬红线,同 nextday_entry 底座):
  · 信号日 t 只用 ≤t 信息;入场在 D+1=t+1,成交/收益只用 t+1 已确定 K 线。
  · close[t+1] 仅作结果标签,绝不回喂入选/入场判定。
  · 所有因子(隔夜动量/日内反转/换手冲击/低IVOL/动量基准)均 as-of ≤t 滚动窗口。
  · 因子/分档/判据预注册(见 2026-09-15_选股风险审计与新策略挖掘报告.md §5),跑完不改。
  · 涨停买入过滤:D+1 open≥昨收×(1+涨停幅−0.5%) 或一字板(high==low 且涨) → 买不进,剔除。
    (注:主口径 limit_pc_0.01 挂价=昨收×0.99,gap-up 到涨停时天然未触发,故过滤主要影响 open 档。)

产物只写 --out-dir(worktree本地);--data-root 指主仓只读。
⚠️ 测试环境研究模拟,非投资建议。

用法:
  python -m tools.research.factor_nextday --data-root <主仓> --out-dir DIR \
      [--start 2018-01-01] [--end 2026-09-14] [--stride 2] [--cost-bps 10] \
      [--entry limit_pc_0.01|open] [--min-names 50] [--max-codes N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger("research.factor_nextday")

_BJ_PREFIX = ("8", "4")
OOS_SPLIT = "2023-01-01"
REGIME_LABELS = ("普跌", "震荡", "普涨")
N_DECILE = 10

# 预注册因子(方向:值越大越"多头/该买")。跑前写死,跑完不改。
FACTORS = (
    "BENCH_MOM",       # 动量基准(正确性锚+增量对照):近20日收益
    "C1_ONMOM",        # 隔夜动量:近20日隔夜收益累计
    "C1_IDREV",        # 日内反转:−近5日日内收益均值
    "C2_TOSHOCK_NEG",  # 换手冲击反转:−(换手/20日均−1)
    "C3_LOWIVOL",      # 低特质波动:−近20日日收益标准差
)


def board_limit(code: str) -> float:
    """板块涨跌停幅(ST 5% 不可识别,按主板 10% 处理,结论标注不确定度)。"""
    if code[:2] in ("30", "68"):   # 创业板 / 科创板
        return 0.20
    return 0.10


# ────────────────────────────── 数据加载 ──────────────────────────────
def universe_codes(data_root: str) -> list[str]:
    kdir = os.path.join(data_root, "data", "master", "kline")
    codes = [fn[:-8] for fn in os.listdir(kdir) if fn.endswith(".parquet")]
    return sorted(c for c in codes if c[:1] not in _BJ_PREFIX)


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


# ────────────────────────────── 单票预算(全 as-of) ──────────────────────────────
def precompute(df: pd.DataFrame) -> dict:
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    to = df["turnover"].to_numpy(float) if "turnover" in df.columns else np.full(len(c), np.nan)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    n = len(c)

    prev = np.empty(n); prev[0] = np.nan; prev[1:] = c[:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        ir = c / o - 1.0                 # 当日 open→close(市场基准/收益用)
        ret_cc = c / prev - 1.0          # 昨收→今收(β/regime/IVOL 用)
        overnight = o / prev - 1.0       # 隔夜 昨收→今开
        intraday = c / o - 1.0           # 日内 今开→今收
    ir[~(o > 0)] = np.nan
    overnight[~(prev > 0)] = np.nan
    intraday[~(o > 0)] = np.nan

    sc = pd.Series(c)
    ma20 = sc.rolling(20, min_periods=20).mean().to_numpy()
    ma60 = sc.rolling(60, min_periods=60).mean().to_numpy()

    def past_ret(k):
        r = np.full(n, np.nan)
        if n > k:
            r[k:] = c[k:] / c[:-k] - 1.0
        return r
    ret20 = past_ret(20)

    # 预注册因子(全部滚动窗口结束于 t,只用 ≤t)
    on_mom = pd.Series(overnight).rolling(20, min_periods=15).sum().to_numpy()
    id_rev = -pd.Series(intraday).rolling(5, min_periods=4).mean().to_numpy()
    to_ma20 = pd.Series(to).rolling(20, min_periods=10).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        to_shock_neg = -(to / to_ma20 - 1.0)
    to_shock_neg[~(to_ma20 > 0)] = np.nan
    low_ivol = -pd.Series(ret_cc).rolling(20, min_periods=15).std().to_numpy()

    factors = {
        "BENCH_MOM": ret20,
        "C1_ONMOM": on_mom,
        "C1_IDREV": id_rev,
        "C2_TOSHOCK_NEG": to_shock_neg,
        "C3_LOWIVOL": low_ivol,
    }
    # 动量代理布尔掩码(复现 R4 底座 +0.33%α 正确性锚)
    with np.errstate(invalid="ignore"):
        mom_proxy = (~np.isnan(ma20)) & (~np.isnan(ma60)) & (c > ma20) & (ma20 > ma60) & (ret20 > 0)

    return dict(dates=dates, o=o, h=h, lo=lo, c=c, prev=prev, ir=ir, ret_cc=ret_cc,
                factors=factors, mom_proxy=mom_proxy, n=n)


# ────────────────────────────── 市场基准 + regime ──────────────────────────────
def build_market(feats: dict) -> dict:
    acc_ir: dict[str, list] = {}
    acc_cc: dict[str, list] = {}
    for f in feats.values():
        d = f["dates"]; ir = f["ir"]; cc = f["ret_cc"]
        for j in np.nonzero(~np.isnan(ir))[0]:
            cell = acc_ir.get(d[j]); acc_ir[d[j]] = [cell[0] + ir[j], cell[1] + 1] if cell else [float(ir[j]), 1]
        for j in np.nonzero(~np.isnan(cc))[0]:
            cell = acc_cc.get(d[j]); acc_cc[d[j]] = [cell[0] + cc[j], cell[1] + 1] if cell else [float(cc[j]), 1]
    mkt_ir = {d: s / n for d, (s, n) in acc_ir.items() if n}
    mkt_cc = {d: s / n for d, (s, n) in acc_cc.items() if n}
    return dict(mkt_ir=mkt_ir, mkt_cc=mkt_cc)


def regime_asof_series(mkt_cc: dict) -> dict:
    """as-of regime:≤t 全A等权 5 日累计收益全局三分位 → 普跌/震荡/普涨。"""
    days = sorted(mkt_cc.keys())
    cc = np.array([mkt_cc[d] for d in days])
    cum5 = np.full(len(days), np.nan)
    for i in range(len(days)):
        cum5[i] = np.prod(1.0 + cc[max(0, i - 4):i + 1]) - 1.0
    valid = ~np.isnan(cum5)
    q1, q2 = np.quantile(cum5[valid], [1 / 3, 2 / 3])
    lab = np.where(cum5 <= q1, 0, np.where(cum5 >= q2, 2, 1))
    return {d: int(lab[i]) for i, d in enumerate(days)}


# ────────────────────────────── 成交(marketable + 涨停过滤) ──────────────────────────────
def fill_and_return(P, o_n, h_n, l_n, c_n, close_t, limit, cost):
    """marketable 限价成交 + 涨停买入过滤 → (net_ret, gross_ret, filled_mask)。

    · open≤P → 成交于 open;否则 low≤P → 成交于 P;否则未触发。
    · 涨停买入过滤:open≥昨收×(1+limit−0.005) 或 一字板(high==low 且 open>昨收) → 剔除。
    · net = (1+gross)×(1−cost)−1。
    """
    fill = np.full(len(P), np.nan)
    take_open = o_n <= P
    fill[take_open] = o_n[take_open]
    rest = ~take_open & (l_n <= P)
    fill[rest] = P[rest]
    # 涨停买入过滤(买不进)
    limit_up_gap = o_n >= close_t * (1.0 + limit - 0.005)
    one_word = (h_n == l_n) & (o_n > close_t)
    unbuyable = limit_up_gap | one_word
    fill[unbuyable] = np.nan
    filled = ~np.isnan(fill)
    gross = np.full(len(P), np.nan)
    gross[filled] = c_n[filled] / fill[filled] - 1.0
    net = np.full(len(P), np.nan)
    net[filled] = (1.0 + gross[filled]) * (1.0 - cost) - 1.0
    return net, gross, filled


# ────────────────────────────── 主流程 ──────────────────────────────
def collect_records(data_root, start, end, stride, entry, cost_bps, max_codes):
    """扫全A → 每个(code,信号日t)一条记录:因子值 + 该单在固定入场规则下的成交结果。

    入场规则对所有因子相同 → 成交结果算一次,再用不同因子分别排 decile。
    """
    codes = universe_codes(data_root)
    if max_codes:
        codes = codes[:max_codes]
    logger.info("加载 %d 票 ...", len(codes))
    feats: dict[str, dict] = {}
    for i, code in enumerate(codes):
        df = load_kline(data_root, code)
        if df is None or len(df) < 120:
            continue
        feats[code] = precompute(df)
        if (i + 1) % 1500 == 0:
            logger.info("  ...%d/%d", i + 1, len(codes))
    logger.info("有效票 %d,建市场基准 ...", len(feats))
    mkt = build_market(feats)
    mkt_ir, mkt_cc = mkt["mkt_ir"], mkt["mkt_cc"]
    regime = regime_asof_series(mkt_cc)
    cost = cost_bps / 1e4

    rows = []  # 每条:dict
    n_sig = 0
    for ci, (code, feat) in enumerate(feats.items()):
        dates = feat["dates"]; n = feat["n"]
        limit = board_limit(code)
        t_all = np.arange(n - 1)
        keep = np.ones(len(t_all), dtype=bool)
        if start is not None:
            keep &= dates[t_all] >= start
        if end is not None:
            keep &= dates[t_all] <= end
        t_idx = t_all[keep]
        if stride > 1:
            t_idx = t_idx[::stride]
        if len(t_idx) == 0:
            continue
        o_n = feat["o"][t_idx + 1]; h_n = feat["h"][t_idx + 1]
        l_n = feat["lo"][t_idx + 1]; c_n = feat["c"][t_idx + 1]
        close_t = feat["c"][t_idx]
        exec_dates = dates[t_idx + 1]
        m_ir = np.array([mkt_ir.get(d, np.nan) for d in exec_dates])
        valid = (o_n > 0) & (c_n > 0) & (h_n > 0) & (l_n > 0) & (close_t > 0) & ~np.isnan(m_ir)
        if not valid.any():
            continue
        t_idx = t_idx[valid]; o_n = o_n[valid]; h_n = h_n[valid]; l_n = l_n[valid]
        c_n = c_n[valid]; close_t = close_t[valid]; m_ir = m_ir[valid]

        # 挂价
        if entry == "open":
            P = o_n.copy()
        elif entry.startswith("limit_pc_"):
            k = float(entry.split("_")[-1]); P = close_t * (1.0 - k)
        else:
            raise ValueError(entry)
        net, gross, filled = fill_and_return(P, o_n, h_n, l_n, c_n, close_t, limit, cost)
        alpha = np.full(len(net), np.nan)
        alpha[filled] = gross[filled] - m_ir[filled]   # α 用 gross(与等权 gross 同口径)

        sig_dates = feat["dates"][t_idx]
        period = (sig_dates >= OOS_SPLIT).astype(int)
        reg = np.array([regime.get(d, 1) for d in sig_dates])
        mom_proxy = feat["mom_proxy"][t_idx]
        for fi in range(len(t_idx)):
            rec = dict(date=sig_dates[fi], code=code, period=int(period[fi]),
                       regime=int(reg[fi]), filled=bool(filled[fi]),
                       net=float(net[fi]) if filled[fi] else np.nan,
                       gross=float(gross[fi]) if filled[fi] else np.nan,
                       alpha=float(alpha[fi]) if filled[fi] else np.nan,
                       mom_proxy=bool(mom_proxy[fi]))
            for fac in FACTORS:
                v = feat["factors"][fac][t_idx[fi]]
                rec[fac] = float(v) if np.isfinite(v) else np.nan
            rows.append(rec)
        n_sig += len(t_idx)
        if (ci + 1) % 1500 == 0:
            logger.info("  扫 ...%d/%d 票, 累计信号 %d", ci + 1, len(feats), n_sig)
    logger.info("完成:%d 票, %d 记录", len(feats), len(rows))
    df = pd.DataFrame(rows)
    return df, dict(n_codes=len(feats), n_records=len(df))


# ────────────────────────────── 汇总 ──────────────────────────────
def _agg(sub: pd.DataFrame) -> dict:
    f = sub[sub["filled"]]
    if len(f) == 0:
        return dict(triggered=int(len(sub)), filled=0, trigger_rate=np.nan,
                    win_net=np.nan, mean_net=np.nan, mean_alpha=np.nan)
    return dict(
        triggered=int(len(sub)), filled=int(len(f)),
        trigger_rate=len(f) / len(sub),
        win_net=float((f["net"] > 0).mean()),
        mean_net=float(f["net"].mean()),
        mean_alpha=float(f["alpha"].mean()),
    )


def decile_table(df: pd.DataFrame, factor: str, min_names: int) -> dict:
    """按因子每日横截面分 decile(1=最低..10=最高),汇总各档 + D10−D1 + OOS + regime。"""
    d = df[df[factor].notna()].copy()
    # 每日 decile(需当日有效名数 ≥ min_names)
    def _decile(g):
        if len(g) < min_names:
            return pd.Series(np.nan, index=g.index)
        try:
            return pd.qcut(g[factor].rank(method="first"), N_DECILE, labels=False) + 1
        except ValueError:
            return pd.Series(np.nan, index=g.index)
    d["decile"] = d.groupby("date", group_keys=False).apply(_decile)
    d = d[d["decile"].notna()]
    d["decile"] = d["decile"].astype(int)

    out = {"ALL": {}, "OOS_晚段": {}, "regime": {}}
    for dec in range(1, N_DECILE + 1):
        out["ALL"][f"D{dec}"] = _agg(d[d["decile"] == dec])
        out["OOS_晚段"][f"D{dec}"] = _agg(d[(d["decile"] == dec) & (d["period"] == 1)])
    # D10 分 regime
    d10 = d[d["decile"] == N_DECILE]
    for ri, rlab in enumerate(REGIME_LABELS):
        out["regime"][rlab] = _agg(d10[d10["regime"] == ri])
    # 早段 D10(OOS 对照)
    out["IS_早段_D10"] = _agg(d[(d["decile"] == N_DECILE) & (d["period"] == 0)])
    # D10−D1 净价差
    def spread(sub):
        a = _agg(sub[sub["decile"] == N_DECILE]); b = _agg(sub[sub["decile"] == 1])
        if a["filled"] and b["filled"]:
            return dict(mean_net=a["mean_net"] - b["mean_net"],
                        mean_alpha=a["mean_alpha"] - b["mean_alpha"])
        return dict(mean_net=np.nan, mean_alpha=np.nan)
    out["D10_minus_D1"] = dict(ALL=spread(d), OOS=spread(d[d["period"] == 1]))
    # IC:每日 factor vs net 的 rank-IC(spearman = pearson of ranks;避免 scipy 依赖)
    f = d[d["filled"]]
    def _rank_ic(g):
        if len(g) < min_names:
            return np.nan
        return g[factor].rank().corr(g["net"].rank())  # pearson of ranks
    ics = f.groupby("date").apply(_rank_ic)
    ics = ics.dropna()
    out["IC"] = dict(mean=float(ics.mean()) if len(ics) else np.nan,
                     t=float(ics.mean() / (ics.std() / np.sqrt(len(ics)))) if len(ics) > 1 and ics.std() > 0 else np.nan,
                     n_days=int(len(ics)))
    return out


def anchor_check(df: pd.DataFrame) -> dict:
    """正确性锚:动量代理掩码内的 net/α,应 ≈ +0.15%/+0.33%(R4 底座)。"""
    m = df[df["mom_proxy"] & df["filled"]]
    return dict(n=int(len(m)),
                mean_net=float(m["net"].mean()) if len(m) else np.nan,
                mean_alpha=float(m["alpha"].mean()) if len(m) else np.nan,
                win_net=float((m["net"] > 0).mean()) if len(m) else np.nan)


def overlap_matrix(df: pd.DataFrame, min_names: int) -> dict:
    """各因子 top-decile 成员集的日均 Jaccard 重叠(拥挤度/分散度)。"""
    # 每因子每日 top-decile 的 code 集
    top: dict[str, dict] = {}
    for fac in FACTORS:
        d = df[df[fac].notna()]
        sets: dict[str, set] = {}
        for date, g in d.groupby("date"):
            if len(g) < min_names:
                continue
            thr = g[fac].quantile(0.9)
            sets[date] = set(g[g[fac] >= thr]["code"])
        top[fac] = sets
    mat = {}
    for i, a in enumerate(FACTORS):
        for b in FACTORS[i:]:
            days = set(top[a]) & set(top[b])
            js = []
            for day in days:
                sa, sb = top[a][day], top[b][day]
                u = sa | sb
                if u:
                    js.append(len(sa & sb) / len(u))
            mat[f"{a}∩{b}"] = dict(mean_jaccard=float(np.mean(js)) if js else np.nan,
                                   n_days=len(js))
    return mat


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", required=True)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--cost-bps", type=float, default=10.0)
    ap.add_argument("--entry", default="limit_pc_0.01")
    ap.add_argument("--min-names", type=int, default=50)
    ap.add_argument("--max-codes", type=int, default=None)
    ap.add_argument("--tag", default=None)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    df, meta = collect_records(args.data_root, args.start, args.end, args.stride,
                               args.entry, args.cost_bps, args.max_codes)
    summ = dict(meta=dict(**meta, entry=args.entry, cost_bps=args.cost_bps,
                          stride=args.stride, start=args.start, end=args.end,
                          min_names=args.min_names),
                anchor=anchor_check(df),
                factors={fac: decile_table(df, fac, args.min_names) for fac in FACTORS},
                overlap_jaccard=overlap_matrix(df, args.min_names))
    os.makedirs(args.out_dir, exist_ok=True)
    tag = args.tag or f"{args.entry}_{args.cost_bps:.0f}bps"
    out = os.path.join(args.out_dir, f"factor_nextday_{tag}.json")
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
