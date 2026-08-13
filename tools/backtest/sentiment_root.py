"""研究 C' · 根源净情绪 IC(政策层 + 公告事件,弃舆情/降权新闻)。

对上一轮研究 C(全三层加权 `净情绪分`)的收窄:只从**根源消息**取情绪 →
  · 政策层净情绪:`sentiment.三层.政策.净情绪`(样本数>0 才用);
  · 公告/政策事件方向:`sentiment.events` 中 层∈{公司行为,政策} 的
    影响方向(利好+1/利空-1/中性0)× 影响强度(1~4)/4,取均值。
  · 根源净情绪 = 上述可用分量的等权平均(全缺 → 该 record 无根源情绪,剔除)。
  · 排除:舆情层、新闻层(二手放大,当噪声)。
  · top-level 公告 events 的 impact 多为"待判"(无可靠方向)→ 不计入数值分,
    仅在 A' 里作"有无根源消息"用。

无未来函数(红线,同研究 C):情绪只用信号日 d 当日已知;前瞻收益 close[d+N]/close[d]-1
仅作标签,不参与选样。

指标复用 backtest_rank 的 IC/分层 + backtest_sentiment 的校准(正/负情绪二分)。
⚠️ 数据现实:根源情绪比全层更稀疏(政策/公告事件只在少数 record),
   **有效横截面极小、统计力≈0**,只出方向性;脚本如实打印样本量。

用法:python -m tools.backtest.sentiment_root [--horizon 1,2,3,5,10] [--json out.json]
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
from tools.backtest.backtest_rank import ic_metrics, decile_metrics
from tools.backtest.backtest_sentiment import calibration

logger = logging.getLogger("backtest.sentiment_root")

_DISCLAIMER = "历史回测≠未来保证,非投资建议。样本极短,仅方向性。"
ROOT_LAYERS = {"公司行为", "政策"}
_DIR = {"利好": 1.0, "利空": -1.0, "中性": 0.0}


def _root_sentiment(rec: dict):
    """从 record 抽根源净情绪 ∈[-1,1] 或 None(无根源信号)。"""
    senti = rec.get("sentiment") or {}
    parts = []
    # 政策层净情绪
    pol = (senti.get("三层") or {}).get("政策") or {}
    if (pol.get("样本数") or 0) > 0 and pol.get("净情绪") is not None:
        try:
            parts.append(float(pol["净情绪"]))
        except (TypeError, ValueError):
            pass
    # 根源层事件方向×强度
    ev_scores = []
    for e in (senti.get("events") or []):
        if isinstance(e, dict) and e.get("层") in ROOT_LAYERS:
            d = _DIR.get(e.get("影响方向"))
            if d is None:
                continue
            try:
                stg = float(e.get("影响强度") or 0)
            except (TypeError, ValueError):
                stg = 0.0
            ev_scores.append(d * min(stg, 4.0) / 4.0)
    if ev_scores:
        parts.append(float(np.mean(ev_scores)))
    if not parts:
        return None
    return float(np.mean(parts))


def build_root_sentiment_panel(dates=None, horizons=(1, 2, 3, 5, 10)) -> pd.DataFrame:
    """逐日读根源净情绪,配前瞻 N 日收益;仅保留有根源情绪的 record。"""
    if dates is None:
        dates = store.list_dates()
    kcache: dict[str, pd.DataFrame | None] = {}

    def _kline(code):
        if code not in kcache:
            try:
                kcache[code] = market.load_kline(code).reset_index(drop=True)
            except Exception:
                kcache[code] = None
        return kcache[code]

    rows, diag = [], {}
    for d in dates:
        n_rec = n_root = 0
        for rec in store.iter_records(date=d):
            n_rec += 1
            code = (rec.get("meta") or {}).get("code")
            score = _root_sentiment(rec)
            if code is None or score is None:
                continue
            n_root += 1
            df = _kline(str(code))
            if df is None or "date" not in df.columns or "close" not in df.columns:
                continue
            kd = [str(x)[:10] for x in df["date"].tolist()]
            if d not in kd:
                continue
            idx = kd.index(d)
            close = df["close"].to_numpy(float)
            row = {"date": d, "code": str(code), "score": score}
            for N in horizons:
                row[f"r_{N}"] = (float(close[idx + N] / close[idx] - 1.0) * 100.0
                                 if idx + N < len(close) and close[idx] > 0 else np.nan)
            rows.append(row)
        diag[d] = {"记录数": n_rec, "有根源情绪": n_root}
    panel = pd.DataFrame(rows)
    panel.attrs["diag"] = diag
    return panel


def run(dates=None, horizons=(1, 2, 3, 5, 10), json_path=None, min_cross=5):
    panel = build_root_sentiment_panel(dates, horizons)
    diag = panel.attrs.get("diag", {})
    print(f"\n===== 研究 C' · 根源净情绪 IC(政策层+公告事件)=====")
    print(f"(无未来函数;{_DISCLAIMER})\n")
    print("—— 覆盖诊断(每日)——")
    for d, dd in diag.items():
        print(f"  {d}: " + "  ".join(f"{k}={v}" for k, v in dd.items()))
    print()
    if panel.empty:
        print("!! panel 为空:当前窗口几乎无根源情绪(政策/公告事件极稀疏)。")
        res = {"错误": "panel 为空", "诊断": diag, "免责": _DISCLAIMER}
        if json_path:
            from pathlib import Path
            Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        return res

    res = {"总观测行": int(len(panel)), "交易日数": int(panel["date"].nunique()),
           "min_cross": min_cross, "诊断": diag, "免责": _DISCLAIMER}
    for N in horizons:
        col = f"r_{N}"
        valid = panel.dropna(subset=[col])
        n_obs = int(len(valid))
        print(f"—— 前瞻 {N} 交易日 ——  (有效观测={n_obs}, 覆盖交易日={int(valid['date'].nunique()) if n_obs else 0})")
        if n_obs == 0:
            print("   无前瞻收益可算。\n")
            res[f"{N}日"] = {"有效观测": 0, "说明": "前瞻数据不足"}
            continue
        ic = ic_metrics(valid, N, min_cross=min_cross)
        dec = decile_metrics(valid, N, min_cross=min_cross)
        cal = calibration(valid, N)
        res[f"{N}日"] = {"有效观测": n_obs, "IC": ic, "分层": dec, "校准": cal}
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
    ap.add_argument("--dates", default="")
    ap.add_argument("--horizon", default="1,2,3,5,10")
    ap.add_argument("--min-cross", type=int, default=5)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    dates = [x for x in a.dates.split(",") if x] or None
    run(dates=dates, horizons=tuple(int(x) for x in a.horizon.split(",")),
        json_path=a.json or None, min_cross=a.min_cross)
