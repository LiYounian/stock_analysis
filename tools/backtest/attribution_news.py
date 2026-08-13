"""研究 A(归因)+ B(事件分类)· 失败信号 ∩ 消息面。

命题:纯量价信号失灵时,有多少伴随消息面(新闻/公告)?这些消息属"可提前捕获"的
预定型,还是"事后才知"的突发型?

数据源(关键选择):**用已落盘 record 里存的 events**,不打 news/announcement 网络接口。
理由:① 现网新闻 API 历史窗口只有天~周,回溯补不了历史信号日;② 本项目每日闭环已把
当日采到的新闻/公告落进 record(top-level `events` = 公告类,`sentiment.events` = 新闻/舆情类),
时间戳≈信号日当日 → 正好是"信号日附近有无消息"的现成快照。故归因窗口 = 有 record 的
那几天(约 08-06~08-13),这也是数据能支持的唯一窗口(方案 §2 已预告)。

信号与结果口径(无未来函数):
  · 信号样本 = 该窗口内的 record;"偏多信号" = `signals.trend.评级 == '偏多'`(信号日收盘已知)。
  · 失败 = 前瞻 N 日收益 < 0(close[t+N]/close[t]-1;t=record 日,仅作结果标签)。
  · "有消息" = record 的 events(两源合并)非空。

研究 A 产出:失败样本"有消息"占比  vs  全样本基准率  vs  成功样本占比。
研究 B 产出:失败∩命中的消息按 预定型(业绩预告/快报/财报/解禁/分红,有日历可提前)
            vs 突发型(重组/监管/龙虎榜/停复牌/传闻/突发)粗分,给两类占比;
            领先时间 = 信号日 − 事件日,数据够才给分布,否则标"无法测·待累积"。

⚠️ 样本极短(仅数百 record 观测、前瞻收益更受窗口限制),结论只能方向性。

用法:python -m tools.backtest.attribution_news [--horizon 5] [--json out.json]
非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.attribution")

_DISCLAIMER = "历史回测≠未来保证,非投资建议。样本极短,仅方向性。"

# 事件分类关键词(粗分:命中预定型优先,再突发型,都不中→其他)
_PREDICTABLE_KW = ["业绩预告", "预增", "预减", "预盈", "预亏", "业绩快报", "快报",
                   "年报", "半年报", "季报", "财报", "年度报告", "解禁", "限售",
                   "分红", "派息", "分配", "股东大会", "定增", "配股", "增发"]
_SURPRISE_KW = ["重组", "并购", "收购", "重大资产", "监管", "问询", "处罚", "立案",
                "调查", "龙虎榜", "停牌", "复牌", "减持", "增持", "担保", "诉讼",
                "仲裁", "传闻", "澄清", "中标", "签约", "合作", "违规", "冻结",
                "质押", "举牌", "易主", "控制权"]


def _classify_event(text: str, etype: str | None) -> str:
    """预定型 / 突发型 / 其他。text=标题+摘要,etype=事件类型字段。"""
    hay = f"{text or ''} {etype or ''}"
    for kw in _PREDICTABLE_KW:
        if kw in hay:
            return "预定型"
    for kw in _SURPRISE_KW:
        if kw in hay:
            return "突发型"
    # 事件类型字段兜底
    if etype in ("业绩",):
        return "预定型"
    if etype in ("市场传闻", "重组", "监管"):
        return "突发型"
    return "其他"


def _record_events(rec: dict) -> list[dict]:
    """合并 record 两源事件为统一结构 [{type,title,time,source,layer}]。"""
    out = []
    for e in (rec.get("events") or []):           # 公告类:date/type/impact/title
        if isinstance(e, dict):
            out.append({"type": e.get("type"), "title": e.get("title") or "",
                        "summary": "", "time": e.get("date"), "source": "公告",
                        "layer": "公司行为"})
    for e in ((rec.get("sentiment") or {}).get("events") or []):  # 新闻/舆情
        if isinstance(e, dict):
            out.append({"type": e.get("事件类型"), "title": e.get("标题") or "",
                        "summary": e.get("摘要") or "", "time": e.get("time"),
                        "source": e.get("source"), "layer": e.get("层")})
    return out


def build_attr_panel(dates=None, horizons=(1, 5)) -> pd.DataFrame:
    """逐 record 落一行:date/code/trend/has_events/n_events/events(list)/r_N。无未来函数。"""
    if dates is None:
        dates = store.list_dates()
    maxN = max(horizons)
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
            code = (rec.get("meta") or {}).get("code")
            if not code:
                continue
            trend = ((rec.get("signals") or {}).get("trend") or {}).get("评级")
            evs = _record_events(rec)
            row = {"date": d, "code": str(code), "trend": trend,
                   "has_events": bool(evs), "n_events": len(evs), "events": evs}
            df = _kline(str(code))
            for N in horizons:
                row[f"r_{N}"] = np.nan
            if df is not None and "date" in df.columns:
                kdates = [str(x)[:10] for x in df["date"].tolist()]
                if d in kdates:
                    idx = kdates.index(d)
                    close = df["close"].to_numpy(float)
                    for N in horizons:
                        if idx + N < len(close) and close[idx] > 0:
                            row[f"r_{N}"] = float(close[idx + N] / close[idx] - 1.0) * 100.0
            rows.append(row)
    return pd.DataFrame(rows)


def _rate(mask_num: pd.Series, mask_den: pd.Series) -> tuple[int, int, float | None]:
    den = int(mask_den.sum())
    num = int((mask_num & mask_den).sum())
    return num, den, (round(num / den * 100, 1) if den else None)


def research_A(panel: pd.DataFrame, N: int, only_bullish: bool) -> dict:
    """失败∩有消息 占比 vs 基准。only_bullish=True 时仅取 trend=='偏多'。"""
    col = f"r_{N}"
    sub = panel.dropna(subset=[col]).copy()
    if only_bullish:
        sub = sub[sub["trend"] == "偏多"]
    n = len(sub)
    if n == 0:
        return {"样本量": 0, "说明": "无可用前瞻收益样本"}
    has = sub["has_events"]
    failed = sub[col] < 0
    success = sub[col] >= 0
    base_num, base_den, base = _rate(has, pd.Series(True, index=sub.index))
    fail_num, fail_den, fail = _rate(has, failed)
    succ_num, succ_den, succ = _rate(has, success)
    return {"样本量": int(n), "仅偏多信号": only_bullish,
            "全样本有消息率%": base, "(有消息/总)": f"{base_num}/{base_den}",
            "失败样本数": int(fail_den), "失败∩有消息%": fail, "(失败有消息/失败)": f"{fail_num}/{fail_den}",
            "成功样本数": int(succ_den), "成功∩有消息%": succ, "(成功有消息/成功)": f"{succ_num}/{succ_den}",
            "失败vs全样本差(pp)": (round(fail - base, 1) if (fail is not None and base is not None) else None)}


def research_B(panel: pd.DataFrame, N: int, only_bullish: bool) -> dict:
    """失败样本命中的消息按 预定/突发/其他 分类 + 领先时间。"""
    col = f"r_{N}"
    sub = panel.dropna(subset=[col]).copy()
    if only_bullish:
        sub = sub[sub["trend"] == "偏多"]
    failed = sub[sub[col] < 0]
    cls = Counter()
    lead_days = []
    n_events = 0
    for _, r in failed.iterrows():
        sig_day = pd.Timestamp(r["date"])
        for e in r["events"]:
            n_events += 1
            cls[_classify_event(f"{e['title']} {e['summary']}", e["type"])] += 1
            t = e.get("time")
            if t:
                try:
                    ev_day = pd.Timestamp(str(t)[:10])
                    lead_days.append((sig_day - ev_day).days)  # >0 = 消息早于信号日
                except Exception:
                    pass
    total = sum(cls.values())
    dist = {k: {"n": v, "占比%": round(v / total * 100, 1)} for k, v in cls.most_common()} if total else {}
    lead_info: dict
    if len(lead_days) >= 5:
        arr = np.array(lead_days)
        lead_info = {"n": len(arr), "领先天数均值": round(float(arr.mean()), 2),
                     "中位": float(np.median(arr)), "早于信号日占比%": round(float((arr > 0).mean()) * 100, 1),
                     "同日占比%": round(float((arr == 0).mean()) * 100, 1)}
    else:
        lead_info = {"n": len(lead_days), "说明": "领先时间样本<5,无法测·待累积(消息时间戳≈信号日当日采集)"}
    return {"失败样本数": int(len(failed)), "失败样本命中消息条数": n_events,
            "事件分类分布": dist, "领先时间": lead_info}


def run(dates=None, horizons=(1, 5), json_path=None):
    panel = build_attr_panel(dates, horizons)
    res = {"总record观测": int(len(panel)), "免责": _DISCLAIMER}
    print(f"\n===== 研究 A(归因)+ B(事件分类)· 失败信号 ∩ 消息 =====")
    print(f"(消息源=已落盘 record events,非网络;无未来函数;{_DISCLAIMER})\n")
    if panel.empty:
        print("!! panel 为空"); return res

    # 覆盖诊断
    print("—— 覆盖 ——")
    print(f"  record 总数={len(panel)}  有events={int(panel['has_events'].sum())} "
          f"({round(panel['has_events'].mean()*100,1)}%)  偏多信号={int((panel['trend']=='偏多').sum())}")
    for N in horizons:
        print(f"  有 r_{N} 前瞻收益的 record 数 = {int(panel[f'r_{N}'].notna().sum())}")
    print()

    for N in horizons:
        res[f"{N}日"] = {}
        for only_bull in (False, True):
            tag = "偏多信号" if only_bull else "全record"
            a = research_A(panel, N, only_bull)
            b = research_B(panel, N, only_bull)
            res[f"{N}日"][tag] = {"研究A": a, "研究B": b}
            print(f"—— 前瞻 {N}日 · {tag} ——")
            if a.get("样本量", 0) == 0:
                print("   无样本\n"); continue
            print(f"  [A] 样本={a['样本量']}  全样本有消息率={a['全样本有消息率%']}%  "
                  f"失败∩有消息={a['失败∩有消息%']}% {a['(失败有消息/失败)']}  "
                  f"成功∩有消息={a['成功∩有消息%']}% {a['(成功有消息/成功)']}  "
                  f"失败vs全样本={a['失败vs全样本差(pp)']}pp")
            print(f"  [B] 失败命中消息 {b['失败样本命中消息条数']} 条 → 分类 " +
                  "  ".join(f"{k}:{v['n']}({v['占比%']}%)" for k, v in b["事件分类分布"].items()))
            print(f"      领先时间: {b['领先时间']}")
            print()

    if json_path:
        from pathlib import Path
        # events list 太大,落盘时剔除
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default="")
    ap.add_argument("--horizon", default="1,5")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    dates = [x for x in a.dates.split(",") if x] or None
    run(dates=dates, horizons=tuple(int(x) for x in a.horizon.split(",")), json_path=a.json or None)
