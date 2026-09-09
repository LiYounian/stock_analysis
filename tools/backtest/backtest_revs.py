"""REVS 四因子(阶段1 E盈利 + S情绪)前瞻回测——横截面多维合成 + 真实成本净额 + 相对单因子增量。

谱系:复用 `backtest_rank` 的 IC/ICIR/分层机制 + `backtest_reversal_turnover` 的 TopK 净额
(非重叠调仓 + 组合换手率 + 真实往返成本),与既有策略**苹果对苹果**可比。

为什么单票 scorer 不够:REVS 综合分 = 每子因子横截面 winsor+zscore→按方向→组内均值成维分→
维间加权(缺维重归一),**需要整个横截面**才能标准化。故本模块:
  1) build_revs_panel:逐票逐日算各维原始子因子 + 前瞻收益(无未来函数)
     · S 情绪:动量/换手/波动率 读 kline[:t+1] 尾部
     · E 盈利:按面板日 disclosure_date<=date PIT 选可见最新报告期的 归母净利增速/营收增速/ROE
  2) add_scores:逐日横截面标准化+方向+维内均值+维间合成 → score_composite;同时保留
     score_E / score_S 单维分做 A/B(诚实性:composite 须相对最好单维有净额增量)
  3) 复用 backtest_rank.ic_metrics/decile_metrics + backtest_reversal_turnover.topk_net_metrics

⚠️ V 估值维暂不进本回测:store 只有近期最新快照、无历史 PIT 面板(见设计文档「数据关口」);
   V 待回填百度历史序列后由 --dims 加入。默认 dims=E盈利,S情绪。

诚实性:net 超额为正(gross 不算数)+ 相对最好单维有净额增量,才算合成成立;无增量如实写。
防未来函数:S 因子只读 kdf[:t+1] 尾部;E 只纳入已披露报告期;前瞻收益取 t 之后价仅作被预测标签。
产物只写 worktree 本地,不写主检出、不动 main。⚠️ 非投资建议,历史回测≠未来保证。

用法:python -m tools.backtest.backtest_revs [--sample N] [--seed 42] [--step 5]
      [--horizon 5,10,20] [--topk 20] [--roundtrip-bps 17.5] [--min-liq-pct 0.5]
      [--dims E盈利,S情绪] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.analysis.financial.metrics import compute_derived
from tools.backtest.backtest_rank import _WARMUP, decile_metrics, ic_metrics
from tools.backtest.backtest_reversal_turnover import topk_net_metrics
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy._factor_util import winsorize_med, zscore
from tools.strategy.revs import momentum_factor, turnover_mean, volatility_factor

logger = logging.getLogger("backtest.revs")

_CFG = THRESHOLDS.get("REVS四因子", {})
_DISCLAIMER = "历史回测≠未来保证,非投资建议。"

# 维度 → (子因子名, record内取值键, 方向)。子因子原始列名即面板列名。
_MOM_N = int(_CFG.get("S情绪", {}).get("动量窗口", 20))
_VOL_N = int(_CFG.get("S情绪", {}).get("波动窗口", 20))
_TURN_N = int(_CFG.get("S情绪", {}).get("换手窗口", 20))
_S_DIR = _CFG.get("S情绪", {}).get("方向", {"动量": -1, "换手": -1, "波动率": -1})
_S_W = _CFG.get("S情绪", {}).get("子权重", {"动量": 1.0, "换手": 1.0, "波动率": 1.0})
_E_W = _CFG.get("E盈利", {}).get("子权重", {"归母净利增速": 1.0, "营收增速": 1.0, "ROE": 1.0})
_V_W = _CFG.get("V估值", {}).get("子权重", {"PE_TTM": 1.0, "PB": 1.0, "市值分位": 1.0})
_V_DIR = _CFG.get("V估值", {}).get("方向", {"PE_TTM": -1, "PB": -1, "市值分位": -1})
_DIM_W = _CFG.get("维度权重", {"E盈利": 0.40, "V估值": 0.33, "S情绪": 0.27})

# 面板列名(原始子因子)
_S_COLS = {"动量": "mom", "换手": "turn", "波动率": "vol"}
_E_COLS = {"归母净利增速": "e_np", "营收增速": "e_rev", "ROE": "e_roe"}
# V:PE/PB 直接列;市值分位在 add_scores 里从 v_mv 按当日横截面秩现算


# ————————————————————————— E 盈利:每票预算 PIT 选期器 —————————————————————————
def _earnings_timeline(code: str):
    """读财报 → (derived_all, sorted[(disclosure_date, period)])。缺失 → (None, [])。"""
    try:
        raw = store.get_raw("financial_report", code)
    except Exception:                                        # noqa: BLE001
        return None, []
    periods_raw = raw.get("periods", {}) if isinstance(raw, dict) else {}
    if not periods_raw:
        return None, []
    derived_all = compute_derived(periods_raw)
    disc_list = sorted(
        [(rec.get("disclosure_date"), p) for p, rec in periods_raw.items()
         if rec.get("disclosure_date")],
        key=lambda x: x[0])
    return derived_all, disc_list


def _earnings_asof(derived_all, disc_list, date: str):
    """面板日 date 可见的最新报告期衍生 {归母净利增速,营收增速,ROE};无可见期→None。"""
    if not derived_all or not disc_list:
        return None
    vis = [p for disc, p in disc_list if disc <= date]
    if not vis:
        return None
    d = derived_all.get(vis[-1]) or {}   # disc_list 已按披露日升序,vis[-1]=最新可见
    out = {k: d.get(k) for k in ("归母净利增速", "营收增速", "ROE")}
    return out if any(v is not None for v in out.values()) else None


# ————————————————————————— V 估值:每票历史日序列 as-of 选取器 —————————————————————————
def _valuation_timeline(code: str):
    """读 valuation 整条日序列 → (dates[str], pe[], pb[], mv[]) 按日期升序。缺失→None。"""
    try:
        df = store.get_raw("valuation", code)
    except Exception:                                        # noqa: BLE001
        return None
    if df is None or len(df) == 0 or not {"date", "PE_TTM", "PB", "总市值"} <= set(df.columns):
        return None
    df = df.sort_values("date")
    dates = [str(x)[:10] for x in df["date"].tolist()]
    return (dates, df["PE_TTM"].to_numpy(float),
            df["PB"].to_numpy(float), df["总市值"].to_numpy(float))


def _valuation_asof(tl, date: str):
    """面板日 date 可见的最新 (PE_TTM, PB, 总市值);PE/PB≤0(亏损/负净资产)记 None;无可见→None。

    防未来函数:bisect 取 date 前(含)最后一行(≤ date);同 master_kline / E披露日 PIT 模型。
    """
    if not tl:
        return None
    import bisect
    dates, pe, pb, mv = tl
    i = bisect.bisect_right(dates, date) - 1
    if i < 0:
        return None
    p = float(pe[i]) if (pe[i] == pe[i] and pe[i] > 0) else None
    b = float(pb[i]) if (pb[i] == pb[i] and pb[i] > 0) else None
    m = float(mv[i]) if (mv[i] == mv[i] and mv[i] > 0) else None
    if p is None and b is None and m is None:
        return None
    return p, b, m


# ————————————————————————— 建横截面 panel —————————————————————————
def build_revs_panel(codes, dims, horizons=(5, 10, 20), step: int = 5,
                     warmup: int = _WARMUP) -> pd.DataFrame:
    """逐票逐日算 S(动量/换手/波动率)+ E(PIT 归母净利增速/营收增速/ROE) 原始子因子 + 前瞻收益。

    无未来函数:S 只读 close/turn[:t+1] 尾部;E 只取 disclosure_date<=当日 的报告期;
    前瞻收益 close[t+N]/close[t]-1 用 t 之后价仅作被预测标签。liq=近20日均成交额(close×vol)。
    """
    want_s = "S情绪" in dims
    want_e = "E盈利" in dims
    want_v = "V估值" in dims
    maxN = max(horizons)
    rows = []
    used = 0
    for code in codes:
        try:
            df = market.load_kline(code)
        except Exception:                                    # noqa: BLE001
            continue
        if df is None or len(df) < warmup + maxN + 5:
            continue
        df = df.reset_index(drop=True)
        close = df["close"].to_numpy(float)
        vol = df["volume"].to_numpy(float) if "volume" in df.columns else np.zeros(len(df))
        turn_arr = (df["turnover"].to_numpy(float) if "turnover" in df.columns
                    else np.full(len(df), np.nan))
        amt = close * vol
        dates = [str(x)[:10] for x in df["date"].tolist()]
        n = len(df)
        derived_all, disc_list = _earnings_timeline(code) if want_e else (None, [])
        val_tl = _valuation_timeline(code) if want_v else None
        used += 1
        for t in range(warmup, n - maxN, step):
            date = dates[t]
            row = {"date": date, "code": code,
                   "liq": float(np.mean(amt[max(0, t - 19): t + 1]))}
            ok = False
            if want_s:
                mom = momentum_factor(close[: t + 1], n=_MOM_N)
                tn = turnover_mean(turn_arr[: t + 1], n=_TURN_N)
                vlt = volatility_factor(close[: t + 1], n=_VOL_N)
                row["mom"] = float(mom) if mom is not None else np.nan
                row["turn"] = float(tn) if tn is not None else np.nan
                row["vol"] = float(vlt) if vlt is not None else np.nan
                ok = ok or any(v is not None for v in (mom, tn, vlt))
            if want_e:
                e = _earnings_asof(derived_all, disc_list, date)
                row["e_np"] = float(e["归母净利增速"]) if e and e.get("归母净利增速") is not None else np.nan
                row["e_rev"] = float(e["营收增速"]) if e and e.get("营收增速") is not None else np.nan
                row["e_roe"] = float(e["ROE"]) if e and e.get("ROE") is not None else np.nan
                ok = ok or (e is not None)
            if want_v:
                vv = _valuation_asof(val_tl, date)
                row["v_pe"] = float(vv[0]) if vv and vv[0] is not None else np.nan
                row["v_pb"] = float(vv[1]) if vv and vv[1] is not None else np.nan
                row["v_mv"] = float(vv[2]) if vv and vv[2] is not None else np.nan
                ok = ok or (vv is not None)
            if not ok:
                continue
            for N in horizons:
                row[f"r_{N}"] = float(close[t + N] / close[t] - 1.0) * 100.0
            rows.append(row)
    panel = pd.DataFrame(rows)
    panel.attrs["used"] = used
    return panel


# ————————————————————————— 打分(逐日横截面·维内均值·维间合成)—————————————————————————
def _zdir(series_vals, direction: int, scale: float):
    """对一列 present 值 winsor+zscore 再乘方向;NaN 保持 NaN 位置对齐。返回 np.array。"""
    arr = np.asarray(series_vals, float)
    mask = np.isfinite(arr)
    out = np.full(len(arr), np.nan)
    if mask.sum() >= 1:
        z = zscore(winsorize_med(arr[mask].tolist(), scale=scale))
        out[mask] = np.asarray(z) * direction
    return out


def _dim_from_subs(sub_zcols: dict, weights: dict) -> np.ndarray:
    """维内:多子因子 z 列按权重加权均值(逐行按 present 子因子重归一);全缺→NaN。"""
    keys = list(sub_zcols)
    Z = np.vstack([sub_zcols[k] for k in keys])          # (nsub, nrow)
    W = np.array([float(weights.get(k, 1.0)) for k in keys]).reshape(-1, 1)
    present = np.isfinite(Z)
    wZ = np.where(present, Z * W, 0.0)
    wsum = np.where(present, W, 0.0).sum(axis=0)
    num = wZ.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(wsum > 0, num / wsum, np.nan)


def add_scores(panel: pd.DataFrame, dims, dim_weights, scale: float = 3.0) -> pd.DataFrame:
    """逐日横截面标准化+方向+维内均值+维间合成 → score_composite;保留 score_E/score_S。"""
    if panel.empty:
        return panel
    out = []
    for _, g in panel.groupby("date", sort=False):
        g = g.copy()
        dim_vals = {}
        if "S情绪" in dims:
            subz = {name: _zdir(g[col].to_numpy(), int(_S_DIR.get(name, -1)), scale)
                    for name, col in _S_COLS.items()}
            g["score_S"] = _dim_from_subs(subz, _S_W)
            dim_vals["S情绪"] = g["score_S"].to_numpy()
        if "E盈利" in dims:
            subz = {name: _zdir(g[col].to_numpy(), 1, scale)      # E 全部 +1(值大越好)
                    for name, col in _E_COLS.items()}
            g["score_E"] = _dim_from_subs(subz, _E_W)
            dim_vals["E盈利"] = g["score_E"].to_numpy()
        if "V估值" in dims:
            mv_pct = g["v_mv"].rank(pct=True).to_numpy()          # 市值分位:当日横截面秩(缺mv→NaN)
            subz = {
                "PE_TTM": _zdir(g["v_pe"].to_numpy(), int(_V_DIR.get("PE_TTM", -1)), scale),
                "PB": _zdir(g["v_pb"].to_numpy(), int(_V_DIR.get("PB", -1)), scale),
                "市值分位": _zdir(mv_pct, int(_V_DIR.get("市值分位", -1)), scale),
            }
            g["score_V"] = _dim_from_subs(subz, _V_W)
            dim_vals["V估值"] = g["score_V"].to_numpy()
        # 维间合成(逐行 present 维重归一)
        keys = [d for d in dims if d in dim_vals]
        Z = np.vstack([dim_vals[d] for d in keys])
        W = np.array([float(dim_weights.get(d, 0.0)) for d in keys]).reshape(-1, 1)
        present = np.isfinite(Z)
        num = np.where(present, Z * W, 0.0).sum(axis=0)
        wsum = np.where(present, W, 0.0).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            g["score_composite"] = np.where(wsum > 0, num / wsum, np.nan)
        out.append(g)
    res = pd.concat(out, ignore_index=True)
    # 丢弃 composite 为 NaN 的行(该日该票无任何维度有值)
    return res[np.isfinite(res["score_composite"])].reset_index(drop=True)


def _liq_filter(panel: pd.DataFrame, min_liq_pct: float) -> pd.DataFrame:
    if not min_liq_pct or "liq" not in panel.columns:
        return panel
    keep = panel.groupby("date")["liq"].transform(lambda s: s.rank(pct=True)) >= min_liq_pct
    return panel[keep]


# ————————————————————————— 主流程 —————————————————————————
def run(codes, dims=("E盈利", "V估值", "S情绪"), horizons=(5, 10, 20), step=5, topk=20,
        roundtrip_bps=17.5, min_liq_pct=0.0, dim_weights=None, min_date=None,
        json_path=None):
    dims = list(dims)
    dim_weights = dict(dim_weights or _DIM_W)
    dim_weights = {d: float(w) for d, w in dim_weights.items() if d in dims}

    panel = build_revs_panel(codes, dims, horizons, step=step)
    if panel.empty:
        print("!! panel 为空"); return None
    used = int(panel.attrs.get("used", 0))
    # 限制到公共窗口(E 财报历史只覆盖近 ~3 年;pre-E 时段 composite 退化为纯 S,对比不公平)
    if min_date:
        panel = panel[panel["date"] >= min_date].reset_index(drop=True)
        if panel.empty:
            print(f"!! min_date={min_date} 后 panel 为空"); return None
    if min_liq_pct:
        panel = _liq_filter(panel, min_liq_pct)
    panel = add_scores(panel, dims, dim_weights)
    if panel.empty:
        print("!! 打分后 panel 为空(无任何维度有值)"); return None

    # 对比因子:合成 + 各参与单维
    factors = {"composite": "score_composite"}
    if "E盈利" in dims:
        factors["E盈利"] = "score_E"
    if "V估值" in dims:
        factors["V估值"] = "score_V"
    if "S情绪" in dims:
        factors["S情绪"] = "score_S"

    res = {
        "策略": f"REVS四因子前瞻回测({'+'.join(dims)})",
        "参数": {"参与维度": dims, "维度权重": dim_weights, "topk": topk,
                 "往返成本bps": roundtrip_bps, "流动性过滤分位": min_liq_pct, "step": step,
                 "动量窗口": _MOM_N, "换手窗口": _TURN_N, "波动窗口": _VOL_N},
        "样本股数": used, "总观测": int(len(panel)),
        "交易日数": int(panel["date"].nunique()), "免责": _DISCLAIMER,
        "结果": {},
    }
    print(f"\n===== REVS四因子 前瞻回测({'+'.join(dims)}) · 样本 {used} 只 · 观测 {len(panel)} · "
          f"{res['交易日数']} 交易日 · 往返成本 {roundtrip_bps}bps · 流动性过滤≥{min_liq_pct} · "
          f"维度 {dims} =====")
    print("(横截面·无未来函数;net 超额为正才算能交易;composite 须相对最好单维有增量;非投资建议)\n")

    for fname, col in factors.items():
        res["结果"][fname] = {}
        # 该因子有值的行(单维列对"只有另一维"的票为 NaN,需先滤掉再打分/分层)
        sub = panel[np.isfinite(panel[col])]
        print(f"########## 因子:{fname}(有效观测 {len(sub)}) ##########")
        for N in horizons:
            tmp = sub.rename(columns={col: "score"})
            ic = ic_metrics(tmp, N)
            dec = decile_metrics(tmp, N)
            tk = topk_net_metrics(sub, col, N, k=topk, roundtrip_bps=roundtrip_bps)
            res["结果"][fname][f"{N}日"] = {"IC": ic, "分层": dec, "TopK净额": tk}
            d0 = dec[0]["均收益%"]; d9 = dec[9]["均收益%"]
            mono = round((d9 - d0), 2) if (d0 is not None and d9 is not None) else None
            print(f"  —— {N}日 ——  IC均值={ic.get('IC均值')} ICIR={ic.get('ICIR')} "
                  f"t={ic.get('t')} | 分层D9-D0={mono}pp | "
                  f"TopK: gross年化={tk.get('gross年化超额%')}% net年化={tk.get('net年化超额%')}% "
                  f"换手={tk.get('组合换手率')} net正={tk.get('net是否为正')}")
        print()

    _verdict(res, horizons, dims)
    if json_path:
        from pathlib import Path
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


def _verdict(res: dict, horizons, dims) -> None:
    """诚实判定:composite 的 net 是否 >0 且相对**最好单维**有增量。写入 res['判定'] 并打印。"""
    comp = res["结果"].get("composite", {})
    single = [d for d in ("E盈利", "V估值", "S情绪") if d in dims and d in res["结果"]]
    lines = []
    for N in horizons:
        cn = (comp.get(f"{N}日", {}) or {}).get("TopK净额", {}).get("net年化超额%")
        cic = (comp.get(f"{N}日", {}) or {}).get("IC", {}).get("ICIR")
        singles = {d: (res["结果"][d].get(f"{N}日", {}) or {}).get("TopK净额", {}).get("net年化超额%")
                   for d in single}
        best = max([v for v in singles.values() if v is not None], default=None)
        if cn is None:
            continue
        if best is None:
            verdict = "composite net%s(无单维可比)" % ("为正" if cn > 0 else "≤0")
            incr = None
        else:
            incr = round(cn - best, 1)
            verdict = ("复合净额增量成立" if (cn > 0 and incr > 0)
                       else ("复合net>0但无相对最好单维增量" if cn > 0
                             else "复合net≤0(扣真实成本后不可交易)"))
        sstr = " ".join(f"{d}={v}%" for d, v in singles.items())
        lines.append(f"  {N}日: composite net年化={cn}% (ICIR={cic}) vs 单维[{sstr}] "
                     f"最好={best}% 增量={('%+g' % incr) if incr is not None else 'NA'}pp → {verdict}")
    res["判定"] = lines
    print("========== 诚实判定(net 为准,gross 不算数;须相对最好单维有增量)==========")
    for ln in lines:
        print(ln)
    if not lines:
        print("  (无足够数据判定)")
    print()


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser(description="REVS四因子(阶段1 E+S) 前瞻回测")
    ap.add_argument("--codes", default="")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--step", type=int, default=5, help="每隔几个交易日取一个截面(E慢变,默认5减重叠)")
    ap.add_argument("--horizon", default="5,10,20")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--roundtrip-bps", type=float, default=17.5)
    ap.add_argument("--min-liq-pct", type=float, default=0.0, help="每日剔除成交额分位<此值的票(0~1)")
    ap.add_argument("--dims", default="E盈利,V估值,S情绪", help="参与维度(逗号分隔;默认完整三维E/V/S)")
    ap.add_argument("--min-date", default="", help="只保留>=此日期的截面(公平对比E:财报史~3年,默认不限)")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    codes = [c for c in a.codes.split(",") if c] or None
    if a.sample:
        import random
        allc = sorted(store.list_master_codes())
        codes = random.Random(a.seed).sample(allc, min(a.sample, len(allc)))
    run(codes=codes, dims=tuple(d.strip() for d in a.dims.split(",") if d.strip()),
        horizons=tuple(int(x) for x in a.horizon.split(",")),
        step=a.step, topk=a.topk, roundtrip_bps=a.roundtrip_bps,
        min_liq_pct=a.min_liq_pct, min_date=a.min_date or None, json_path=a.json or None)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
