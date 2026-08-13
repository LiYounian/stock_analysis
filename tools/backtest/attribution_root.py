"""研究 A' · 根源消息口径归因(公告 + 政策,弃舆情/新闻)。

背景:上一轮研究 A 用"任意 events(含舆情/新闻)"→"有消息率"被泛舆情顶到 ~85%,
"有无消息"沦为噪声、无区分度。用户洞察:信号只应取**根源消息 = 公司公告 + 国家政策**,
新闻转载/舆情大V 是二手放大,当噪声丢。

本模块复用研究 A 的面板(`attribution_news.build_attr_panel`,已把每 record 的
top-level 公告 events 与 sentiment.events 合并、带 `layer` 字段),
把"有消息"从"任意 events"**收紧到根源层**:
  · 根源层 = {"公司行为"(=公告), "政策"};排除 {"舆情", "新闻", None}。
    (build_attr_panel 把 top-level 公告 events 打标 layer="公司行为";
     sentiment.events 保留自身 层 字段。)

对照维度(无未来函数,口径同研究 A:信号日收盘已知,前瞻收益仅作标签):
  · 全样本"根源消息率" vs 旧"任意消息率"(看收窄幅度)。
  · 失败样本(前瞻<0)∩根源 vs 成功样本∩根源 vs 全样本基准 → 看是否**出现区分度**。

⚠️ 样本极短(约 08-06~08-13、数百 record),结论只能方向性。
用法:python -m tools.backtest.attribution_root [--horizon 1,5] [--json out.json]
非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging

import pandas as pd

from tools.backtest.attribution_news import build_attr_panel

logger = logging.getLogger("backtest.attribution_root")

_DISCLAIMER = "历史回测≠未来保证,非投资建议。样本极短,仅方向性。"
ROOT_LAYERS = {"公司行为", "政策"}          # 根源:公告 + 政策
NOISE_LAYERS = {"舆情", "新闻"}             # 衍生:弃/降权


def _root_events(evs: list[dict]) -> list[dict]:
    return [e for e in (evs or []) if e.get("layer") in ROOT_LAYERS]


def _add_root_flags(panel: pd.DataFrame) -> pd.DataFrame:
    panel = panel.copy()
    panel["has_root"] = panel["events"].map(lambda evs: len(_root_events(evs)) > 0)
    panel["n_root"] = panel["events"].map(lambda evs: len(_root_events(evs)))
    # 分层诊断:各层命中(record 级)
    for lay in ("公司行为", "政策", "舆情", "新闻"):
        panel[f"has_{lay}"] = panel["events"].map(
            lambda evs, L=lay: any(e.get("layer") == L for e in (evs or [])))
    return panel


def _rate(num_mask: pd.Series, den_mask: pd.Series):
    den = int(den_mask.sum())
    num = int((num_mask & den_mask).sum())
    return num, den, (round(num / den * 100, 1) if den else None)


def research_A_root(panel: pd.DataFrame, N: int, only_bullish: bool) -> dict:
    col = f"r_{N}"
    sub = panel.dropna(subset=[col]).copy()
    if only_bullish:
        sub = sub[sub["trend"] == "偏多"]
    n = len(sub)
    if n == 0:
        return {"样本量": 0, "说明": "无可用前瞻收益样本"}
    allmask = pd.Series(True, index=sub.index)
    failed = sub[col] < 0
    success = sub[col] >= 0

    # 旧口径(任意消息)对照
    _, _, any_base = _rate(sub["has_events"], allmask)
    _, _, any_fail = _rate(sub["has_events"], failed)
    # 根源口径
    root_bn, root_bd, root_base = _rate(sub["has_root"], allmask)
    root_fn, root_fd, root_fail = _rate(sub["has_root"], failed)
    root_sn, root_sd, root_succ = _rate(sub["has_root"], success)
    # 分层命中率(全样本)
    layer_rates = {lay: _rate(sub[f"has_{lay}"], allmask)[2]
                   for lay in ("公司行为", "政策", "舆情", "新闻")}

    return {
        "样本量": int(n), "仅偏多信号": only_bullish,
        "旧_任意消息率%": any_base, "旧_失败∩任意%": any_fail,
        "根源消息率%(公告+政策)": root_base, "(根源/总)": f"{root_bn}/{root_bd}",
        "失败∩根源%": root_fail, "(失败根源/失败)": f"{root_fn}/{root_fd}",
        "成功∩根源%": root_succ, "(成功根源/成功)": f"{root_sn}/{root_sd}",
        "失败vs成功区分度(pp)": (round(root_fail - root_succ, 1)
                                if (root_fail is not None and root_succ is not None) else None),
        "失败vs全样本(pp)": (round(root_fail - root_base, 1)
                            if (root_fail is not None and root_base is not None) else None),
        "分层命中率%": layer_rates,
    }


def run(dates=None, horizons=(1, 5), json_path=None):
    panel = build_attr_panel(dates, horizons)
    print(f"\n===== 研究 A' · 根源消息口径归因(公告+政策,弃舆情/新闻)=====")
    print(f"(消息源=已落盘 record events;无未来函数;{_DISCLAIMER})\n")
    if panel.empty:
        print("!! panel 为空")
        return {"错误": "panel 为空", "免责": _DISCLAIMER}
    panel = _add_root_flags(panel)

    print("—— 覆盖 ——")
    print(f"  record 总数={len(panel)}  任意消息率={round(panel['has_events'].mean()*100,1)}%  "
          f"根源(公告+政策)率={round(panel['has_root'].mean()*100,1)}%")
    print(f"  分层命中: " + "  ".join(
        f"{lay}={round(panel[f'has_{lay}'].mean()*100,1)}%" for lay in ("公司行为", "政策", "舆情", "新闻")))
    print()

    res = {"总record": int(len(panel)),
           "任意消息率%": round(panel["has_events"].mean() * 100, 1),
           "根源消息率%": round(panel["has_root"].mean() * 100, 1),
           "免责": _DISCLAIMER}
    for N in horizons:
        res[f"{N}日"] = {}
        for only_bull in (False, True):
            tag = "偏多信号" if only_bull else "全record"
            a = research_A_root(panel, N, only_bull)
            res[f"{N}日"][tag] = a
            print(f"—— 前瞻 {N}日 · {tag} ——")
            if a.get("样本量", 0) == 0:
                print("   无样本\n"); continue
            print(f"  样本={a['样本量']}  旧任意消息率={a['旧_任意消息率%']}%  "
                  f"根源消息率={a['根源消息率%(公告+政策)']}%")
            print(f"  失败∩根源={a['失败∩根源%']}% {a['(失败根源/失败)']}  "
                  f"成功∩根源={a['成功∩根源%']}% {a['(成功根源/成功)']}  "
                  f"→ 失败vs成功区分度={a['失败vs成功区分度(pp)']}pp")
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
    ap.add_argument("--horizon", default="1,5")
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    dates = [x for x in a.dates.split(",") if x] or None
    run(dates=dates, horizons=tuple(int(x) for x in a.horizon.split(",")), json_path=a.json or None)
