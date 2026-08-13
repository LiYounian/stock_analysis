"""创 100 日新高 · 续涨事件研究回测(数据说话,非投资建议)。

缘起(策略提供者):"牛市常有创百日新高的股票,想回测创百日新高后续涨得怎么样"。
本脚本做**全 A 横截面事件研究**去板块/幸存者偏差,严格无未来函数,诚实回填。
详见 docs/计划/创百日新高_续涨回测_预注册.md。

口径(钉死):
  - 事件(收盘版):第 t 日 close[t] == max(close[t-99..t])(=最近 100 交易日最高收盘),
    且前一日不是新高 → **首次创 100 日新高**(避免连续新高重复计数)。
  - 事件(最高价版):当日 high[t] == max(high[t-99..t]) 且前一日不是,作对照。
  - 进场:t+1 收盘(机械基线,非最终买法);另报 当日收盘→t+1 开盘(隔夜)。
  - 前瞻:5/10/20 交易日收益 = close[t+1+N]/close[t+1]-1;
    Alpha = 个股持有期收益 − 沪深300(000300)同持有期收益(按日期对齐)。
  - 分层:①放量(量比≥1.5) vs 缩量;②20cm 板(300/301/688/689)vs 主板;
    ③均线多头(close>MA200 且 MA5>MA10>MA20)vs 否;④regime(沪深300>MA20 上行 vs 下行)。

无未来函数红线:新高判定只用 ≤t 的数据;前瞻收益 t+1 及之后作标签。

⚠️ 统计口径诚实声明:新高事件在行情里**高度聚集**(牛市一天几百只齐创新高),
观测之间横截面强相关、时间上重叠 → 朴素 iid 的 t 值会被严重高估。故对每个指标
同时报**朴素 t**(所有观测独立假设)与**按进场日聚类 t**(每个交易日先取截面均值,
再跨日算 t;有效样本=交易日数),后者才是可信的显著性下界。

用法:
  python -m tools.backtest.backtest_newhigh [--sample 1500] [--seed 42] [--json 路径]
  python -m tools.backtest.backtest_newhigh --codes 300308,688981    # 指定票
  python -m tools.backtest.backtest_newhigh --full                    # 跑全 A 主档
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from tools.collectors import index as index_col
from tools.collectors import market
from tools.store import repo as store

_DISCLAIMER = "历史回测≠未来保证,非投资建议;新高事件高度聚集,朴素 t 值偏乐观,以按日聚类 t 为准。"
_LOOKBACK = 100        # 新高窗口:最近 100 交易日
_HORIZONS = (5, 10, 20)
_VOL_HOT = 1.5         # 放量阈值(量比)


# ————————————————————————— 分类工具 —————————————————————————
def _is_20cm(code: str) -> bool:
    """20cm 涨跌幅板:创业板(300/301)、科创板(688/689)。其余按主板处理。"""
    return code.startswith(("300", "301", "688", "689"))


def _load_index_close() -> dict:
    """沪深300 日期→收盘 映射(Alpha 基准)。缺失返回空 dict(Alpha 全 NaN)。"""
    try:
        hs = index_col.load_index("沪深300")
    except Exception:
        return {}
    hs = hs.copy()
    hs["date"] = pd.to_datetime(hs["date"])
    return dict(zip(hs["date"], hs["close"].astype(float)))


def _index_regime(idx_close: dict, ma: int = 20) -> dict:
    """沪深300 regime:日期→是否处于上行(close>自身 MA{ma})。用于 regime 分层。"""
    if not idx_close:
        return {}
    s = pd.Series(idx_close).sort_index()
    up = s > s.rolling(ma).mean()
    return dict(zip(up.index, up.values))


# ————————————————————————— 逐票建事件面板 —————————————————————————
def build_panel(codes, idx_close: dict, regime: dict,
                horizons=_HORIZONS, lookback=_LOOKBACK) -> pd.DataFrame:
    """逐票建**全交易日面板**(每行=一个 code×t 观测),打上事件/分层标记 + 前瞻收益/Alpha。

    全日面板既作事件子集来源,又作"全样本基准"(所有交易日前瞻收益)对照。
    """
    maxN = max(horizons)
    frames = []
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:
            continue
        if df is None or len(df) < lookback + maxN + 5:
            continue
        df = df.reset_index(drop=True).copy()
        df["date"] = pd.to_datetime(df["date"])
        close = df["close"].astype(float)
        high = df["high"].astype(float)
        vol = df["volume"].astype(float)
        n = len(df)
        used += 1

        # —— 事件判定(只用 ≤t;rolling 窗口含当日,故 close>=rolling_max ⟺ 当日为窗口最高)——
        roll_c = close.rolling(lookback).max()
        nh_c = close >= roll_c                         # 当日创 100 日新高收盘
        first_c = nh_c & ~nh_c.shift(1, fill_value=False)
        roll_h = high.rolling(lookback).max()
        nh_h = high >= roll_h
        first_h = nh_h & ~nh_h.shift(1, fill_value=False)

        # —— 分层特征(全用 ≤t)——
        vol_ma5 = vol.shift(1).rolling(5).mean()       # 前 5 日均量(不含当日)
        vol_ratio = vol / vol_ma5
        ma5, ma10, ma20 = (close.rolling(w).mean() for w in (5, 10, 20))
        ma200 = close.rolling(200).mean()
        ma_bull = (close > ma200) & (ma5 > ma10) & (ma10 > ma20)

        # —— 前瞻收益(t+1 进场)+ 隔夜 ——
        entry = close.shift(-1)                        # close[t+1]
        open_next = df["open"].astype(float).shift(-1) # open[t+1]
        overnight = open_next / close - 1.0            # 当日收盘→次日开盘
        entry_date = df["date"].shift(-1)
        rec = {
            "code": code, "t": np.arange(n), "date_t": df["date"], "entry_date": entry_date,
            "first_nh_close": first_c.values, "first_nh_high": first_h.values,
            "vol_ratio": vol_ratio.values, "is_20cm": _is_20cm(code),
            "ma_bull": ma_bull.values, "overnight": overnight.values,
        }
        for N in horizons:
            exit_c = close.shift(-(1 + N))             # close[t+1+N]
            rec[f"r{N}"] = (exit_c / entry - 1.0).values
            exit_date = df["date"].shift(-(1 + N))
            # index 收益按日期对齐(个股与指数共用交易日历)
            ie = entry_date.map(idx_close)
            ix = exit_date.map(idx_close)
            rec[f"idx_r{N}"] = (ix / ie - 1.0).values
        sub = pd.DataFrame(rec)
        # regime 按进场日的沪深300 状态
        sub["regime_up"] = sub["entry_date"].map(regime)
        frames.append(sub)

    if not frames:
        panel = pd.DataFrame()
    else:
        panel = pd.concat(frames, ignore_index=True)
    # 只保留前瞻收益完整的行(t+1+maxN 存在)
    if not panel.empty:
        panel = panel.dropna(subset=[f"r{N}" for N in horizons])
        for N in horizons:
            panel[f"alpha{N}"] = panel[f"r{N}"] - panel[f"idx_r{N}"]
    panel.attrs["used"] = used
    return panel


# ————————————————————————— 统计(含按日聚类 t)—————————————————————————
def _tstat(x: np.ndarray) -> float:
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return float("nan")
    sd = x.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(x.mean() / (sd / math.sqrt(len(x))))


def _clustered_t(vals: pd.Series, dates: pd.Series) -> tuple[float, int]:
    """按进场日聚类 t:每个交易日先取截面均值,再跨日算 t。返回 (t, 交易日数)。"""
    d = pd.DataFrame({"v": vals.values, "d": dates.values}).dropna()
    if d.empty:
        return float("nan"), 0
    daily = d.groupby("d")["v"].mean()
    return _tstat(daily.to_numpy()), int(len(daily))


def _metric(sub: pd.DataFrame, col: str) -> dict:
    """一列收益指标:均值%、胜率%、朴素 t、按日聚类 t、n、交易日数。"""
    v = sub[col].dropna()
    if len(v) == 0:
        return {"n": 0}
    ct, ndays = _clustered_t(sub[col], sub["entry_date"])
    return {
        "n": int(len(v)),
        "均值%": round(float(v.mean()) * 100, 3),
        "中位%": round(float(v.median()) * 100, 3),
        "胜率%": round(float((v > 0).mean()) * 100, 1),
        "朴素t": round(_tstat(v.to_numpy()), 2),
        "聚类t": round(ct, 2),
        "交易日数": ndays,
    }


def _group_stats(sub: pd.DataFrame, horizons) -> dict:
    """一个样本子集在各 horizon 的 收益 + Alpha 指标。"""
    out = {"样本数": int(len(sub))}
    for N in horizons:
        out[f"{N}日"] = {"收益": _metric(sub, f"r{N}"), "Alpha": _metric(sub, f"alpha{N}")}
    return out


def analyze(panel: pd.DataFrame, horizons=_HORIZONS) -> dict:
    """全套分层分析:基准 / 无条件新高 / 各分层。"""
    res = {
        "样本股数": int(panel.attrs.get("used", 0)),
        "全交易日观测": int(len(panel)),
        "新高窗口": _LOOKBACK,
        "免责": _DISCLAIMER,
    }
    nh = panel[panel["first_nh_close"]]
    nh_high = panel[panel["first_nh_high"]]

    res["全样本基准(所有交易日)"] = _group_stats(panel, horizons)
    res["无条件·首次创新高(收盘)"] = _group_stats(nh, horizons)
    res["无条件·首次创新高(最高价对照)"] = _group_stats(nh_high, horizons)
    # 隔夜(收盘版事件)
    res["新高隔夜收益(收→次开)"] = {
        "全样本": _metric(panel, "overnight"),
        "新高组": _metric(nh, "overnight"),
    }

    # —— 分层(均在"收盘首次新高"事件内切)——
    strat = {}
    strat_defs = {
        "①放量(量比≥1.5)": nh[nh["vol_ratio"] >= _VOL_HOT],
        "①缩量(量比<1.5)": nh[nh["vol_ratio"] < _VOL_HOT],
        "②20cm板(创/科)": nh[nh["is_20cm"]],
        "②主板": nh[~nh["is_20cm"]],
        "③均线多头": nh[nh["ma_bull"] == True],   # noqa: E712 (含 NaN→False 语义,显式)
        "③非均线多头": nh[nh["ma_bull"] == False],
        "④regime上行(沪深300>MA20)": nh[nh["regime_up"] == True],  # noqa: E712
        "④regime下行": nh[nh["regime_up"] == False],  # noqa: E712
        "①+③放量且均线多头": nh[(nh["vol_ratio"] >= _VOL_HOT) & (nh["ma_bull"] == True)],  # noqa: E712
    }
    for name, s in strat_defs.items():
        strat[name] = _group_stats(s, horizons)
    res["分层"] = strat
    return res


# ————————————————————————— 报告 —————————————————————————
def _fmt_metric(m: dict) -> str:
    if m.get("n", 0) == 0:
        return "n=0(样本空)"
    return (f"n={m['n']:<6} 日数={m['交易日数']:<4} 均值={m['均值%']:+.2f}% "
            f"胜率={m['胜率%']:.0f}% 朴素t={m['朴素t']:+.2f} 聚类t={m['聚类t']:+.2f}")


def _print_group(title: str, g: dict, horizons):
    print(f"—— {title}  (样本数={g['样本数']}) ——")
    for N in horizons:
        blk = g.get(f"{N}日", {})
        print(f"  [{N:>2}日 收益 ] {_fmt_metric(blk.get('收益', {}))}")
        print(f"  [{N:>2}日 Alpha] {_fmt_metric(blk.get('Alpha', {}))}")
    print()


def report(res: dict, horizons=_HORIZONS):
    print(f"\n===== 创 {res['新高窗口']} 日新高 · 续涨事件研究 =====")
    print(f"样本股数={res['样本股数']}  全交易日观测={res['全交易日观测']}")
    print(f"(严格无未来函数;{res['免责']})\n")
    _print_group("全样本基准(所有交易日)", res["全样本基准(所有交易日)"], horizons)
    _print_group("无条件·首次创新高(收盘)", res["无条件·首次创新高(收盘)"], horizons)
    _print_group("无条件·首次创新高(最高价对照)", res["无条件·首次创新高(最高价对照)"], horizons)
    ov = res["新高隔夜收益(收→次开)"]
    print(f"—— 隔夜(当日收→次日开)——\n  全样本: {_fmt_metric(ov['全样本'])}\n  新高组: {_fmt_metric(ov['新高组'])}\n")
    for name, g in res["分层"].items():
        _print_group(name, g, horizons)


# ————————————————————————— 抽样 / CLI —————————————————————————
def _sample_universe(n: int, seed: int) -> list[str]:
    """从本地主档随机抽 n 只(播种可复现),破板块/自选偏差。"""
    import random
    allc = sorted(store.list_master_codes())
    rng = random.Random(seed)
    return rng.sample(allc, min(n, len(allc)))


def run(codes=None, horizons=_HORIZONS, json_path=None):
    idx_close = _load_index_close()
    regime = _index_regime(idx_close)
    if not idx_close:
        print("!! 沪深300 主档缺失,Alpha 将全为 NaN。")
    panel = build_panel(codes, idx_close, regime, horizons)
    if panel.empty:
        print("!! panel 为空(无足够历史)")
        return
    res = analyze(panel, horizons)
    report(res, horizons)
    if json_path:
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=1500, help="从主档随机抽 N 只(默认 1500)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--full", action="store_true", help="跑全 A 主档(忽略 --sample)")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    codes = [c for c in a.codes.split(",") if c] or None
    if codes is None:
        codes = store.list_master_codes() if a.full else _sample_universe(a.sample, a.seed)
    run(codes=codes, json_path=a.json or None)
