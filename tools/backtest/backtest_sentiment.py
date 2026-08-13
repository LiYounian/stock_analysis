"""研究 C · 情绪信号回测器:存储的点位「净情绪分」vs 前瞻 N 日收益。

与 backtest_rank 的区别:打分**不是**从 K 线现算,而是读**已落盘的点位情绪面板**
——逐日 `store.iter_records(date=d)` 取每票 `sentiment.净情绪分`(LLM 三层加权,-1~1),
配该票**前瞻 N 日收益**(从 `market.load_kline` 取 close[t+N]/close[t]-1)。

无未来函数(红线):
  · 情绪分 = 信号日 d 收盘后已知(events 时间戳均在 d 当日/之前);
  · 进场价 = close[d];标签 = close[d+N](d 之后价,仅作被预测收益,不参与选样)。
  · 不在 d 之后回看任何信息定情绪。

指标(复用 backtest_rank 的 IC/分层写法):
  1. 横截面 IC(Spearman: 净情绪分 vs r_N)+ ICIR/t —— 有预测力才显著>0。
  2. 分层(每日按情绪分横截面分档,池化各档前瞻收益;单调递增=有效)。
  3. 上涨命中校准 —— 按情绪分分桶,各桶 P(r_N>0),看"情绪越正→上涨概率越高"是否成立。

⚠️ 数据现实:点位情绪只存了极短窗口(约 08-06~08-13 共 7 个交易日、每日子集),
且前瞻收益需要信号日之后的 K 线 → **能算出前瞻收益的观测极少**(见 run 报告的
"有效观测/交易日")。故本回测**统计力接近零,结论只能方向性**,脚本会如实打印样本量。

用法:python -m tools.backtest.backtest_sentiment [--horizon 1,2,3,5,10] [--json out.json]
非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store
# 复用排序型回测的横截面指标(无 scipy 依赖的 Spearman / IC / 分层)
from tools.backtest.backtest_rank import _spearman, ic_metrics, decile_metrics

logger = logging.getLogger("backtest.sentiment")

_DISCLAIMER = "历史回测≠未来保证,非投资建议。样本极短,仅方向性。"


# ————————————————————————— 建情绪横截面 panel —————————————————————————
def build_sentiment_panel(dates=None, horizons=(1, 2, 3, 5, 10)) -> pd.DataFrame:
    """逐日读存储的净情绪分,配前瞻 N 日收益,落长表 (date, code, score, r_N...)。

    · dates 缺省 = store.list_dates() 全部有 records 的日期。
    · score = record['sentiment']['净情绪分'](缺失/None 跳过)。
    · r_N:在该票 K 线里定位信号日 d 的行,close[idx+N]/close[idx]-1;越界(前瞻不足)
      → 该 horizon 留 NaN(仍保留该行,供短 horizon 用)。
    · 无未来函数:情绪只用 d 当日已知,收益仅作标签。
    返回 panel;panel.attrs 记录覆盖诊断(每日有效票数等)。
    """
    if dates is None:
        dates = store.list_dates()
    maxN = max(horizons)

    # 预载 K 线缓存(每票只读一次)
    kline_cache: dict[str, pd.DataFrame | None] = {}

    def _kline(code: str):
        if code not in kline_cache:
            try:
                df = market.load_kline(code)
                df = df.reset_index(drop=True)
                kline_cache[code] = df
            except Exception:
                kline_cache[code] = None
        return kline_cache[code]

    rows = []
    diag = {}  # date -> {记录数, 有情绪分, 有前瞻r5}
    for d in dates:
        n_rec = n_score = 0
        n_fwd = {N: 0 for N in horizons}
        for rec in store.iter_records(date=d):
            n_rec += 1
            code = (rec.get("meta") or {}).get("code")
            senti = (rec.get("sentiment") or {})
            score = senti.get("净情绪分")
            if code is None or score is None:
                continue
            try:
                score = float(score)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(score):
                continue
            n_score += 1

            df = _kline(str(code))
            if df is None or "date" not in df.columns or "close" not in df.columns:
                continue
            kdates = [str(x)[:10] for x in df["date"].tolist()]
            if d not in kdates:
                continue  # 信号日无 K 线(停牌/未落地)→ 无法定进场价
            idx = kdates.index(d)
            close = df["close"].to_numpy(float)
            row = {"date": d, "code": str(code), "score": score}
            for N in horizons:
                if idx + N < len(close) and close[idx] > 0:
                    row[f"r_{N}"] = float(close[idx + N] / close[idx] - 1.0) * 100.0
                    n_fwd[N] += 1
                else:
                    row[f"r_{N}"] = np.nan
            rows.append(row)
        diag[d] = {"记录数": n_rec, "有净情绪分": n_score,
                   **{f"有r_{N}": n_fwd[N] for N in horizons}}

    panel = pd.DataFrame(rows)
    panel.attrs["diag"] = diag
    return panel


# ————————————————————————— 上涨命中校准 —————————————————————————
def calibration(panel: pd.DataFrame, N: int, n_bins: int = 5) -> dict:
    """按净情绪分分桶(全样本池化,固定阈值分桶),各桶 P(r_N>0) + 均收益。

    看"情绪越正→上涨概率越高"是否成立(单调即校准良好)。桶用分位切,样本少
    时自动降桶数。另给"正情绪 vs 负情绪"二分对照(最稳健的方向性判据)。
    """
    col = f"r_{N}"
    sub = panel.dropna(subset=[col, "score"])
    if len(sub) < 4:
        return {"n": int(len(sub)), "说明": "样本不足,无法校准"}

    out = {"n": int(len(sub))}
    # 分位分桶
    try:
        nb = min(n_bins, max(2, len(sub) // 3))
        cats = pd.qcut(sub["score"], nb, duplicates="drop")
        bins = []
        for cat, g in sub.groupby(cats, observed=True):
            r = g[col].to_numpy()
            bins.append({"情绪区间": str(cat), "n": int(len(g)),
                         "上涨占比%": round(float((r > 0).mean()) * 100, 1),
                         "均收益%": round(float(r.mean()), 2)})
        out["分桶"] = bins
    except Exception as e:
        out["分桶"] = f"分桶失败: {e}"

    # 正/负 情绪二分
    pos = sub[sub["score"] > 0][col].to_numpy()
    neg = sub[sub["score"] < 0][col].to_numpy()
    out["正情绪"] = {"n": int(len(pos)),
                    "上涨占比%": round(float((pos > 0).mean()) * 100, 1) if len(pos) else None,
                    "均收益%": round(float(pos.mean()), 2) if len(pos) else None}
    out["负情绪"] = {"n": int(len(neg)),
                    "上涨占比%": round(float((neg > 0).mean()) * 100, 1) if len(neg) else None,
                    "均收益%": round(float(neg.mean()), 2) if len(neg) else None}
    return out


# ————————————————————————— 主流程 —————————————————————————
def run(dates=None, horizons=(1, 2, 3, 5, 10), json_path=None, min_cross=5):
    panel = build_sentiment_panel(dates, horizons)
    diag = panel.attrs.get("diag", {})

    print(f"\n===== 研究 C · 情绪信号回测(存储净情绪分 vs 前瞻收益)=====")
    print(f"(无未来函数;{_DISCLAIMER})\n")
    print("—— 数据覆盖诊断(每日)——")
    for d, dd in diag.items():
        print(f"  {d}: " + "  ".join(f"{k}={v}" for k, v in dd.items()))
    print()

    if panel.empty:
        print("!! panel 为空,无任何可用观测。")
        res = {"错误": "panel 为空", "诊断": diag, "免责": _DISCLAIMER}
        if json_path:
            from pathlib import Path
            Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return res

    res = {"总观测行": int(len(panel)), "交易日数(有情绪分)": int(panel["date"].nunique()),
           "min_cross(IC最小横截面票数)": min_cross, "诊断": diag, "免责": _DISCLAIMER}

    for N in horizons:
        col = f"r_{N}"
        valid = panel.dropna(subset=[col])
        n_obs = int(len(valid))
        n_days = int(valid["date"].nunique()) if n_obs else 0
        print(f"—— 前瞻 {N} 交易日 ——  (有效观测={n_obs}, 覆盖交易日={n_days})")
        if n_obs == 0:
            print("   无前瞻收益可算(信号日之后 K 线不足)。\n")
            res[f"{N}日"] = {"有效观测": 0, "说明": "前瞻数据不足"}
            continue
        ic = ic_metrics(valid, N, min_cross=min_cross)
        dec = decile_metrics(valid, N, min_cross=min_cross)
        cal = calibration(valid, N)
        res[f"{N}日"] = {"有效观测": n_obs, "覆盖交易日": n_days,
                        "IC": ic, "分层": dec, "校准": cal}
        print(f"  [IC] " + "  ".join(f"{k}={v}" for k, v in ic.items()))
        pos, neg = cal.get("正情绪", {}), cal.get("负情绪", {})
        print(f"  [校准] 正情绪 n={pos.get('n')} 上涨{pos.get('上涨占比%')}% 均{pos.get('均收益%')}%  |  "
              f"负情绪 n={neg.get('n')} 上涨{neg.get('上涨占比%')}% 均{neg.get('均收益%')}%")
        print()

    if json_path:
        from pathlib import Path
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="", help="逗号分隔 YYYY-MM-DD;缺省=全部有记录日期")
    ap.add_argument("--horizon", default="1,2,3,5,10")
    ap.add_argument("--min-cross", type=int, default=5)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    dates = [x for x in a.dates.split(",") if x] or None
    run(dates=dates, horizons=tuple(int(x) for x in a.horizon.split(",")),
        json_path=a.json or None, min_cross=a.min_cross)
