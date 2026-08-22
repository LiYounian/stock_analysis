"""DB01「首板次日回调低吸 + 情绪周期门」walk-forward 回测(预注册 §5 全实现)。

策略草案:docs/计划/AI策略挖掘实验/策略草案_v2.md(commit d698cf4)
审判放行:docs/计划/AI策略挖掘实验/审判_v2.md(commit bf96087)+ 3 条执行注记。

━━ 核心检验(命门 H6)━━
edge 在可交易流动性区间(成交额前 50% / ≥1亿)扣费后是否存活?若只活在成交额
后 50% 的不可交易尾部 → 判「账面真·不可交易」(同项目 C-2 反转照妖镜)。

━━ 防未来函数纪律(与 event_study / screen_forward 同源)━━
· 信号在 T 日只读 kdf.iloc[:t+1];入场锚 T+1 开盘(第 t+1 行 open);离场 T+2 开盘。
· regime 门用 T 日收盘(≤t 的全市场横截面)算,门控 T+1 入场(时序 OK)。
· 卖出/买入侧一字板/停牌不可成交 → 顺延至下一可成交日开盘,如实计被迫持有损益。
· 单测锁死:注入 t 之后极端数据不改变 T 时刻 regime 与信号(tests/test_db01_backtest.py)。

━━ 数据口径(§5.4 已与审判者+挖掘者对齐)━━
· 退市股:主档=当前在市快照,**不含**2018 以来已退市股 → 幸存者偏差,方向=高估 edge。
  结论全程带「幸存者偏差=系统性高估、正 edge 打折解读」声明。
· ST 排除:主档无 point-in-time 名称 → 靠制度涨停阈值隐式排除(ST 涨停 ±5% < 首板阈值,
  天然不进池)+ 动态护栏(近 60 日 ≥3 次 ±5% 特征剔除)。比名称快照更 point-in-time。

⚠️ 非投资建议。产物只写 worktree 本地,不写主检出。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger("backtest.db01")

WINDOWS_HOLD = (1,)              # E-base 主口径持仓 1 日(T+1 开盘买 → T+2 开盘卖)
_BJ_PREFIX = ("8", "4")

# ── 涨停制度阈值(board + date aware)────────────────────────────────
# 用 pct_chg ≥ 阈值 判涨停;留 0.2pct 缓冲吸收前复权除权导致的 pct_chg 微偏。
_GEM_20PCT_DATE = pd.Timestamp("2020-08-24")   # 创业板 20% 涨跌幅起始
_STAR_20PCT_DATE = pd.Timestamp("2019-07-22")  # 科创板 20% 涨跌幅起始
_STAMP_CUT_DATE = pd.Timestamp("2023-08-28")   # 印花税 0.10%→0.05%(2023-08-28 起单边 0.05%)


def board_of(code: str) -> str:
    """代码 → 板块。主板(600/601/603/605/000/001/002/003)/创业板(300/301)/科创板(688)。"""
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("30",)):
        return "gem"
    return "main"


def limit_up_threshold(code: str, date: pd.Timestamp) -> float:
    """该票该日的首板涨停判定阈值(%)。ST(±5%)不在此列 → 天然不达标、被隐式排除。"""
    b = board_of(code)
    if b == "star":
        return 19.8 if date >= _STAR_20PCT_DATE else 9.8
    if b == "gem":
        return 19.8 if date >= _GEM_20PCT_DATE else 9.8
    return 9.8


def stamp_tax_rate(date: pd.Timestamp) -> float:
    """印花税率(卖出侧单边):2023-08-28 起 0.05%,之前 0.10%。"""
    return 0.0005 if date >= _STAMP_CUT_DATE else 0.0010


# ── 每票预处理:涨停标记 + 连板计数(向量化,只用当日及之前)────────────
def annotate_limit(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """给单票 K 线加列:is_zt(是否涨停,board+date aware)、streak(连板计数)、
    zt_price(涨停价近似=前收×(1+阈值))、is_yiziban(一字板:open==high==low 且涨停)。

    连板计数口径(主流,待挖掘者确认微调):
      · streak = 连续涨停天数(今涨停则 = 昨 streak + 1,否则 0);
      · 停牌日(K 线本身缺 bar)不出现在 df 中 → 自然「不中断」相邻交易日连板;
      · 开板但收盘仍涨停(pct_chg ≥ 阈值)算该板(以收盘 pct_chg 判定,非盘中是否开板)。
    首板 ⟺ streak == 1。
    """
    d = df.copy()
    dates = pd.to_datetime(d["date"])
    thr = np.array([limit_up_threshold(code, dt) for dt in dates])
    pct = d["pct_chg"].to_numpy(dtype=float)
    is_zt = pct >= thr
    d["is_zt"] = is_zt
    # 连板计数(逐行,O(n))
    streak = np.zeros(len(d), dtype=int)
    run = 0
    for i in range(len(d)):
        run = run + 1 if is_zt[i] else 0
        streak[i] = run
    d["streak"] = streak
    # 涨停价近似 + 一字板
    prev_close = d["close"].shift(1)
    d["zt_price_approx"] = (prev_close * (1.0 + thr / 100.0)).round(2)
    o, h, l = d["open"].to_numpy(float), d["high"].to_numpy(float), d["low"].to_numpy(float)
    flat = (o == h) & (h == l)
    d["is_yiziban"] = is_zt & flat                       # 一字涨停(买不进)
    # 一字跌停(卖不出):open==high==low 且 pct_chg ≤ −阈值(用同板阈值,留 0.5 缓冲)
    d["is_yizi_down"] = flat & (pct <= -thr + 0.5)
    return d


def is_st_like(df: pd.DataFrame, t: int, lookback: int = 60,
               n_hits: int = 3) -> bool:
    """point-in-time 动态 ST 护栏:T 日近 lookback 日内出现 ≥ n_hits 次 [4.5%,5.5%] 涨幅
    且期间无 >9.5% 涨幅 → 疑似 ±5% 制度票(ST/退市整理),剔除。不依赖任何名称表。"""
    lo = max(0, t - lookback + 1)
    seg = df["pct_chg"].iloc[lo:t + 1].to_numpy(dtype=float)
    if seg.size == 0:
        return False
    hit5 = int(((seg >= 4.5) & (seg <= 5.5)).sum())
    hit10 = int((seg > 9.5).sum())
    return hit5 >= n_hits and hit10 == 0


# ── 数据加载(复用 screen_forward_common 的离线加载)──────────────────
def universe_codes(exclude_bj: bool = True) -> list[str]:
    from tools.backtest import screen_forward_common as sfc
    return sfc.universe_codes(exclude_bj=exclude_bj)


def load_klines(codes: list[str], min_bars: int = 80) -> dict[str, pd.DataFrame]:
    """加载全历史 K 线 + 预标注涨停/连板。丢弃历史不足 min_bars(§S5 上市≥60,留余量)。"""
    from tools.collectors import market
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
        out[c] = annotate_limit(df, c)
    logger.info("加载+标注 %d 只(跳过 %d,min_bars=%d)", len(out), skipped, min_bars)
    return out


# ── 情绪周期门(全市场横截面,逐日,只用 ≤t)──────────────────────────
def build_regime(klines: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """逐交易日全市场情绪面板 → regime。

    N_lu   = 当日涨停家数(全 A,在市股);H_max = 当日最高连板;
    晋级率 = 昨日连板(streak≥1)票中今日仍涨停(晋级)的比例。
    三态:强(N_lu≥60 且 晋级率≥60日中位)/ 中性(30≤N_lu<60)/ 弱退潮(N_lu<30 或 H_max 较昨降≥2)。
    仅 regime∈{强,中性} 放开开仓。

    返回 DataFrame index=date,列 [N_lu,H_max,promote,regime,gate_open]。
    """
    # 长表:每 (date, code) 的 is_zt / streak
    frames = []
    for c, d in klines.items():
        frames.append(pd.DataFrame({"date": d["date"], "code": c,
                                    "is_zt": d["is_zt"], "streak": d["streak"]}))
    long = pd.concat(frames, ignore_index=True)
    # 每日涨停家数 & 最高连板
    g = long.groupby("date")
    n_lu = g["is_zt"].sum().astype(int)
    h_max = g["streak"].max().astype(int)
    daily = pd.DataFrame({"N_lu": n_lu, "H_max": h_max}).sort_index()

    # 晋级率:昨日 streak≥1 的票今日是否仍 is_zt(晋级 k→k+1)
    zt_only = long[long["streak"] >= 1][["date", "code"]].copy()
    zt_only["prev_zt"] = True
    # 昨日涨停票集合 → 映射到"下一交易日"
    all_days = daily.index.tolist()
    next_day = {all_days[i]: all_days[i + 1] for i in range(len(all_days) - 1)}
    zt_only["target"] = zt_only["date"].map(next_day)
    # 今日 is_zt 集合(用于判晋级)
    zt_today = long[long["is_zt"]][["date", "code"]].copy()
    zt_today_set = zt_today.groupby("date")["code"].apply(set).to_dict()
    promote = {}
    for tgt, grp in zt_only.dropna(subset=["target"]).groupby("target"):
        prev_codes = set(grp["code"])
        today = zt_today_set.get(tgt, set())
        promote[tgt] = len(prev_codes & today) / len(prev_codes) if prev_codes else np.nan
    daily["promote"] = pd.Series(promote).reindex(daily.index)

    # regime 三态
    prom_med60 = daily["promote"].rolling(60, min_periods=20).median()
    h_prev = daily["H_max"].shift(1)
    regimes = []
    for dt, row in daily.iterrows():
        nlu, hm, pr = row["N_lu"], row["H_max"], row["promote"]
        med = prom_med60.loc[dt]
        drop2 = (not pd.isna(h_prev.loc[dt])) and (h_prev.loc[dt] - hm >= 2)
        if nlu < 30 or drop2:
            regimes.append("弱")
        elif nlu >= 60 and not pd.isna(pr) and not pd.isna(med) and pr >= med:
            regimes.append("强")
        elif 30 <= nlu < 60:
            regimes.append("中性")
        elif nlu >= 60:
            regimes.append("中性")   # N_lu≥60 但晋级率未达强 → 归中性(仍放开)
        else:
            regimes.append("弱")
    daily["regime"] = regimes
    daily["gate_open"] = daily["regime"].isin(["强", "中性"])
    return daily


# ── 信号扫描(SELECT §2.2,逐票逐日,只用 ≤t)────────────────────────
_AMT_MIN = 5e7          # S4 成交额硬下限 5000 万元
_TURN_MIN = 1.0        # S2 换手率下限 1%
_RUNUP_MAX = 0.20      # S3 前 20 日累计涨幅上限
_MIN_LIST_DAYS = 60    # S5 上市交易日下限


def scan_candidates(klines: dict[str, pd.DataFrame], regime: pd.DataFrame,
                    require_gate: bool = True) -> list[dict]:
    """逐票逐日扫 SELECT(S1–S5)+ ST 护栏 + regime 门 → 候选池。

    每候选 = {code,t,date_T,P0,turnover,amount,runup20,streak,regime}。
    留 t ≤ len-3(需 T+1 入场行、T+2 离场行)。防未来函数:只读 df.iloc[:t+1]。
    require_gate=True 时仅收 regime∈{强,中性};False 用于 H3「不加门」对照。
    """
    gate = regime["gate_open"].to_dict()
    reg_map = regime["regime"].to_dict()
    out: list[dict] = []
    for code, d in klines.items():
        n = len(d)
        if n < _MIN_LIST_DAYS + 3:
            continue
        is_zt = d["is_zt"].to_numpy()
        streak = d["streak"].to_numpy()
        low = d["low"].to_numpy(float)
        ztp = d["zt_price_approx"].to_numpy(float)
        turn = d["turnover"].to_numpy(float)
        amt = d["amount"].to_numpy(float)
        close = d["close"].to_numpy(float)
        dates = d["date"].tolist()
        for t in range(_MIN_LIST_DAYS, n - 2):
            if not (is_zt[t] and streak[t] == 1):          # S1 首板
                continue
            if not (low[t] < ztp[t]):                       # S2 换手板(非一字)
                continue
            if not (turn[t] >= _TURN_MIN):                  # S2 换手率下限
                continue
            if t < 20 or not np.isfinite(close[t - 20]) or close[t - 20] <= 0:
                continue
            if (close[t] / close[t - 20] - 1.0) > _RUNUP_MAX:  # S3 非高位末端
                continue
            if not (np.isfinite(amt[t]) and amt[t] >= _AMT_MIN):  # S4 流动性
                continue
            if is_st_like(d, t):                            # ST 动态护栏
                continue
            dt = dates[t]
            if require_gate and not gate.get(dt, False):     # regime 门
                continue
            out.append({
                "code": code, "t": t, "date_T": str(dt.date()),
                "P0": float(close[t]), "turnover": float(turn[t]),
                "amount": float(amt[t]), "streak": int(streak[t]),
                "runup20": float(close[t] / close[t - 20] - 1.0),
                "regime": reg_map.get(dt, "未知"),
            })
    return out


# ── 成本模型 ────────────────────────────────────────────────────────
_SLIP = 0.0020         # 单边滑点 0.20%(主口径,注记1:DB01 与基线A 同此成本)
_COMM = 0.0005         # 佣金单边 0.05%


def _round_trip_net(buy_open: float, sell_open: float, sell_date: pd.Timestamp,
                    slip: float = _SLIP) -> float:
    """扣费后净 round-trip 收益率(买卖两侧同成本模型)。
    买入实付 = buy_open×(1+slip+comm);卖出实收 = sell_open×(1−slip−comm−印花)。
    """
    buy_cost = buy_open * (1.0 + slip + _COMM)
    sell_net = sell_open * (1.0 - slip - _COMM - stamp_tax_rate(sell_date))
    return sell_net / buy_cost - 1.0


# ── 入场 / 离场(E-base + 成交概率折算 §2.3/§2.4)───────────────────
def _resolve_sell_idx(d: pd.DataFrame, want_idx: int) -> int | None:
    """卖出日成交概率折算:计划卖出行若一字跌停(open==high==low 且跌停)→ 卖不出,
    顺延到下一可成交行(非一字跌停)。停牌已由 df 行连续(缺 bar 天不在 df)隐式顺延。"""
    n = len(d)
    yizi_down = d["is_yizi_down"].to_numpy()
    idx = want_idx
    while idx < n:
        if not yizi_down[idx]:      # 非一字跌停 → 可成交
            return idx
        idx += 1
    return None


def simulate_trade(d: pd.DataFrame, t: int, slip: float = _SLIP,
                   apply_r_filter: bool = True) -> dict:
    """单笔 E-base:P0=close[t];T+1 开盘 r 判定入场;T+2 开盘卖(一字跌停顺延)。

    apply_r_filter=True → DB01(r∈[−5%,+3%] 才入场);False → 基线A(忽略 r 机械买)。
    返回 {入场,r,gross,net,buy_date,sell_date,sell_delayed,amount,regime,code,...}
    入场=False 表示 r 过滤未通过(不计入可下单集,但记 r 供统计放弃分布)。
    """
    P0 = float(d["close"].iloc[t])
    buy_open = float(d["open"].iloc[t + 1])
    r = buy_open / P0 - 1.0
    rec = {"r": round(r, 6), "入场": True, "gross": None, "net": None,
           "sell_delayed": False}
    if apply_r_filter and not (-0.05 <= r <= 0.03):
        rec["入场"] = False
        return rec
    # 卖出:E-base T+2 开盘,成交概率折算
    sell_idx = _resolve_sell_idx(d, t + 2)
    if sell_idx is None:
        rec["入场"] = False   # 无可成交卖出日(极端,罕见)
        return rec
    rec["sell_delayed"] = sell_idx != (t + 2)
    sell_open = float(d["open"].iloc[sell_idx])
    sell_date = d["date"].iloc[sell_idx]
    rec["gross"] = round(sell_open / buy_open - 1.0, 6)
    rec["net"] = round(_round_trip_net(buy_open, sell_open, sell_date, slip), 6)
    rec["buy_date"] = str(d["date"].iloc[t + 1].date())
    rec["sell_date"] = str(sell_date.date())
    return rec


# ── 统计工具(无 scipy 依赖:Welch t + 正态近似 p)──────────────────
import math


def _welch_t(a: list[float], b: list[float]) -> tuple[float, float]:
    """两独立样本均值差 Welch t 与双尾 p(正态近似,样本大 n>1e3 时 z≈t)。
    返回 (t, p)。a=处理组(DB01),b=对照组(基线A)。"""
    a = [x for x in a if x is not None]
    b = [x for x in b if x is not None]
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return float("nan"), float("nan")
    ma, mb = sum(a) / na, sum(b) / nb
    va = sum((x - ma) ** 2 for x in a) / (na - 1)
    vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return float("nan"), float("nan")
    t = (ma - mb) / se
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return round(t, 3), round(p, 5)


def _one_sample_t(a: list[float]) -> tuple[float, float]:
    """单样本均值 vs 0 的 t 与双尾 p(正态近似)。用于「净超额是否显著>0」。"""
    a = [x for x in a if x is not None]
    n = len(a)
    if n < 2:
        return float("nan"), float("nan")
    m = sum(a) / n
    v = sum((x - m) ** 2 for x in a) / (n - 1)
    se = math.sqrt(v / n)
    if se == 0:
        return float("nan"), float("nan")
    t = m / se
    p = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(t) / math.sqrt(2.0))))
    return round(t, 3), round(p, 5)


def _pct(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.3f}%"


def _stat_block(nets: list[float]) -> dict:
    """一组净收益 → {N,均值,中位,胜率,盈亏比,std,t,p}。"""
    v = [x for x in nets if x is not None]
    n = len(v)
    if n == 0:
        return {"N": 0}
    arr = np.array(v, dtype=float)
    wins = arr[arr > 0]; losses = arr[arr < 0]
    pl = (wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else None
    t, p = _one_sample_t(v)
    return {"N": n, "均值": round(float(arr.mean()), 6), "中位": round(float(np.median(arr)), 6),
            "胜率": round(float((arr > 0).mean()), 4), "盈亏比": round(pl, 3) if pl else None,
            "std": round(float(arr.std(ddof=1)), 6) if n > 1 else None,
            "t_vs0": t, "p_vs0": p}


# ── 组合日频(H2 相关 / H3 Sharpe / 最大回撤)────────────────────────
def portfolio_daily(trades: list[dict]) -> pd.Series:
    """把每笔 net 按买入日等权归集为组合日频收益(持仓 1 日,当日多笔取均值)。
    空仓日不计(不补 0)——用于笔级 Sharpe;与 HS300 对齐时再 reindex。"""
    rows = [(tr["buy_date"], tr["net"]) for tr in trades
            if tr.get("入场") and tr.get("net") is not None]
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["date", "net"])
    return df.groupby("date")["net"].mean().sort_index()


def _sharpe_maxdd(daily: pd.Series) -> dict:
    from tools.backtest import metrics
    if daily is None or len(daily) < 2:
        return {"Sharpe": None, "最大回撤": None, "交易日数": len(daily) if daily is not None else 0}
    eq = (1.0 + daily).cumprod()
    return {"Sharpe": round(metrics.sharpe(daily), 3),
            "最大回撤": round(metrics.max_drawdown(eq), 4),
            "交易日数": int(len(daily))}


# ── 主回测:DB01 + 基线A,分层聚合,H1–H6 判定 ──────────────────────
def run_backtest(klines: dict[str, pd.DataFrame], regime: pd.DataFrame,
                 oos_split: str = "2024-01-01") -> dict:
    """全流程。返回结构化结果 dict(供报告渲染 + 断言测试)。"""
    cands = scan_candidates(klines, regime, require_gate=True)
    logger.info("候选池(S1–S5+门):%d 笔", len(cands))

    # 逐候选跑 DB01(r 过滤)+ 基线A(忽略 r,同成本)
    db_trades, baseA_trades = [], []
    n_delayed = n_r_reject = 0
    for c in cands:
        d = klines[c["code"]]; t = c["t"]
        tr = simulate_trade(d, t, apply_r_filter=True)
        base = simulate_trade(d, t, apply_r_filter=False)
        for x, src in ((tr, c), (base, c)):
            x.update({"code": c["code"], "date_T": c["date_T"], "amount": c["amount"],
                      "regime": c["regime"], "turnover": c["turnover"]})
        if tr["入场"]:
            db_trades.append(tr)
            if tr["sell_delayed"]:
                n_delayed += 1
        else:
            n_r_reject += 1
        if base["入场"] and base["net"] is not None:
            baseA_trades.append(base)

    db_nets = [x["net"] for x in db_trades if x["net"] is not None]
    baseA_nets = [x["net"] for x in baseA_trades]
    db_gross = [x["gross"] for x in db_trades if x["gross"] is not None]

    # H1:DB01(净) vs 基线A(净)超额 + Welch t
    excess = (np.mean(db_nets) - np.mean(baseA_nets)) if db_nets and baseA_nets else None
    h1_t, h1_p = _welch_t(db_nets, baseA_nets)

    # H4:全A 扣费净均超额>0(相对基线A);同时报毛超额佐证成本敏感度
    excess_gross = (np.mean(db_gross) - np.mean([x["gross"] for x in baseA_trades])) \
        if db_gross and baseA_trades else None

    # H6(命门):可交易层 = 当日成交额前 50% 或 ≥1亿;各层扣费净超额
    layers = _liquidity_layers(db_trades, baseA_trades)

    # 容量曲线:amount 门槛递增
    capacity = _capacity_curve(db_trades, baseA_trades)

    # regime 分层
    reg_layers = {}
    for rg in ("强", "中性"):
        sub = [x["net"] for x in db_trades if x["regime"] == rg and x["net"] is not None]
        reg_layers[rg] = _stat_block(sub)

    # H2:组合日频 vs HS300 相关
    daily = portfolio_daily(db_trades)
    h2_corr = _corr_vs_hs300(daily)

    # H3:加门 vs 不加门 同样本 Sharpe
    h3 = _gate_effect(klines, regime)

    # H5:OOS(样本内 2018–2023 vs 准样本外 2024–2026 vs 2026 更盲 holdout)
    h5 = _oos_split(db_trades, baseA_trades, oos_split)

    # 净收益分布分位(照妖镜:是否少数极端标的主导)
    dist = _net_distribution(db_nets)

    result = {
        "策略": "DB01 首板次日回调低吸+情绪周期门",
        "样本区间": (f"{min(c['date_T'] for c in cands)} → {max(c['date_T'] for c in cands)}"
                   if cands else "空"),
        "候选池笔数": len(cands),
        "DB01入场笔数": len(db_nets), "基线A笔数": len(baseA_nets),
        "r过滤放弃笔数": n_r_reject,
        "卖出侧一字跌停顺延笔数": n_delayed,
        "卖出顺延比例": round(n_delayed / len(db_nets), 4) if db_nets else None,
        "成本模型": f"滑点{_SLIP*100:.2f}%/side + 佣金{_COMM*100:.2f}%/side + 印花(date-aware)",
        "── DB01(净)": _stat_block(db_nets),
        "── 基线A(净)": _stat_block(baseA_nets),
        "── DB01(毛)": _stat_block(db_gross),
        "── 基线A(毛)": _stat_block([x["gross"] for x in baseA_trades if x.get("gross") is not None]),
        "H1_回调择时净增量": {
            "净超额": round(excess, 6) if excess is not None else None,
            "毛超额": round(excess_gross, 6) if excess_gross is not None else None,
            "Welch_t": h1_t, "p": h1_p,
            "成立": bool(excess is not None and excess > 0 and h1_p < 0.05),
        },
        "H2_alpha非beta": {"与HS300相关": h2_corr,
                          "成立": bool(h2_corr is not None and abs(h2_corr) < 0.3)},
        "H3_情绪门有用": h3,
        "H4_成本存活全A": {
            "净均超额": round(excess, 6) if excess is not None else None,
            "毛均超额": round(excess_gross, 6) if excess_gross is not None else None,
            "成立": bool(excess is not None and excess > 0),
        },
        "H5_样本外方向一致": h5,
        "H6_可交易层存活(命门)": layers,
        "容量曲线": capacity,
        "regime分层": reg_layers,
        "净收益分布": dist,
        "组合日频": _sharpe_maxdd(daily),
    }
    # 成立门槛 = H1 ∧ H4 ∧ H5 ∧ H6
    result["综合判定"] = _final_verdict(result)
    return result


def _liquidity_layers(db_trades, baseA_trades) -> dict:
    """H6 命门:按当日横截面成交额分位,DB01 与基线A 分「可交易层(前50%/≥1亿)」
    与「尾部(后50%)」,各报扣费净均超额 + t。edge 只活尾部 → 判不可交易。"""
    # 每笔按 date_T 当日候选内成交额分位;≥1亿 绝对阈值另算
    def _by_day_quantile(trades):
        df = pd.DataFrame([{"date": x["date_T"], "amount": x["amount"], "net": x["net"]}
                           for x in trades if x.get("net") is not None])
        if df.empty:
            return df
        df["q"] = df.groupby("date")["amount"].rank(pct=True)
        return df
    db = _by_day_quantile(db_trades)
    ba = _by_day_quantile(baseA_trades)
    out = {}
    if db.empty or ba.empty:
        return {"error": "空样本"}
    for name, mask_db, mask_ba in [
        ("成交额前50%", db["q"] >= 0.5, ba["q"] >= 0.5),
        ("成交额后50%", db["q"] < 0.5, ba["q"] < 0.5),
        ("成交额≥1亿", db["amount"] >= 1e8, ba["amount"] >= 1e8),
        ("成交额≥2亿", db["amount"] >= 2e8, ba["amount"] >= 2e8),
    ]:
        dn = db[mask_db]["net"].tolist(); bn = ba[mask_ba]["net"].tolist()
        exc = (np.mean(dn) - np.mean(bn)) if dn and bn else None
        t, p = _welch_t(dn, bn)
        out[name] = {"DB01_N": len(dn), "DB01净均": round(float(np.mean(dn)), 6) if dn else None,
                     "净超额vs基线A": round(exc, 6) if exc is not None else None,
                     "t": t, "p": p,
                     "净超额显著>0": bool(exc is not None and exc > 0 and p < 0.05)}
    return out


def _capacity_curve(db_trades, baseA_trades) -> list:
    """容量–收益曲线:成交额门槛递增,各档 DB01 扣费净均 + 净超额。看 edge 是否随可交易性消失。"""
    out = []
    for thr, label in [(5e7, "≥5千万"), (1e8, "≥1亿"), (2e8, "≥2亿"), (5e8, "≥5亿"), (1e9, "≥10亿")]:
        dn = [x["net"] for x in db_trades if x.get("net") is not None and x["amount"] >= thr]
        bn = [x["net"] for x in baseA_trades if x["amount"] >= thr]
        exc = (np.mean(dn) - np.mean(bn)) if dn and bn else None
        out.append({"门槛": label, "DB01_N": len(dn),
                    "DB01净均": round(float(np.mean(dn)), 6) if dn else None,
                    "净超额": round(exc, 6) if exc is not None else None})
    return out


def _net_distribution(nets: list[float]) -> dict:
    if not nets:
        return {}
    arr = np.array(nets, dtype=float)
    qs = {f"p{q}": round(float(np.percentile(arr, q)), 5) for q in (1, 5, 25, 50, 75, 95, 99)}
    # 尾部主导检验:去掉最好 5% 后均值
    n = len(arr); cut = max(1, int(n * 0.05))
    trimmed = np.sort(arr)[:-cut]
    qs["去顶5%后均值"] = round(float(trimmed.mean()), 6)
    qs["全样本均值"] = round(float(arr.mean()), 6)
    return qs


def _corr_vs_hs300(daily: pd.Series):
    if daily is None or len(daily) < 20:
        return None
    try:
        from tools.backtest import screen_forward_common as sfc
        hs = sfc.load_hs300()
    except Exception:
        return None
    hs = hs.copy(); hs["date"] = pd.to_datetime(hs["date"])
    hs["ret"] = hs["close"].pct_change()
    hs_ret = hs.set_index(hs["date"].dt.strftime("%Y-%m-%d"))["ret"]
    j = pd.DataFrame({"db": daily, "hs": hs_ret}).dropna()
    if len(j) < 20:
        return None
    return round(float(j["db"].corr(j["hs"])), 4)


def _gate_effect(klines, regime) -> dict:
    """H3:加门 vs 不加门(同 candidate 全集,只差 regime 过滤)组合日频 Sharpe。"""
    cg = scan_candidates(klines, regime, require_gate=True)
    cn = scan_candidates(klines, regime, require_gate=False)
    def _daily(cands):
        trs = []
        for c in cands:
            tr = simulate_trade(klines[c["code"]], c["t"], apply_r_filter=True)
            if tr["入场"] and tr["net"] is not None:
                tr["buy_date"] = tr.get("buy_date"); trs.append(tr)
        return portfolio_daily(trs)
    sg = _sharpe_maxdd(_daily(cg))
    sn = _sharpe_maxdd(_daily(cn))
    imp = (sg["Sharpe"] - sn["Sharpe"]) if sg["Sharpe"] is not None and sn["Sharpe"] is not None else None
    return {"加门Sharpe": sg["Sharpe"], "不加门Sharpe": sn["Sharpe"],
            "加门笔数": len(cg), "不加门笔数": len(cn),
            "Sharpe提升": round(imp, 3) if imp is not None else None,
            "成立": bool(imp is not None and imp > 0)}


def _oos_split(db_trades, baseA_trades, split: str) -> dict:
    """H5:样本内(<split)/ 准样本外(≥split)/ 2026 更盲 holdout,各净超额方向。"""
    def _seg(lo, hi):
        dn = [x["net"] for x in db_trades if x.get("net") is not None and lo <= x["date_T"] < hi]
        bn = [x["net"] for x in baseA_trades if lo <= x["date_T"] < hi]
        exc = (np.mean(dn) - np.mean(bn)) if dn and bn else None
        return {"DB01_N": len(dn), "DB01净均": round(float(np.mean(dn)), 6) if dn else None,
                "净超额": round(exc, 6) if exc is not None else None}
    seg_in = _seg("0000", split)
    seg_oos = _seg(split, "2026-01-01")
    seg_hold = _seg("2026-01-01", "9999")
    in_exc = seg_in.get("净超额"); oos_exc = seg_oos.get("净超额")
    consistent = bool(in_exc is not None and oos_exc is not None
                      and (in_exc > 0) == (oos_exc > 0) and oos_exc > 0)
    return {"样本内(2018–2023)": seg_in, "准样本外(2024–2025)": seg_oos,
            "更盲holdout(2026至今)": seg_hold, "方向一致且OOS>0": consistent}


def _final_verdict(r: dict) -> dict:
    h1 = r["H1_回调择时净增量"]["成立"]
    h4 = r["H4_成本存活全A"]["成立"]
    h5 = r["H5_样本外方向一致"]["方向一致且OOS>0"]
    h6_layer = r["H6_可交易层存活(命门)"]
    h6 = bool(isinstance(h6_layer, dict) and
              h6_layer.get("成交额前50%", {}).get("净超额显著>0"))
    passed = h1 and h4 and h5 and h6
    return {"H1": h1, "H4": h4, "H5": h5, "H6命门": h6,
            "成立门槛H1∧H4∧H5∧H6": passed,
            "结论": ("✅ 新且经回测检验" if passed else
                    ("🔴 H6否定→账面真·不可交易(同C-2反转陷阱)" if (h1 and not h6)
                     else "🔴 不成立")),
            "幸存者偏差声明": "主档不含退市股→edge系统性高估,以上正向结论须打折解读"}


def run_full(universe_limit: int | None = None, oos_split: str = "2024-01-01") -> dict:
    """全A 端到端:加载→情绪门(优先读缓存)→回测。universe_limit 仅调试用。"""
    codes = universe_codes()
    if universe_limit:
        codes = codes[:universe_limit]
    kl = load_klines(codes)
    try:
        regime = pd.read_parquet("data/backtest_local/db01_regime.parquet")
        if universe_limit:  # 子样本调试时重算,避免用全A门错配
            regime = build_regime(kl)
    except FileNotFoundError:
        regime = build_regime(kl)
    return run_backtest(kl, regime, oos_split=oos_split)


def _selftest():
    """小规模自测:验证涨停判定/连板/情绪门在真实数据上合理(非断言,肉眼核对)。"""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    codes = universe_codes()[:300]
    kl = load_klines(codes)
    print(f"加载 {len(kl)} 只")
    # 抽一只看首板/连板
    c0 = next(iter(kl))
    d = kl[c0]
    zt = d[d["is_zt"]]
    print(f"{c0}: 涨停日 {len(zt)} 个,最大连板 {d['streak'].max()}")
    print(d[d["streak"] >= 2][["date", "pct_chg", "streak"]].head(8).to_string())
    reg = build_regime(kl)
    print("\n情绪门(前300只子样本,近20日):")
    print(reg.tail(20)[["N_lu", "H_max", "promote", "regime"]].to_string())
    print("\nregime 分布:", reg["regime"].value_counts().to_dict())


def _main(argv=None):
    import argparse
    import json
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="DB01 首板次日回调低吸 walk-forward 回测")
    ap.add_argument("--selftest", action="store_true", help="根基自测(涨停/连板/情绪门)")
    ap.add_argument("--limit", type=int, help="仅取前 N 只(调试)")
    ap.add_argument("--oos-split", default="2024-01-01", help="样本内/OOS 分界")
    ap.add_argument("--out", help="结果 JSON 落盘路径")
    a = ap.parse_args(argv)
    if a.selftest:
        _selftest(); return 0
    r = run_full(universe_limit=a.limit, oos_split=a.oos_split)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, default=str)
        logger.info("结果落盘 %s", a.out)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
