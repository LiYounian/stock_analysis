"""研究 PEAD · 业绩预告超预期漂移(根源=公司公告)。

命题:业绩预告(yjyg)是**公司公告(根源消息)**、有披露日历、文献有 PEAD 漂移 edge。
事件锚 = 披露日(公告日),进场 t+1(次日),测前瞻 5/10/20 交易日收益 + 相对沪深300 Alpha。
超预期分档:正(预增/略增/扭亏/续盈/减亏)vs 负(预减/略减/首亏/续亏/增亏)。

数据源:AKShare `stock_yjyg_em(date=报告期)` —— 每条含 预测指标/预告类型/业绩变动幅度/公告日期。
  · 只取 `预测指标` 含"归属于上市公司股东的净利润"的行(预告类型随预测指标不同而不同,
    须锁定归母净利润这条主口径,否则同一票会混入营收/扣非等相互矛盾的方向)。
  · 同一(报告期,code)保留**最早披露日**那条(首次预告 = 最干净的事件锚/最大信息增量)。
  · 拉到的原始表落到 scratch(不写项目 store,尊重"不触发全A回填/不改现有数据"的约束)。

防未来函数(红线):
  · 事件方向(预告类型/变动幅度)只用披露日**当日及之前**的信息(预告本身在公告日已公开)。
  · t0 = 首个 >= 公告日 的交易日;进场 = t0+1(次日);前瞻收益 = close[entry+N]/close[entry]-1,
    只用进场日**之后**的价 → 严格不回看。前瞻收益仅作标签,绝不反哺信号。
  · 另单独测"披露日当日收益"= close[t0]/close[t0-1]-1(首日见光反应),用于判断是否已 price-in。
  · 窗口越界(t0+1+N 超出本地 K 线)该窗记 None 并从样本剔除,不编造、不外推。

统计:每组每窗给 样本数/均值收益/胜率/均值Alpha/单样本 t(收益、Alpha);
     正−负组差做 Welch 双样本 t 检验(收益与 Alpha)。

用法:python -m tools.backtest.backtest_pead [--periods 20250630,20251231,...] [--json out.json]
非投资建议。历史回测≠未来保证。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.collectors import index as idx_col
from tools.collectors import market

logger = logging.getLogger("backtest.pead")

_DISCLAIMER = "历史回测≠未来保证,非投资建议。事件方向仅用披露日及之前信息,前瞻收益仅作标签。"

# 预告类型分档(锁定归母净利润口径)
POS_TYPES = {"预增", "略增", "扭亏", "续盈", "减亏"}
NEG_TYPES = {"预减", "略减", "预亏", "首亏", "续亏", "增亏"}
# "不确定" 及未知 → 弃

_NET_KEY = "归属于上市公司股东的净利润"   # 主口径预测指标
# 报告期缺省清单:覆盖本地 K 线(2025-03-31~)能测的报告期
DEFAULT_PERIODS = ("20250630", "20250930", "20251231", "20260331", "20260630")

_SCRATCH = Path("/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
                "c3f60e01-bbca-41c6-b337-cf7966926ca4/scratchpad")


# ————————————————————————— 数据:拉业绩预告 —————————————————————————
def fetch_forecasts(periods=DEFAULT_PERIODS, use_cache=True) -> pd.DataFrame:
    """拉各报告期业绩预告(归母净利润口径),规整为事件表并 scratch 缓存。

    Returns df[code, 报告期, 公告日期(Timestamp), 预告类型, 变动幅度, 方向('pos'/'neg'/None)]。
    每(报告期,code)保留最早披露日一条。akshare 某期失败 → 跳过并 log,不抛。
    """
    cache = _SCRATCH / "pead_forecasts.parquet"
    if use_cache and cache.exists():
        df = pd.read_parquet(cache)
        want = set(periods)
        if want.issubset(set(df["报告期"].unique())):
            logger.info("命中 scratch 缓存 %s", cache)
            return df[df["报告期"].isin(want)].reset_index(drop=True)

    import akshare as ak
    frames = []
    for p in periods:
        try:
            raw = ak.stock_yjyg_em(date=p)
        except Exception as e:                       # noqa: BLE001
            logger.warning("stock_yjyg_em(%s) 失败,跳过: %s", p, e)
            continue
        if raw is None or len(raw) == 0:
            logger.warning("stock_yjyg_em(%s) 空", p)
            continue
        net = raw[raw["预测指标"].astype(str).str.contains(_NET_KEY, na=False)].copy()
        if net.empty:
            continue
        net["code"] = net["股票代码"].astype(str).str.zfill(6)
        net["报告期"] = p
        net["公告日期"] = pd.to_datetime(net["公告日期"], errors="coerce")
        net["预告类型"] = net["预告类型"].astype(str)
        net["变动幅度"] = pd.to_numeric(net["业绩变动幅度"], errors="coerce")
        net = net.dropna(subset=["公告日期"])
        # 同 (报告期,code) 取最早披露
        net = net.sort_values("公告日期").drop_duplicates(subset=["报告期", "code"], keep="first")
        frames.append(net[["code", "报告期", "公告日期", "预告类型", "变动幅度"]])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["方向"] = out["预告类型"].map(
        lambda t: "pos" if t in POS_TYPES else ("neg" if t in NEG_TYPES else None))
    try:
        _SCRATCH.mkdir(parents=True, exist_ok=True)
        out.to_parquet(cache)
    except Exception as e:                           # noqa: BLE001
        logger.warning("scratch 缓存落盘失败: %s", e)
    return out


# ————————————————————————— 单事件前瞻收益 —————————————————————————
def _first_ge(dates: list, t0: pd.Timestamp) -> int | None:
    for i, d in enumerate(dates):
        if d >= t0:
            return i
    return None


def compute_event(event_date, kline: pd.DataFrame, bench: pd.DataFrame,
                  windows=(5, 10, 20), max_gap_days: int = 10) -> dict:
    """算单事件:披露日当日收益 + 进场(t+1)前瞻 N 日收益 + 相对沪深300 Alpha。

    无未来函数:t0=首个>=公告日交易日;进场=t0+1;前瞻只用进场之后价。
    max_gap_days:t0 与披露日相隔超过该自然日数 → 判定停牌/数据缺口,事件作废
    (否则会把"停牌数月后复牌首日"当进场,漂移窗完全失真)。
    """
    rec = {"披露日当日收益": None, "进场日": None, "t0_gap": None,
           "前瞻": {n: None for n in windows}, "alpha": {n: None for n in windows}}
    if kline is None or len(kline) == 0 or "close" not in kline.columns:
        return rec
    kd = pd.to_datetime(kline["date"]).tolist()
    P = kline["close"].astype(float).tolist()
    ed = pd.to_datetime(event_date)
    t0 = _first_ge(kd, ed)
    if t0 is None:
        return rec
    gap = (kd[t0] - ed).days
    rec["t0_gap"] = gap
    if gap > max_gap_days:                            # 停牌/数据缺口 → 作废
        return rec
    # 披露日当日反应(见光)
    if t0 >= 1 and P[t0 - 1]:
        rec["披露日当日收益"] = round(P[t0] / P[t0 - 1] - 1.0, 6)
    entry = t0 + 1                                   # t+1 进场
    if entry >= len(P) or not P[entry]:
        return rec
    rec["进场日"] = str(kd[entry].date())

    # 基准对齐:同样 t+1 进场
    bd = bP = bentry = None
    if bench is not None and len(bench) and "close" in bench.columns:
        bd = pd.to_datetime(bench["date"]).tolist()
        bP = bench["close"].astype(float).tolist()
        bt0 = _first_ge(bd, pd.to_datetime(event_date))
        if bt0 is not None and bt0 + 1 < len(bP):
            bentry = bt0 + 1

    for n in windows:
        j = entry + n
        if j < len(P):
            fr = round(P[j] / P[entry] - 1.0, 6)
            rec["前瞻"][n] = fr
            if bentry is not None and bentry + n < len(bP) and bP[bentry]:
                bfr = bP[bentry + n] / bP[bentry] - 1.0
                rec["alpha"][n] = round(fr - bfr, 6)
    return rec


# ————————————————————————— 汇总 + 检验 —————————————————————————
def _tstat(x: np.ndarray):
    """单样本 t(H0: 均值=0)+ scipy p 值。"""
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return {"n": int(n), "均值": None, "t": None, "p": None}
    from scipy import stats
    t, p = stats.ttest_1samp(x, 0.0)
    return {"n": int(n), "均值": round(float(x.mean()), 6),
            "t": round(float(t), 3), "p": round(float(p), 4)}


def summarize_group(events: list[dict], windows=(5, 10, 20)) -> dict:
    """一组事件 → 每窗 样本数/均值/胜率/均Alpha/单样本t;外加披露日当日反应统计。"""
    out = {}
    same = np.array([e["披露日当日收益"] for e in events
                     if e["披露日当日收益"] is not None], dtype=float)
    out["披露日当日"] = {"n": int(len(same)),
                         "均值": round(float(same.mean()), 6) if len(same) else None,
                         "上涨占比": round(float((same > 0).mean()), 4) if len(same) else None}
    for n in windows:
        r = np.array([e["前瞻"][n] for e in events if e["前瞻"][n] is not None], dtype=float)
        a = np.array([e["alpha"][n] for e in events if e["alpha"][n] is not None], dtype=float)
        out[n] = {
            "样本数": int(len(r)),
            "均值收益": round(float(r.mean()), 6) if len(r) else None,
            "胜率": round(float((r > 0).mean()), 4) if len(r) else None,
            "收益t检验": _tstat(r),
            "均值Alpha": round(float(a.mean()), 6) if len(a) else None,
            "Alpha胜率": round(float((a > 0).mean()), 4) if len(a) else None,
            "Alpha_t检验": _tstat(a),
        }
    return out


def _welch(a: np.ndarray, b: np.ndarray) -> dict:
    """Welch 双样本 t(H0: 均值相等),返回 差值/t/p。"""
    a = a[np.isfinite(a)]; b = b[np.isfinite(b)]
    if len(a) < 3 or len(b) < 3:
        return {"n_pos": int(len(a)), "n_neg": int(len(b)), "差": None, "t": None, "p": None}
    from scipy import stats
    t, p = stats.ttest_ind(a, b, equal_var=False)
    return {"n_pos": int(len(a)), "n_neg": int(len(b)),
            "差": round(float(a.mean() - b.mean()), 6),
            "t": round(float(t), 3), "p": round(float(p), 4)}


def spread_test(pos_events, neg_events, windows=(5, 10, 20)) -> dict:
    """正−负组前瞻收益 & Alpha 的 Welch 检验(PEAD 漂移是否显著)。"""
    out = {}
    for n in windows:
        rp = np.array([e["前瞻"][n] for e in pos_events if e["前瞻"][n] is not None], dtype=float)
        rn = np.array([e["前瞻"][n] for e in neg_events if e["前瞻"][n] is not None], dtype=float)
        ap = np.array([e["alpha"][n] for e in pos_events if e["alpha"][n] is not None], dtype=float)
        an = np.array([e["alpha"][n] for e in neg_events if e["alpha"][n] is not None], dtype=float)
        out[n] = {"收益_正减负": _welch(rp, rn), "Alpha_正减负": _welch(ap, an)}
    return out


# ————————————————————————— 主流程 —————————————————————————
def run(periods=DEFAULT_PERIODS, windows=(5, 10, 20), json_path=None, spot_check=True):
    print(f"\n===== 研究 PEAD · 业绩预告漂移(根源=公司公告)=====")
    print(f"(事件锚=披露日, 进场 t+1, 前瞻 {windows} 交易日, 基准=沪深300; {_DISCLAIMER})\n")

    fc = fetch_forecasts(periods)
    if fc.empty:
        print("!! 未拉到任何业绩预告")
        return {"错误": "无预告数据", "免责": _DISCLAIMER}

    print("—— 预告事件覆盖(归母净利润口径, 每报告期×code 取最早披露)——")
    for p in periods:
        sub = fc[fc["报告期"] == p]
        if len(sub):
            vc = sub["方向"].value_counts(dropna=False).to_dict()
            print(f"  {p}: 事件={len(sub)}  披露区间={sub['公告日期'].min().date()}~{sub['公告日期'].max().date()}  方向={vc}")
    print()

    bench = idx_col.load_index(idx_col.BENCHMARK)    # 沪深300
    kcache: dict[str, pd.DataFrame | None] = {}

    def _kline(code):
        if code not in kcache:
            try:
                kcache[code] = market.load_kline(code).reset_index(drop=True)
            except Exception:
                kcache[code] = None
        return kcache[code]

    pos_ev, neg_ev = [], []
    dropped = 0
    spot_rows = []
    for _, r in fc.iterrows():
        if r["方向"] not in ("pos", "neg"):
            continue
        k = _kline(r["code"])
        ev = compute_event(r["公告日期"], k, bench, windows)
        # 至少有一个前瞻窗口有值才计入
        if all(v is None for v in ev["前瞻"].values()):
            dropped += 1
            continue
        ev["_code"] = r["code"]; ev["_period"] = r["报告期"]
        ev["_type"] = r["预告类型"]; ev["_disc"] = str(r["公告日期"].date())
        (pos_ev if r["方向"] == "pos" else neg_ev).append(ev)
        if spot_check and len(spot_rows) < 3 and ev["前瞻"][windows[-1]] is not None:
            spot_rows.append((r, k, ev))

    print(f"—— 可用事件: 正={len(pos_ev)}  负={len(neg_ev)}  (前瞻全越界剔除 {dropped}) ——\n")

    pos = summarize_group(pos_ev, windows)
    neg = summarize_group(neg_ev, windows)
    spread = spread_test(pos_ev, neg_ev, windows)

    def _fmt_group(name, g):
        print(f"—— {name}组 —— 披露日当日: n={g['披露日当日']['n']} 均值={g['披露日当日']['均值']} 上涨={g['披露日当日']['上涨占比']}")
        for n in windows:
            b = g[n]
            print(f"   {n}日: n={b['样本数']} 均收益={b['均值收益']} 胜率={b['胜率']} "
                  f"| 均Alpha={b['均值Alpha']} Alpha胜率={b['Alpha胜率']} "
                  f"| 收益t={b['收益t检验']['t']}(p={b['收益t检验']['p']}) "
                  f"Alpha_t={b['Alpha_t检验']['t']}(p={b['Alpha_t检验']['p']})")
    _fmt_group("正超预期", pos)
    _fmt_group("负超预期", neg)
    print("\n—— 正−负组差(Welch 双样本 t)——")
    for n in windows:
        s = spread[n]
        print(f"   {n}日: 收益差={s['收益_正减负']['差']} t={s['收益_正减负']['t']} p={s['收益_正减负']['p']} "
              f"| Alpha差={s['Alpha_正减负']['差']} t={s['Alpha_正减负']['t']} p={s['Alpha_正减负']['p']}")

    # 红线自检:打印几条事件的日期链,肉眼验证前瞻价严格晚于披露日
    if spot_check and spot_rows:
        print("\n—— 无未来函数 spot-check(前瞻价日期须严格晚于披露日)——")
        for r, k, ev in spot_rows:
            kd = pd.to_datetime(k["date"]).tolist()
            t0 = _first_ge(kd, r["公告日期"])
            chain = {"披露日": str(r["公告日期"].date()),
                     "t0(首个>=披露日交易日)": str(kd[t0].date()),
                     "进场t+1": ev["进场日"]}
            for n in windows:
                j = t0 + 1 + n
                if j < len(kd):
                    chain[f"进场+{n}"] = str(kd[j].date())
            print(f"   {r['code']}/{r['报告期']}/{r['预告类型']}: {chain}")

    res = {"periods": list(periods), "windows": list(windows),
           "覆盖": {p: int((fc["报告期"] == p).sum()) for p in periods},
           "可用事件": {"正": len(pos_ev), "负": len(neg_ev), "前瞻越界剔除": dropped},
           "正超预期组": pos, "负超预期组": neg, "正减负Welch": spread,
           "免责": _DISCLAIMER}
    if json_path:
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--periods", default=",".join(DEFAULT_PERIODS))
    ap.add_argument("--windows", default="5,10,20")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    run(periods=tuple(x for x in a.periods.split(",") if x),
        windows=tuple(int(x) for x in a.windows.split(",")),
        json_path=a.json or None)
