"""治本 · 前向累积记分卡:每日 picks + 预测方向 + 净情绪分 → 配"到期实际前瞻收益"。

痛点:点位情绪/预测只存了极短窗口,研究 C 现在统计力≈0。治本靠**滚存累积**:
每天把当日 record 的方向判断(趋势评级 / 净情绪分)记一行,等 K 线走出 t+N 后**自动
回填**该行的实际前瞻收益与"方向命中没"。跑几周就攒出真正可回测的样本,是研究 C 的
长期数据来源,也是"昨日选股 vs 今日实际"记分卡。

幂等设计(可每天重跑):
  · 每次运行**重读全部历史 record**,重算每 (date, code) 的方向标签;
  · 前瞻收益按当前 K 线能算多少算多少(已到期→填实际值,未到期→留空 pending);
  · 全量覆盖写出(而非 append 去重),因此重复运行只会把"新到期"的行补上,不产生脏数据。

无未来函数:方向标签只用信号日 t 及之前(record 是 t 收盘后落的);前瞻收益 close[t+N]
仅作结果标签。命中 = sign(前瞻收益) 与预测方向一致。

字段:date, code, name, trend(趋势评级), senti(净情绪分), pred_dir(合成预测方向 +1/-1/0),
      r_1/r_5/r_10(到期实际%,未到期=空), hit_1/hit_5/hit_10(方向命中 1/0,未到期=空)。

用法:python -m tools.backtest.forward_scorecard [--out path.csv] [--horizon 1,5,10]
      缺省 --out 落 scratch;部署到每日闭环时由上层传持久路径(如 data/analysis/backtest/)。
非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import os

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.scorecard")

_DEFAULT_OUT = os.path.join(
    "/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
    "c3f60e01-bbca-41c6-b337-cf7966926ca4/scratchpad", "forward_scorecard.csv")


def _pred_dir(trend: str | None, senti: float | None) -> int:
    """合成预测方向:趋势评级为主(偏多+1/偏空-1/中性0),中性时用净情绪分符号兜底。"""
    if trend == "偏多":
        return 1
    if trend == "偏空":
        return -1
    if senti is not None and np.isfinite(senti):
        if senti > 0.05:
            return 1
        if senti < -0.05:
            return -1
    return 0


def build_scorecard(dates=None, horizons=(1, 5, 10)) -> pd.DataFrame:
    """重读全部历史 record → 逐 (date, code) 一行,回填已到期前瞻收益 + 方向命中。"""
    if dates is None:
        dates = store.list_dates()
    kline_cache: dict[str, pd.DataFrame | None] = {}

    def _kline(code):
        if code not in kline_cache:
            try:
                kline_cache[code] = market.load_kline(code).reset_index(drop=True)
            except Exception:
                kline_cache[code] = None
        return kline_cache[code]

    rows = []
    for d in dates:
        for rec in store.iter_records(date=d):
            meta = rec.get("meta") or {}
            code = meta.get("code")
            if not code:
                continue
            trend = ((rec.get("signals") or {}).get("trend") or {}).get("评级")
            senti = ((rec.get("sentiment") or {}).get("净情绪分"))
            try:
                senti = float(senti) if senti is not None else None
            except (TypeError, ValueError):
                senti = None
            pdir = _pred_dir(trend, senti)
            row = {"date": d, "code": str(code), "name": meta.get("name"),
                   "trend": trend, "senti": senti, "pred_dir": pdir}

            df = _kline(str(code))
            kdates = [str(x)[:10] for x in df["date"].tolist()] if df is not None and "date" in df.columns else []
            idx = kdates.index(d) if d in kdates else None
            close = df["close"].to_numpy(float) if idx is not None else None
            for N in horizons:
                r = np.nan
                hit = None
                if idx is not None and idx + N < len(close) and close[idx] > 0:
                    r = float(close[idx + N] / close[idx] - 1.0) * 100.0
                    if pdir != 0:
                        hit = int(np.sign(r) == np.sign(pdir))
                row[f"r_{N}"] = r
                row[f"hit_{N}"] = hit
            rows.append(row)
    df = pd.DataFrame(rows)
    return df.sort_values(["date", "code"]).reset_index(drop=True) if not df.empty else df


def summarize(sc: pd.DataFrame, horizons=(1, 5, 10)) -> dict:
    """当前记分卡的方向命中率概览(仅统计已到期 + 方向非中性的行)。"""
    out = {"总行数": int(len(sc))}
    for N in horizons:
        hcol = f"hit_{N}"
        matured = sc.dropna(subset=[hcol])
        n = len(matured)
        out[f"{N}日"] = {"已到期且有方向": int(n),
                        "方向命中率%": round(float(matured[hcol].mean()) * 100, 1) if n else None,
                        "待到期(pending)": int(sc[f"r_{N}"].isna().sum())}
    return out


def run(out=_DEFAULT_OUT, horizons=(1, 5, 10)):
    sc = build_scorecard(horizons=horizons)
    print("\n===== 前向累积记分卡(治本·滚存)=====")
    print("(无未来函数;历史回测≠未来保证,非投资建议)\n")
    if sc.empty:
        print("!! 无 record,记分卡为空")
        return sc
    # 幂等:全量覆盖写(重复运行只补新到期行)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    sc.to_csv(out, index=False, encoding="utf-8-sig")
    summ = summarize(sc, horizons)
    print(f"已写 {len(sc)} 行 → {out}")
    print(f"覆盖日期: {sorted(sc['date'].unique())}")
    for N in horizons:
        s = summ[f"{N}日"]
        print(f"  {N}日: 已到期且有方向={s['已到期且有方向']}  方向命中率={s['方向命中率%']}%  "
              f"待到期={s['待到期(pending)']}")
    print("\n(样本仍薄;每日重跑此脚本即自动把新到期的前瞻收益补进记分卡。)")
    return sc


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--horizon", default="1,5,10")
    a = ap.parse_args()
    run(out=a.out, horizons=tuple(int(x) for x in a.horizon.split(",")))
