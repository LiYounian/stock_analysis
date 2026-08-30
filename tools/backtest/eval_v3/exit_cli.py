"""eval_v3.1 带离场可实现收益评测入口(replay 轨为主,长样本)。

用法:
  python -m tools.backtest.eval_v3.exit_cli [--universe-n 800] [--lookback 250] [--stride 1]
      [--out-md docs/策略成绩报告_离场v31.md] [--out-json data/analysis/backtest/eval_v31_exit.json]

流程:构建 replay 预测(5 screener + 动量4)→ 建每日选中集(危险信号 as-of)→ 逐记录跑参数网格
(止盈{5,8,10}×止跌{3,5,8}×时间止损{5,10}×危险信号{关,开})→ 三口径聚合(固定持有①/带离场②/离场增量③)
→ 按②带离场净收益排序 → 报告。产物只写 worktree 本地。非投资建议。
"""
from __future__ import annotations

import argparse
import json
import logging
import os

import pandas as pd

from tools.config import settings

from . import aggregate_exit as _ae
from . import exit_sim as _ex
from . import prices, replay_source, scoring

logger = logging.getLogger("backtest.eval_v3.exit_cli")

_DEFAULT_MD = str(settings.PROJECT_ROOT / "docs" / "策略成绩报告_离场v31.md")
_DEFAULT_JSON = str(settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "eval_v31_exit.json")


def build_replay_preds(universe_n, lookback, stride):
    """合并 5 个逐票 screener 回放 + 动量4 截面 TopK 回放为一张统一预测记录表。"""
    p1, m1 = replay_source.replay_predictions(universe_n=universe_n, lookback_days=lookback,
                                              stride=stride)
    p2, m2 = replay_source.replay_momentum_predictions(universe_n=universe_n,
                                                        lookback_days=lookback, stride=stride)
    preds = pd.concat([p1, p2], ignore_index=True) if not p2.empty else p1
    return preds, {"screener": m1, "momentum": m2}


def run(universe_n=800, lookback=250, stride=1):
    book = prices.PriceBook()
    preds, meta = build_replay_preds(universe_n, lookback, stride)
    logger.info("预测记录 %d 条(策略 %s)", len(preds),
                sorted(preds["strategy_id"].unique().tolist()) if not preds.empty else [])
    if preds.empty:
        return {}, {}, meta
    membership, pred_days = _ae.build_membership(preds)
    records = _ae.iter_matured_records(preds, book)
    logger.info("可定位记录基元 %d", len(records))
    rows = _ae.evaluate(records, membership, pred_days)
    logger.info("参数网格展开行 %d", len(rows))
    # 买入持有全市场基准(horizon=时间止损 5/10),同抽样宇宙
    pred_dates = sorted(preds["pred_date"].unique().tolist())
    uni = replay_source.sample_universe(universe_n)
    uni_ret = scoring.universe_returns(pred_dates, _ex.TIME_STOP_GRID, uni, book)
    agg = _ae.aggregate_exit(rows, uni_ret)
    return agg, uni_ret, meta


# ────────────────────── 报告渲染 ──────────────────────
def _fmt(v, dash="—"):
    return dash if v is None else v


def _combo_label(key):
    """tp8_sl5_ts10_danger0_cost0.1 → 止盈+8%/止跌−5%/时间止损10日/危险信号关/成本0.1%。"""
    import re
    m = re.match(r"tp([\d.]+)_sl([\d.]+)_ts(\d+)_danger(\d)_cost([\d.]+)", key)
    if not m:
        return key
    tp, sl, ts, dg, c = m.groups()
    return (f"止盈+{tp}%/止跌−{sl}%/时间止损{ts}日/危险信号{'开' if dg=='1' else '关'}/成本{c}%")


def render(agg, meta, generated) -> str:
    L = ["# 带离场可实现收益报告 v3.1(三口径:固定持有① / 带离场② / 离场增量③)", "",
         f"> 生成于 {generated}。replay 回放轨(长样本)。**非投资建议**;历史观测≠未来保证。", "",
         "## 〇、口径与怎么读", "",
         "> 现状 eval_v3 的收益是**固定持有**(T+1 入场、死拿到 T+h 收盘),会把『涨过又跌回』的利润算没。",
         "> 本报告升级到**带离场的可实现收益**:T+1 入场后逐日推进,按优先级择机离场——",
         "> **① 盘中止盈**:某日最高价≥入场价×(1+止盈%)→ **在触线价成交**(不是当天收盘价);",
         "> **② 盘中止跌**:某日最低价≤入场价×(1−止跌%)→ 触线价成交;",
         "> **③ 同日高摸止盈∧低摸止跌**:日K 看不出先后 → **保守假设先触止跌** + 打 `path_ambiguous` 标记(占比如实报);",
         "> **④ 危险信号离场**(可选开关):策略自身当日不再选该票 → 按当日收盘卖(纯 as-of,不作弊);",
         "> **⑤ 时间止损**:持有到第 time_stop 日仍未触 → 当日收盘卖(退出锚=idx+time_stop,与固定持有 horizon=N 对齐)。", "",
         "> **三口径**(承箱体3 教训『防好离场救烂选股』):",
         "> - **① 纯选股固定持有**:close[T+time_stop]/入场价−1(毛,现状口径);",
         "> - **② 带离场可实现净收益**:离场毛收益 − 往返成本(0.1%/0.2% 双档);",
         "> - **③ 离场增量 = ② − ①**:离场规则相对『死拿』多赚/少赚多少(**成本可约掉,与成本档无关**),"
         "纯衡量离场时机贡献——好离场救不了烂选股就在这项现原形。③正且聚类 p 小=离场时机确有正贡献。", "",
         "> **参数预注册全跑**:止盈∈{+5,+8,+10}%、止跌∈{−3,−5,−8}%、时间止损∈{5,10}日、"
         "危险信号∈{关,开}、成本∈{0.1,0.2}%。分档:**全部选中票等权**(广筛,所有策略)/ **Top5·Top10**"
         "(按 rank_score 降序,仅可排序型有连续打分者)。", "",
         "> **危险信号开关为何拆两跑**:纯技术 screener 的下一交易日多半已不满足触发条件(setup 消失),"
         "危险信号会在 T+1 立即离场、令止盈/止跌参数几乎不 binding。故『关』档留给止盈/止跌/时间止损扫参可解读,"
         "『开』档单列展示危险信号 as-of 离场的净效果,两者并报。", "",
         f"> **回放元信息**:{meta}", ""]

    # 每策略:全部档最优组合(危险信号关,成本0.1)一行 + Top-N 若有
    L += ["## 一、各策略最优参数组三口径(危险信号『关』· 成本0.1% · 按②带离场净收益降序)", "",
          "> ⚠️ 『最优参数组』为**网格内后视最优**(按②挑),含选择偏差、偏乐观;全网格 18 组×成本×开关见 JSON。"
          "判断以 ③离场增量的**聚类 CI/p**(离场时机是否稳健正贡献)与 ②超额(带离场后跑不跑赢大盘)为准,"
          "不要只看被挑出来的②点值。", "",
          "| 策略 | 分档 | 最优参数组 | 样本 | 预测日 | ①固定持有% | ②带离场净% | ③离场增量% | ③增量聚类CI% | ③增量p | ②超额vs买入持有% | ②超额p | 平均持有天 | path_ambig% |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for lvl in ("全部", "Top5", "Top10"):
        best = _ae.best_combos(agg, level=lvl, danger=0, prefer_cost=0.1)
        for sid in sorted(best):
            b = best[sid]
            c = b["cell"]
            L.append(f"| {sid} {b['策略名']} | {lvl} | {_combo_label(b['最优key'])} | "
                     f"{c['样本']} | {c['预测日数']} | {_fmt(c['①固定持有均收益%'])} | "
                     f"{_fmt(c['②带离场净收益%'])} | {_fmt(c['③离场增量%'])} | "
                     f"{_fmt(c['③增量_聚类CI%'])} | {_fmt(c['③增量_聚类p值'])} | "
                     f"{_fmt(c['②超额vs买入持有全市场%'])} | {_fmt(c['②超额_聚类p值'])} | "
                     f"{_fmt(c['平均持有天数'])} | {_fmt(c['path_ambiguous占比%'])} |")
    L.append("")

    # 危险信号『开』档对照
    L += ["## 二、危险信号『开』档对照(全部档 · 成本0.1% · 按②降序)", "",
          "> 策略自身次日不再选该票即按当日收盘离场(纯 as-of)。看离场原因分布中危险信号占比。", "",
          "| 策略 | 最优参数组 | 样本 | ①固定持有% | ②带离场净% | ③离场增量% | ③增量p | 平均持有天 | 离场原因分布 |",
          "|---|---|---|---|---|---|---|---|---|"]
    best_dg = _ae.best_combos(agg, level="全部", danger=1, prefer_cost=0.1)
    for sid in sorted(best_dg):
        b = best_dg[sid]
        c = b["cell"]
        reasons = "; ".join(f"{k}:{v}" for k, v in sorted(c["离场原因分布"].items(),
                                                          key=lambda kv: -kv[1]))
        L.append(f"| {sid} {b['策略名']} | {_combo_label(b['最优key'])} | {c['样本']} | "
                 f"{_fmt(c['①固定持有均收益%'])} | {_fmt(c['②带离场净收益%'])} | "
                 f"{_fmt(c['③离场增量%'])} | {_fmt(c['③增量_聚类p值'])} | "
                 f"{_fmt(c['平均持有天数'])} | {reasons} |")
    L.append("")

    # 自审
    L += ["## 三、自审要点", "",
          "- **① 离场价=触线价非收盘**:盘中止盈/止跌均在入场价×(1±线%)成交,非当天收盘价(单测 test_take_profit_fills_at_trigger_not_close 锁死)。",
          "- **② 同日双触保守**:高摸止盈∧低摸止跌同日 → 保守假设先触止跌 + path_ambiguous 标记,占比列于表中(如实标注日频回测边界)。",
          "- **③ 危险信号纯 as-of**:用策略自身每日选中集(≤当日),非预测日→持有(None≠剔除);关/开双跑对照。",
          "- **④ 三口径归因**:③离场增量=②−①(成本约掉),独立识别『离场时机贡献』,好离场救不了烂选股会现原形(③≤0)。",
          "- **⑤ 成本双档**:0.1%/0.2% 往返成本从②毛收益扣;③增量与成本档无关。",
          "- **⑥ 防未来函数**:离场只用入场后自身价格路径 + 策略 as-of 再选;maturity 要求 idx+time_stop<n,三口径同一样本配对。",
          "- **⑦ 单测锁离场语义**:tests/test_exit_sim.py 覆盖触线成交/双触保守/时间止损/危险信号/成本/防未来(9 项)。", "",
          "## 四、假设与未完成", "",
          "- **假设**:所有存活策略 long-only,模拟器只实现多头;direction≠+1 记不支持(当前无)。",
          "- **假设**:危险信号『选中集』取策略每日**全部选中票**(广筛 SELECT / 动量当日 TopK),"
          "非 Top-N 内部再排名;非预测日无法判断则持有。",
          "- **假设**:买入持有全市场基准=同抽样宇宙、同 horizon=time_stop 的等权固定持有(与 eval_v3 一致,指数基准未接)。",
          "- **未完成**:不可回放策略(0多专家/9最强)无历史外部快照,不进本轨;融合基线另见附录判断。", ""]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe-n", type=int, default=800)
    ap.add_argument("--lookback", type=int, default=250)
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--out-md", default=_DEFAULT_MD)
    ap.add_argument("--out-json", default=_DEFAULT_JSON)
    a = ap.parse_args(argv)

    agg, _uni, meta = run(a.universe_n, a.lookback, a.stride)
    generated = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")
    md = render(agg, meta, generated)

    os.makedirs(os.path.dirname(a.out_json) or ".", exist_ok=True)
    with open(a.out_json, "w", encoding="utf-8") as f:
        json.dump({"聚合": agg, "回放元信息": meta, "生成时间": generated},
                  f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(a.out_md) or ".", exist_ok=True)
    with open(a.out_md, "w", encoding="utf-8") as f:
        f.write(md)
    print("\n===== eval_v3.1 带离场三口径 =====")
    print(f"策略数 {len(agg)} | 覆盖 {sorted(agg.keys())}")
    print(f"→ {a.out_md}\n→ {a.out_json}")
    return agg


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
