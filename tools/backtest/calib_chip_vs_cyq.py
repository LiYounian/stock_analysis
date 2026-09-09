"""S05「最强选股」切本地筹码 · 阶段一校准脚本(**只做校准,不改生产代码**)。

目标:切换前用**有 Tushare cyq_perf 真值的历史交易日**,逐 (日,票) 对比
  本地推演筹码(tools.collectors.chip)与 Tushare cyq_perf 真值,判两级 gate 是否达标:
    甲 因子级对齐:winner_rate vs 获利比例×100(方案A百分数口径)、cost_95pct vs 成本区间上沿;
    乙 信号级质量:新旧口径各跑 screen_strong.signal_at(①②③不变、仅换④)→ 入选集合
       Jaccard + T+1/3/5/10 前向收益,新口径 ≥ 旧口径 × 0.9 才算不掉。

口径(依据 docs/计划/2026-09-09_最强选股切本地筹码_方案.md,用户已拍板):
  · 校准数据范围:近 N 交易日(默认120)× 流动性前 M(默认1500,按近60日成交额中位数排名,排北交所)。
  · 阈值口径:方案A(百分数)——本地 `获利比例`(0~1)× 100 复用现有 95.0 语义。
  · gate:因子相关(高获利区 Spearman)≥ 0.6;信号一致率(新旧入选集合 Jaccard 均值)≥ 0.80;
          前向收益(T+5 均值/胜率)≥ 旧口径 × 0.9。

无未来函数:本地筹码走 chip.summarize_asof(df, as_of) 只用 ≤as_of 的 bar;真值取当日 cyq_perf;
  前向收益 close[t+N] 仅作结果标签(t 之后价),不参与入选判定。

⚠️ 非投资建议,纯工程口径校验。产物默认写 data/analysis/backtest/(json)+ 由调用方另写 md 报告。
用法:
  python -m tools.backtest.calib_chip_vs_cyq [--days 120] [--topn 1500]
        [--thresholds 90,92,94,95,96] [--out data/analysis/backtest/calib_chip_vs_cyq_result.json]
        [--max-codes N(调试限量)] [--end 2026-09-08(真值截止日,默认自动取最近有真值的交易日)]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

from tools.collectors import chip, tushare_daily
from tools.config.strategy import THRESHOLDS
from tools.pipeline import screen_strong
from tools.store import repo as store

logger = logging.getLogger("backtest.calib_chip")

_BJ_PREFIX = ("8", "4")
_CFG = THRESHOLDS["最强选股"]
_FWD_WINDOWS = (1, 3, 5, 10)
_HIGH_PROFIT_LO = 90.0     # 高获利区下界(winner_rate≥90,S05 真正用到的区间)
_LIQ_LOOKBACK = 60         # 流动性排名回看根数(成交额中位数)


# ----------------------------------------------------------------------------- 数据准备
def _load_token_from_env_file() -> None:
    """token 仅从 env 读;若 shell 未 export,尝试从本机受限文件补(不入库、不打印)。"""
    if tushare_daily.is_configured():
        return
    envf = os.path.expanduser(os.environ.get("STOCK_SYNC_ENV", "~/.config/stock/sync.env"))
    if not os.path.exists(envf):
        return
    for line in open(envf):
        line = line.strip()
        if line.startswith("TUSHARE_TOKEN="):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            if val:
                os.environ["TUSHARE_TOKEN"] = val
            break
    # 重新加载 settings 模块级常量(settings.TUSHARE_TOKEN 在 import 时定格)
    from tools.config import settings
    settings.TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
    settings.TUSHARE_ENABLED = bool(settings.TUSHARE_TOKEN)


def calib_trade_dates(days: int, end: str | None) -> list[str]:
    """取最近 days 个**有 cyq_perf 真值**的交易日(升序)。

    end 给定则以其为真值截止;否则从交易日历倒推、逐日试探 fetch_chip 找到最近有真值的日子。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    start = (pd.Timestamp(today) - pd.Timedelta(days=int(days * 2.2) + 30)).strftime("%Y-%m-%d")
    cal = tushare_daily.trade_dates(start, end or today)   # Tushare 返回**降序**,统一转升序
    cal = sorted(d for d in set(cal) if d <= (end or today))
    if end is None:
        # 从最近交易日倒推,找到最近一个 cyq_perf 非空的交易日作为真值截止(排除今日未发布)
        for d in reversed(cal):
            try:
                df = tushare_daily.fetch_chip(d)
                if df is not None and not df.empty:
                    end = d
                    break
            except Exception:
                continue
        if end is None:
            raise RuntimeError("回溯若干交易日均无 cyq_perf 真值,无法校准(Tushare 完全不可用)")
        cal = [d for d in cal if d <= end]
    return cal[-days:]


def liquidity_universe(topn: int, max_codes: int | None) -> list[str]:
    """按近 _LIQ_LOOKBACK 根成交额中位数排名取流动性前 topn(排北交所)。小票筹码推演噪声大,故剔除。"""
    codes = [c for c in store.list_master_codes() if c[:1] not in _BJ_PREFIX]
    rows = []
    for c in codes:
        try:
            df = store.get_master_kline(c)
        except Exception:
            continue
        amt = pd.to_numeric(df.get("amount"), errors="coerce").dropna()
        if len(amt) < 20:
            continue
        rows.append((c, float(amt.tail(_LIQ_LOOKBACK).median())))
    rows.sort(key=lambda x: x[1], reverse=True)
    uni = [c for c, _ in rows[:topn]]
    if max_codes:
        uni = uni[:max_codes]
    logger.info("流动性票池:候选 %d → 取前 %d(排北交所;成交额中位数排名)", len(rows), len(uni))
    return uni


# ----------------------------------------------------------------------------- 配对采集
def collect_pairs(codes: list[str], dates: list[str],
                  thresholds: list[float]) -> tuple[pd.DataFrame, dict]:
    """逐 (日,票) 配对本地推演 vs cyq_perf 真值 + 逐日新旧口径 signal_at 入选。

    返回:
      pairs_df: 每行一个 (date, code) 配对(甲用),含 wr_true/wr_local/cost95_*/high/degrade。
      sig: {date: {"old": set(codes), "new": {thr: set(codes)}, "fwd": {code: {N: ret}}}}(乙用)。
    """
    dates_set = set(dates)
    # —— 真值:每交易日一次 cyq_perf(整市场),按票裁到 universe ——
    truth: dict[str, dict[str, tuple]] = {}
    uni_set = set(codes)
    for d in dates:
        try:
            tdf = tushare_daily.fetch_chip(d)
        except Exception as e:
            logger.warning("cyq_perf(%s) 取失败,该日跳过:%s", d, e)
            continue
        truth[d] = {r["code"]: (r["winner_rate"], r["cost_95pct"])
                    for _, r in tdf.iterrows() if r["code"] in uni_set}
    valid_dates = [d for d in dates if d in truth]
    logger.info("真值可得交易日 %d/%d", len(valid_dates), len(dates))

    pair_rows: list[dict] = []
    sig: dict = {d: {"old": set(), "new": {t: set() for t in thresholds}, "fwd": {}}
                 for d in valid_dates}
    need = int(_CFG["最少历史根数"])
    n_codes = len(codes)
    for ci, code in enumerate(codes, 1):
        if ci % 200 == 0:
            logger.info("[%d/%d] 配对推进中...", ci, n_codes)
        try:
            df = store.get_master_kline(code)
        except Exception:
            continue
        if df is None or len(df) < need:
            continue
        df = df.reset_index(drop=True)
        dser = pd.to_datetime(df["date"])
        close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
        high = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
        pos = {dser.iloc[i].strftime("%Y-%m-%d"): i for i in range(len(df))}
        n = len(df)
        for d in valid_dates:
            t = pos.get(d)
            if t is None or t < need:
                continue
            tv = truth[d].get(code)
            # —— 本地 point-in-time 推演(只用 ≤d 的 bar)——
            # 主档按 date 升序 → df.iloc[:t+1] 等价 summarize_asof 的 date<=d 切片(无未来函数);
            # 推演窗口仅 _WINDOW(250)根,直接切最后 250 根喂 chip.summarize(生产函数),
            # 避开 summarize_asof 每次重解析整列 date 的热点(180k 次)。
            lo = max(0, t - chip._WINDOW + 1)
            rec = chip.summarize(df.iloc[lo:t + 1])
            wr_local = rec.get("获利比例")
            cost95_local = rec.get("成本区间上沿")
            degrade = bool(rec.get("降级"))
            wr_local_pct = (float(wr_local) * 100.0) if wr_local is not None else None
            # —— 甲:配对(需两侧都有)——
            if tv is not None and wr_local_pct is not None:
                wr_true, cost95_true = tv
                if pd.notna(wr_true):
                    pair_rows.append({
                        "date": d, "code": code,
                        "wr_true": float(wr_true),
                        "wr_local": float(wr_local_pct),
                        "cost95_true": (float(cost95_true) if pd.notna(cost95_true) else np.nan),
                        "cost95_local": (float(cost95_local) if cost95_local is not None else np.nan),
                        "high": float(high[t]),
                        "degrade": degrade,
                    })
            # —— 乙:signal_at 新旧口径入选(①②③相同,仅④换源)——
            # ①②③ 与 chip 无关(仅 OHLC),先算一次 base 门控:三者不全过 → 新旧皆不可能入选,
            # 跳过 6 次带 chip 的 signal_at(热点优化,>99% 样本在此短路,与逐次调用等价)。
            base = screen_strong.signal_at(df, t, chip=None)
            c123 = bool(base.get("C1_六均线多头") and base.get("C2_近期连涨")
                        and base.get("C3_高位区间"))
            if c123:
                # 旧口径:真值 chip
                if tv is not None and pd.notna(tv[0]):
                    chip_old = {"winner_rate": float(tv[0]),
                                "cost_95pct": (float(tv[1]) if pd.notna(tv[1]) else None)}
                    if screen_strong.signal_at(df, t, chip=chip_old).get("SELECT"):
                        sig[d]["old"].add(code)
                # 新口径:本地映射 {winner_rate=获利比例×100, cost_95pct=成本区间上沿},阈值网格
                if wr_local_pct is not None:
                    for thr in thresholds:
                        cfg = dict(_CFG); cfg["获利比阈值"] = float(thr)
                        chip_new = {"winner_rate": wr_local_pct, "cost_95pct": cost95_local}
                        if screen_strong.signal_at(df, t, chip=chip_new, cfg=cfg).get("SELECT"):
                            sig[d]["new"][thr].add(code)
            # —— 前向收益标签(结果侧,t 之后)——
            fwd = {}
            for N in _FWD_WINDOWS:
                fwd[N] = (float(close[t + N] / close[t] - 1.0)
                          if (t + N) < n and close[t] > 0 else None)
            sig[d]["fwd"][code] = fwd
    return pd.DataFrame(pair_rows), sig


# ----------------------------------------------------------------------------- 甲 因子对齐
def _corr(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """(pearson, spearman);样本<3 或方差 0 → (nan, nan)。"""
    if len(a) < 3:
        return float("nan"), float("nan")
    pear = float(np.corrcoef(a, b)[0, 1]) if a.std() and b.std() else float("nan")
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    spear = float(np.corrcoef(ra, rb)[0, 1]) if ra.std() and rb.std() else float("nan")
    return pear, spear


def factor_report(pairs: pd.DataFrame) -> dict:
    def _block(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"n": 0}
        a = df["wr_local"].to_numpy(); b = df["wr_true"].to_numpy()
        pear, spear = _corr(a, b)
        bias = a - b
        out = {
            "n": int(len(df)),
            "pearson": round(pear, 4), "spearman": round(spear, 4),
            "MAE": round(float(np.mean(np.abs(bias))), 3),
            "RMSE": round(float(np.sqrt(np.mean(bias ** 2))), 3),
            "bias_mean": round(float(np.mean(bias)), 3),
            "bias_median": round(float(np.median(bias)), 3),
            "bias_p5": round(float(np.percentile(bias, 5)), 3),
            "bias_p95": round(float(np.percentile(bias, 95)), 3),
        }
        # cost95 相对误差(两侧非空)
        cc = df.dropna(subset=["cost95_true", "cost95_local"])
        cc = cc[cc["cost95_true"] > 0]
        if len(cc):
            rel = (cc["cost95_local"] - cc["cost95_true"]) / cc["cost95_true"]
            out["cost95_relerr_median"] = round(float(rel.median()), 4)
            out["cost95_relerr_absmedian"] = round(float(rel.abs().median()), 4)
            out["cost95_n"] = int(len(cc))
            # 布尔量 HIGH≥cost95 两口径一致率
            bt = (cc["high"] >= cc["cost95_true"])
            bl = (cc["high"] >= cc["cost95_local"])
            out["cost95_bool_agree"] = round(float((bt == bl).mean()), 4)
        return out

    rep = {"overall": _block(pairs)}
    hp = pairs[pairs["wr_true"] >= _HIGH_PROFIT_LO]
    rep["high_profit(wr_true>=90)"] = _block(hp)
    rep["degrade=False"] = _block(pairs[~pairs["degrade"]])
    rep["degrade=True"] = _block(pairs[pairs["degrade"]])
    return rep


# ----------------------------------------------------------------------------- 乙 信号质量
def _fwd_stats(codes_by_date: dict[str, set], fwd_by_date: dict) -> dict:
    """一组逐日入选集合 → T+N 前向收益均值/中位/胜率/命中数。"""
    out = {}
    for N in _FWD_WINDOWS:
        rets = []
        for d, cs in codes_by_date.items():
            fmap = fwd_by_date.get(d, {})
            for c in cs:
                v = fmap.get(c, {}).get(N)
                if v is not None:
                    rets.append(v)
        if rets:
            arr = np.array(rets)
            out[f"T+{N}"] = {"n": int(len(arr)),
                            "mean": round(float(arr.mean()) * 100, 4),
                            "median": round(float(np.median(arr)) * 100, 4),
                            "winrate": round(float((arr > 0).mean()), 4)}
        else:
            out[f"T+{N}"] = {"n": 0, "mean": None, "median": None, "winrate": None}
    return out


def signal_report(sig: dict, thresholds: list[float]) -> dict:
    dates = sorted(sig.keys())
    fwd_by_date = {d: sig[d]["fwd"] for d in dates}
    old_by_date = {d: sig[d]["old"] for d in dates}

    # Jaccard(逐日)与入选数
    def _jaccard(a: set, b: set) -> float:
        u = a | b
        return (len(a & b) / len(u)) if u else 1.0

    old_counts = [len(sig[d]["old"]) for d in dates]
    rep = {
        "n_dates": len(dates),
        "old_selected_total": int(sum(old_counts)),
        "old_selected_per_day_mean": round(float(np.mean(old_counts)), 3) if dates else 0,
        "old_forward": _fwd_stats(old_by_date, fwd_by_date),
        "by_threshold": {},
    }
    for thr in thresholds:
        new_by_date = {d: sig[d]["new"][thr] for d in dates}
        jacc = [_jaccard(sig[d]["old"], sig[d]["new"][thr]) for d in dates
                if (sig[d]["old"] or sig[d]["new"][thr])]
        new_counts = [len(sig[d]["new"][thr]) for d in dates]
        newf = _fwd_stats(new_by_date, fwd_by_date)
        oldf = rep["old_forward"]
        ratio = {}
        for N in _FWD_WINDOWS:
            om = oldf[f"T+{N}"]["mean"]; nm = newf[f"T+{N}"]["mean"]
            ratio[f"T+{N}_mean_ratio"] = (round(nm / om, 3)
                                          if (om not in (None, 0) and nm is not None) else None)
        rep["by_threshold"][str(thr)] = {
            "jaccard_mean": round(float(np.mean(jacc)), 4) if jacc else None,
            "new_selected_total": int(sum(new_counts)),
            "new_selected_per_day_mean": round(float(np.mean(new_counts)), 3) if dates else 0,
            "new_forward": newf,
            "new_vs_old_mean_ratio": ratio,
        }
    return rep


# ----------------------------------------------------------------------------- gate 判定
def evaluate_gates(factor: dict, signal: dict, thresholds: list[float]) -> dict:
    hp = factor.get("high_profit(wr_true>=90)", {})
    g1_corr = hp.get("spearman")
    g1 = (g1_corr is not None and not pd.isna(g1_corr) and g1_corr >= 0.6)
    # 选一个"最佳阈值":Jaccard 最高且前向 T+5 比值 ≥0.9 优先
    best_thr = None; best_score = -1
    per_thr = {}
    for thr in thresholds:
        b = signal["by_threshold"][str(thr)]
        jacc = b["jaccard_mean"]
        r5 = b["new_vs_old_mean_ratio"].get("T+5_mean_ratio")
        g2 = (jacc is not None and jacc >= 0.80)
        g3 = (r5 is not None and r5 >= 0.90)
        per_thr[str(thr)] = {"jaccard": jacc, "T+5_ratio": r5,
                             "gate2_一致率>=0.80": bool(g2), "gate3_前向>=0.9旧": bool(g3),
                             "pass": bool(g2 and g3)}
        score = (jacc or 0) + (0.5 if g3 else 0)
        if score > best_score:
            best_score = score; best_thr = thr
    overall_pass = bool(g1 and per_thr.get(str(best_thr), {}).get("pass"))
    return {
        "gate1_因子相关(高获利区Spearman>=0.6)": {"value": g1_corr, "pass": bool(g1)},
        "gate2_3_by_threshold": per_thr,
        "best_threshold": best_thr,
        "overall_pass": overall_pass,
        "结论": ("达标可切" if overall_pass else "未达标,不切/需调阈值或先解决数据"),
    }


# ----------------------------------------------------------------------------- 主流程
def run(days: int, topn: int, thresholds: list[float], out: str,
        max_codes: int | None, end: str | None) -> dict:
    _load_token_from_env_file()
    if not tushare_daily.is_configured():
        raise RuntimeError("TUSHARE_TOKEN 未配置,无法取 cyq_perf 真值做校准(见报告 blocker 分支)")
    dates = calib_trade_dates(days, end)
    logger.info("校准交易日 %d 天:%s ... %s", len(dates), dates[0], dates[-1])
    codes = liquidity_universe(topn, max_codes)
    pairs, sig = collect_pairs(codes, dates, thresholds)
    logger.info("配对样本 %d 行", len(pairs))
    factor = factor_report(pairs)
    signal = signal_report(sig, thresholds)
    gates = evaluate_gates(factor, signal, thresholds)
    result = {
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "口径": {"方案": "A(百分数,winner_rate 复用95.0语义)",
                 "校准范围": f"近{len(dates)}交易日 × 流动性前{len(codes)}(排北交所,成交额中位数排名)",
                 "真值": "Tushare cyq_perf(winner_rate/cost_95pct)",
                 "本地": "chip.summarize_asof point-in-time(获利比例×100 / 成本区间上沿)",
                 "阈值网格": thresholds},
        "交易日范围": [dates[0], dates[-1]],
        "配对样本数": int(len(pairs)),
        "甲_因子对齐": factor,
        "乙_信号质量": signal,
        "gate判定": gates,
    }
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info("校准结果写出:%s", out)
    return result


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    # 批量点位推演每票每日都会触发 chip 的"换手缺失降级"WARNING(数万条),淹没进度日志;
    # 降级信息**未丢失**——每个配对的 `degrade` 字段已单独记录并在报告里分层统计。此处仅静音噪声。
    logging.getLogger("collectors.chip").setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(description="S05 切本地筹码 · 阶段一校准(不改生产代码)")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--topn", type=int, default=1500)
    ap.add_argument("--thresholds", default="90,92,94,95,96")
    ap.add_argument("--end", default=None, help="真值截止交易日(默认自动取最近有真值日)")
    ap.add_argument("--max-codes", type=int, default=None, help="调试限量")
    ap.add_argument("--out", default="data/analysis/backtest/calib_chip_vs_cyq_result.json")
    a = ap.parse_args(argv)
    thrs = [float(x) for x in a.thresholds.split(",") if x.strip()]
    res = run(a.days, a.topn, thrs, a.out, a.max_codes, a.end)
    g = res["gate判定"]
    logger.info("=== gate:%s(best_thr=%s)===", g["结论"], g["best_threshold"])
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
