"""市场广度聚合器(纯本地,不触网)——大盘预测的"广度"维。

扫 master/kline 全A日K,逐交易日横截面聚合:
  · 涨跌家数(adv/dec/flat)与净涨占比
  · 涨停/跌停家数(**涨停线按板块**:主板±10% / 创业·科创±20% / 北交所±30% / 主板ST±5%)
  · 创 N 日新高/新低家数(默认 20/60)
  · 破位广度(收盘跌破 MA20 的家数占比)
  · 全市场中位 / 均值涨幅、站上 MA20 占比

输出**可回测的历史广度日序列**(DataFrame,index=date)。防未来函数:每个交易日的
广度只用该日及之前的横截面(rolling 新高/MA 均为因果窗),不引入未来行情。

涨停线启发式(无 ST 名单时):按代码前缀路由板块主限价;主板另用 5% 带兜住 ST,
即"封板 + pct 落在某板块允许限价的容差带内"→ 记涨停/跌停。见 §说明。

⚠️ **横截面口径单一真源**(2026-09-03 抽出):`classify_pct` / `equal_weight_mean_pct` /
`cross_section_stats` / `is_limit_hit` 是"全A等权(mean_pct=proxy)/中位数/涨跌家数/净广度/分位/
涨跌停"的**唯一定义处**。历史回测(本模块 `compute_breadth` → `features.build_proxy_index`)与
当日收盘节点(`tools.pipeline.market_breadth`)都必须调这几个函数,不许各写一份——否则
"全A等权"会出现两个互相漂移的定义,大盘预测的基准与盘尾 α 记分的基准对不上账。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from tools.config.strategy import THRESHOLDS

logger = logging.getLogger("market_forecast.breadth")

_CFG = THRESHOLDS["大盘预测"]


# ————————————————————————— 板块 / 涨停线 —————————————————————————
def board_of(code: str) -> str:
    """按代码前缀判板块:主板 / 创业板 / 科创板 / 北交所。"""
    code = str(code)
    if code[:2] == "30":
        return "创业板"
    if code[:2] == "68":
        return "科创板"
    if code[:2] in ("92", "83", "87", "43") or code[:1] in ("8", "4"):
        return "北交所"
    return "主板"  # 60 / 00


def allowed_limits(code: str, cfg: dict | None = None) -> list[float]:
    """该票**允许的**涨停线百分比集合(主板含 ST 的 5%,故 {10,5})。"""
    cfg = cfg or _CFG
    L = cfg["涨停线"]
    b = board_of(code)
    if b == "主板":
        return [L["主板"], L["ST"]]           # 10% 或(ST)5%
    if b == "创业板":
        return [L["创业板"]]
    if b == "科创板":
        return [L["科创板"]]
    return [L["北交所"]]


def _hit_limit(pct: np.ndarray, close: np.ndarray, high: np.ndarray,
               low: np.ndarray, limits: list[float], tol: float,
               seal_tol: float, up: bool) -> np.ndarray:
    """逐行判涨停(up=True)/跌停(up=False):封板 + pct 落在允许限价容差带内。

    封板:涨停要求 close≈high(收在最高),跌停要求 close≈low。pct 需 |pct∓L|≤tol。
    """
    if up:
        sealed = close >= high * (1.0 - seal_tol)
        near = np.zeros_like(pct, dtype=bool)
        for L in limits:
            near |= np.abs(pct - L) <= tol
    else:
        sealed = close <= low * (1.0 + seal_tol)
        near = np.zeros_like(pct, dtype=bool)
        for L in limits:
            near |= np.abs(pct + L) <= tol
    return sealed & near


def is_limit_hit(code: str, pct: float, close: float, high: float, low: float,
                 *, up: bool = True, cfg: dict | None = None) -> bool:
    """标量版涨/跌停判定(单票单日):`_hit_limit` 的对外包装,供实时报价逐票调用。

    与历史广度聚合走**同一**启发式(板块限价路由 + 封板 + 容差带),故两侧涨跌停家数同口径。
    任一入参缺失(None/NaN)→ False(缺失就是缺失,不猜)。
    """
    cfg = cfg or _CFG
    vals = [pct, close, high, low]
    if any(v is None for v in vals):
        return False
    arr = [np.array([float(v)], dtype=float) for v in vals]
    if any(np.isnan(a[0]) for a in arr):
        return False
    return bool(_hit_limit(arr[0], arr[1], arr[2], arr[3],
                           allowed_limits(code, cfg), float(cfg["涨停容差"]),
                           float(cfg["封板容差"]), up=up)[0])


# ————————————————————————— 横截面口径(单一真源) —————————————————————————
#: 全市场涨幅分位的默认切点(P10/P25/P75/P90)——盘尾 α 记分看分布厚尾用。
PCT_QUANTILES: tuple[float, ...] = (0.10, 0.25, 0.75, 0.90)

#: 供产出 meta 标注"口径出处",下游/复盘据此确认两侧同源。
CROSS_SECTION_SOURCE = "tools.analysis.market_forecast.breadth.cross_section_stats"


def classify_pct(pct) -> dict[str, np.ndarray]:
    """涨幅数组 → 涨/跌/平的布尔掩码(**涨跌平的唯一定义**:pct>0 / <0 / ==0)。

    NaN(停牌/上市首日无涨幅)在三类里**都是 False**——既不算涨也不算跌也不算平。
    """
    arr = np.asarray(pct, dtype=float)
    return {"adv": arr > 0, "dec": arr < 0, "flat": arr == 0}


def equal_weight_mean_pct(pct, total: int | float | None = None) -> float:
    """**全A等权(mean_pct / proxy)的唯一定义**:横截面涨幅的算术平均。

    · 分子 = Σpct(跳过 NaN);分母 = `total`,缺省 = 序列长度(=当日在市家数)。
      NaN 项**计入分母不计入分子**——与历史聚合 `pct_sum / listed` 逐日口径一致。
    · 单位:百分点(与源方 pct_chg 同单位),不做 /100。
    · `features.build_proxy_index` 就是把本函数的逐日结果累乘成代理指数。
    """
    arr = np.asarray(pct, dtype=float)
    n = float(arr.size if total is None else total)
    if n <= 0:
        return float("nan")
    return float(np.nansum(arr) / n)


def net_breadth(adv_n: int | float, dec_n: int | float,
                total_n: int | float) -> float:
    """净广度(净涨占比)的唯一定义:(涨家数 − 跌家数) / 总家数;总家数 0 → NaN。"""
    total_n = float(total_n)
    if total_n <= 0:
        return float("nan")
    return float((float(adv_n) - float(dec_n)) / total_n)


def cross_section_stats(pct, *, total: int | None = None,
                        quantiles: tuple[float, ...] = PCT_QUANTILES) -> dict:
    """一日全市场涨幅横截面 → 聚合口径 dict(**历史回测与当日节点共用**)。

    入参 `pct` 为该日全市场个股涨幅(%)序列(允许含 NaN);`total` 缺省 = 序列长度。
    返回:total / mean_pct(全A等权) / median_pct / adv,dec,flat / net_adv(净广度) /
          quantiles{P10,P25,...}。空序列 → 计数 0、比率 NaN(不假造 0)。
    """
    arr = np.asarray(pct, dtype=float)
    n = int(arr.size if total is None else total)
    m = classify_pct(arr)
    adv, dec, flat = int(m["adv"].sum()), int(m["dec"].sum()), int(m["flat"].sum())
    valid = arr[~np.isnan(arr)]
    med = float(np.median(valid)) if valid.size else float("nan")
    qs = {f"P{int(round(q * 100))}": (float(np.quantile(valid, q)) if valid.size
                                      else float("nan")) for q in quantiles}
    return {
        "total": n,
        "mean_pct": equal_weight_mean_pct(arr, total=n),
        "median_pct": med,
        "adv": adv, "dec": dec, "flat": flat,
        "net_adv": net_breadth(adv, dec, n),
        "quantiles": qs,
    }


# ————————————————————————— 单票 → 逐日指标 —————————————————————————
def _per_stock_indicators(df: pd.DataFrame, code: str,
                          cfg: dict | None = None) -> pd.DataFrame | None:
    """单票 K线 → 逐日广度贡献(date + 各计数列,均为 0/1 或数值)。样本过短→None。

    因果性:新高/新低用 rolling(window).max/min(含当日,只回看);MA20 同理;pct_chg 为当日。
    """
    cfg = cfg or _CFG
    if df is None or len(df) < 2 or "close" not in df.columns:
        return None
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    close = d["close"].astype(float)
    high = d["high"].astype(float)
    low = d["low"].astype(float)
    # pct_chg:优先用现成列,缺则用 close 环比(首行 NaN)
    if "pct_chg" in d.columns and d["pct_chg"].notna().any():
        pct = d["pct_chg"].astype(float).to_numpy()
    else:
        pct = (close.pct_change() * 100.0).to_numpy()

    c, h, lo = close.to_numpy(), high.to_numpy(), low.to_numpy()
    tol = float(cfg["涨停容差"])
    seal = float(cfg["封板容差"])
    lims = allowed_limits(code, cfg)

    masks = classify_pct(pct)                     # 涨跌平走单一真源(与当日节点同口径)
    up, dn, flat = masks["adv"], masks["dec"], masks["flat"]
    lu = _hit_limit(pct, c, h, lo, lims, tol, seal, up=True)
    ld = _hit_limit(pct, c, h, lo, lims, tol, seal, up=False)

    out = {"date": d["date"].values,
           "listed": np.ones(len(d), dtype=int),
           "adv": up.astype(int), "dec": dn.astype(int), "flat": flat.astype(int),
           "limit_up": lu.astype(int), "limit_down": ld.astype(int),
           "pct": pct}

    for w in cfg["新高新低窗口"]:
        roll_hi = high.rolling(w, min_periods=w).max()
        roll_lo = low.rolling(w, min_periods=w).min()
        out[f"nh{w}"] = (close >= roll_hi).astype(int).to_numpy()
        out[f"nl{w}"] = (close <= roll_lo).astype(int).to_numpy()

    maw = int(cfg["破位MA"])
    ma = close.rolling(maw, min_periods=maw).mean()
    out[f"above_ma{maw}"] = (close > ma).astype(int).to_numpy()
    out[f"below_ma{maw}"] = (close < ma).astype(int).to_numpy()
    out[f"ma{maw}_valid"] = ma.notna().astype(int).to_numpy()

    return pd.DataFrame(out)


# ————————————————————————— 全市场聚合 —————————————————————————
def compute_breadth(codes: list[str] | None = None, data_root=None,
                    chunk: int = 800, cfg: dict | None = None) -> pd.DataFrame:
    """扫 master/kline 全A → 逐交易日广度日序列(index=date 升序)。

    分块累加(groupby.sum 逐块相加),内存有界。列见模块 docstring。
    衍生比率列:adv_dec_ratio / net_adv / limit_net / nh_net_20 / breadth_ma20 等。
    """
    cfg = cfg or _CFG
    from tools.analysis.market_forecast.dataroot import ensure_data_root
    from tools.store import repo as store

    ensure_data_root(str(data_root) if data_root else None)
    codes = codes or store.list_master_codes()
    if not codes:
        raise RuntimeError("master/kline 无代码;检查数据根")

    maw = int(cfg["破位MA"])
    count_cols = ["listed", "adv", "dec", "flat", "limit_up", "limit_down",
                  f"above_ma{maw}", f"below_ma{maw}", f"ma{maw}_valid"]
    for w in cfg["新高新低窗口"]:
        count_cols += [f"nh{w}", f"nl{w}"]

    acc_counts: pd.DataFrame | None = None      # 计数列逐块相加
    pct_lists: dict = {}                        # date -> list(pct)(算均值/中位数/分位)

    buf = []
    n_used = 0
    for i, code in enumerate(codes):
        try:
            df = store.get_master_kline(code)
        except Exception:
            continue
        ind = _per_stock_indicators(df, code, cfg)
        if ind is None:
            continue
        buf.append(ind)
        n_used += 1
        if len(buf) >= chunk or i == len(codes) - 1:
            block = pd.concat(buf, ignore_index=True)
            buf = []
            g = block.groupby("date")
            cnt = g[count_cols].sum()
            acc_counts = cnt if acc_counts is None else acc_counts.add(cnt, fill_value=0)
            for dt, sub in block.groupby("date")["pct"]:
                pct_lists.setdefault(dt, []).append(sub.to_numpy())

    if acc_counts is None:
        raise RuntimeError("无可用个股 K线")

    res = acc_counts.sort_index()
    res.index.name = "date"
    total = res["listed"].replace(0, np.nan)
    res["total"] = res["listed"]
    res["net_adv"] = (res["adv"] - res["dec"]) / total
    res["adv_dec_ratio"] = res["adv"] / res["dec"].replace(0, np.nan)
    res["limit_net"] = (res["limit_up"] - res["limit_down"]) / total
    res["limit_up_ratio"] = res["limit_up"] / total
    for w in cfg["新高新低窗口"]:
        res[f"nh_net_{w}"] = (res[f"nh{w}"] - res[f"nl{w}"]) / total
    ma_valid = res[f"ma{maw}_valid"].replace(0, np.nan)
    res["above_ma20_ratio"] = res[f"above_ma{maw}"] / ma_valid
    res["below_ma20_ratio"] = res[f"below_ma{maw}"] / ma_valid   # 破位广度
    # 均值 / 中位涨幅:走横截面单一真源 cross_section_stats(与当日收盘节点同一函数)
    day_stats = {dt: cross_section_stats(np.concatenate(v), total=int(res.loc[dt, "listed"]))
                 for dt, v in pct_lists.items()}
    res["mean_pct"] = pd.Series({d: s["mean_pct"] for d, s in day_stats.items()}).sort_index()
    res["median_pct"] = pd.Series({d: s["median_pct"] for d, s in day_stats.items()}).sort_index()

    res.attrs["n_stocks_used"] = n_used
    logger.info("广度聚合完成:%d 票 · %d 交易日", n_used, len(res))
    return res


def load_or_compute(cache_path, codes=None, data_root=None,
                    force: bool = False, cfg: dict | None = None) -> pd.DataFrame:
    """带缓存的广度序列:cache_path 存在且 !force → 直接读 parquet,否则重算并落盘。"""
    import os
    if cache_path and os.path.exists(cache_path) and not force:
        df = pd.read_parquet(cache_path)
        return df.set_index("date") if "date" in df.columns else df
    res = compute_breadth(codes=codes, data_root=data_root, cfg=cfg)
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        res.reset_index().to_parquet(cache_path)
    return res
