"""D2 · 金字塔骨架回测底座（§5.1，轮 1 交付）。

逐日 `build_skeleton`（**只读复用**，不改排序逻辑）→ top15/top30 → `d3_score.forward_return`
D+1/D+3/D+5 → 等权收益 / 命中率 / 对全A等权 α；再算**子分 vs forward 收益的 rank IC**（Spearman）
给调权提供证据。输出 `data/analysis/backtest/金字塔骨架回测_<口径>_<区间>.json` + md 摘要。

## 口径与诚实边界
  · **入场口径** `entry_rule`（A8）：默认 "close"（收盘即入·恒成交·全票可比，rank IC 不受撮合噪声干扰）；
    "limit"（回踩限价撮合，含踏空）另跑一版对照。
  · **基准/α**：全A等权 market_ew（沪深300 缺 09-18 时的同源基准，见 d3_score.A13）。
    等权组合 α = 组合等权收益 − 全A等权同期收益。
  · **窗口未满**：D+K 超出 score_asof 的票记 insufficient、剔出该窗分母（防未来铁律）；
    临近区间末端的 D0 自然可记分票变少，不补造。
  · **rank IC** 覆盖**全计分池**（非仅 topN），提高统计功效；filled=False 的踏空票不进 IC 分母。

## 门槛（临时·复盘调，见规划 §5.1）
  候选子分 rank IC 方向正确且 ≥3/5 日；加入后 top15 D+1 等权收益不劣于基线且 α 不降；否则不进权重。

⚠️ 测试环境研究用，非投资建议。
"""
from __future__ import annotations

import json
import os
from typing import Optional

from tools.pyramid._common import data_root, load_kline
from tools.pyramid import d3_score as D3
from tools.pyramid.entry_rule import DEFAULT_ENTRY_RULE, normalize_rule

_WINDOWS_DEFAULT = (1, 3, 5)
_TOPN_DEFAULT = (15, 30)
# rank IC 评估的维度：骨架分 + 五子分（子分名与 d2_compose.compose_one 一致）
_IC_DIMS = ("骨架分", "量价自证", "板块角色", "策略共识", "排雷", "宏观催化")


# ── 交易日历（从 market_ew / 沪深300 取真实交易日，不臆造）─────────────
def _trading_days(start: str, end: str, root: Optional[str]) -> list:
    """区间内真实交易日（升序）。优先 market_ew.parquet 的日期，回退沪深300 指数。"""
    ew = D3._load_ew_series(root)
    if ew:
        days = sorted(d for d in ew if start <= d <= end)
        if days:
            return days
    bdf = D3._load_bench_df(root)
    if bdf is not None and "date" in bdf.columns:
        ds = sorted({str(x)[:10] for x in bdf["date"].tolist()})
        return [d for d in ds if start <= d <= end]
    return []


# ── Spearman rank IC（无 scipy 依赖·平均秩处理并列）────────────────────
def _avg_ranks(xs: list) -> list:
    """升序平均秩（并列取平均），返回与输入同序的秩列表。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0            # 1-based 平均秩
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _spearman(xs: list, ys: list) -> Optional[float]:
    """Spearman 相关（=秩上的 Pearson）。样本<3 或任一侧零方差 → None（无声不编）。"""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return round(sxy / (sxx * syy) ** 0.5, 4)


# ── forward 收益（每票 K线只读一次·全窗复用）──────────────────────────
def _forward_by_code(codes: list, d0: str, windows, score_asof: str,
                     root: Optional[str], entry_rule: str) -> dict:
    """{code: {k: forward_return_dict}}。每票 load_kline 一次、按窗复用（省 IO）。"""
    out: dict = {}
    for c in codes:
        df = load_kline(c, score_asof, root=root)
        out[c] = {k: D3.forward_return(c, d0, k, score_asof, root=root,
                                       entry_rule=entry_rule, df=df) for k in windows}
    return out


def _filled_ret(fr: dict) -> Optional[float]:
    """成交且窗口已满 → ret_pct；踏空/未满/缺 → None。"""
    if fr.get("filled") and "ret_pct" in fr:
        return fr["ret_pct"]
    return None


# ── 单日回测 ──────────────────────────────────────────────────────────
def run_day(as_of: str, score_asof: str, root: Optional[str], entry_rule: str,
            windows=_WINDOWS_DEFAULT, topns=_TOPN_DEFAULT,
            scan_kline: bool = True, limit: Optional[int] = None) -> dict:
    """一个决策日 D0=as_of 的骨架回测：topN 组合指标 + 全池 rank IC。"""
    from tools.pyramid.d2_compose import build_skeleton   # 只读调用（不改其逻辑）
    sk = build_skeleton(as_of, root=root, scan_kline=scan_kline, limit=limit)
    ranked = sk["排序"]
    codes = [r.code for r in ranked]
    fwd = _forward_by_code(codes, as_of, windows, score_asof, root, entry_rule)

    # topN 组合指标（等权收益 / 命中率 / α vs 全A等权 / 踏空数）
    top_metrics: dict = {}
    for N in topns:
        topN = ranked[:N]
        per_win: dict = {}
        for k in windows:
            rets, 踏空 = [], 0
            for r in topN:
                fr = fwd[r.code][k]
                v = _filled_ret(fr)
                if v is not None:
                    rets.append(v)
                elif fr.get("filled") is False:
                    踏空 += 1
            mkt = D3.market_ew_return(as_of, k, score_asof, root=root)
            if rets:
                avg = sum(rets) / len(rets)
                per_win[k] = {
                    "n": len(rets),
                    "命中率": round(sum(1 for x in rets if x > 0) / len(rets), 3),
                    "等权收益": round(avg, 3),
                    "市场等权": mkt,
                    "α": round(avg - mkt, 3) if mkt is not None else None,
                    "踏空": 踏空,
                }
            else:
                per_win[k] = {"n": 0, "踏空": 踏空, "市场等权": mkt}
        top_metrics[N] = per_win

    # 全池 rank IC：各维度子分 vs forward 收益（Spearman）
    rank_ic: dict = {}
    for k in windows:
        dim_ic: dict = {}
        pairs_n = 0
        for dim in _IC_DIMS:
            xs, ys = [], []
            for r in ranked:
                v = _filled_ret(fwd[r.code][k])
                if v is None:
                    continue
                score = r.骨架分 if dim == "骨架分" else r.子分.get(dim)
                if not isinstance(score, (int, float)):
                    continue
                xs.append(float(score))
                ys.append(v)
            dim_ic[dim] = _spearman(xs, ys)
            pairs_n = max(pairs_n, len(xs))
        rank_ic[k] = {"n": pairs_n, "ic": dim_ic}

    return {
        "as_of": as_of, "score_asof": score_asof,
        "池规模": sk["池规模"], "计分票数": sk["计分票数"],
        "排雷否决数": len(sk.get("排雷否决", [])),
        "top_metrics": {str(N): v for N, v in top_metrics.items()},
        "rank_ic": {str(k): v for k, v in rank_ic.items()},
    }


# ── 区间回测 + 汇总 ───────────────────────────────────────────────────
def run_backtest(start: str, end: str, root: Optional[str] = None,
                 entry_rule: str = DEFAULT_ENTRY_RULE,
                 score_asof: Optional[str] = None,
                 windows=_WINDOWS_DEFAULT, topns=_TOPN_DEFAULT,
                 scan_kline: bool = True, limit: Optional[int] = None) -> dict:
    er = normalize_rule(entry_rule)
    days = _trading_days(start, end, root)
    sa = score_asof or end
    day_results = [run_day(d, sa, root, er, windows, topns, scan_kline, limit)
                   for d in days]
    return {
        "口径": er, "区间": f"{start}~{end}", "score_asof": sa,
        "entry_rule": er, "windows": list(windows), "topns": list(topns),
        "trading_days": days, "days": day_results,
        "汇总": _summarize(day_results, windows, topns),
    }


def _summarize(days: list, windows, topns) -> dict:
    """跨日汇总：topN 各窗均值指标 + rank IC「方向正确且 ≥N/总」计数（调权门槛证据）。"""
    def _mean(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(sum(vals) / len(vals), 3) if vals else None

    top_sum: dict = {}
    for N in topns:
        per_win: dict = {}
        for k in windows:
            all_cells = [d["top_metrics"].get(str(N), {}).get(k) or {} for d in days]
            cells = [c for c in all_cells if c.get("n")]
            per_win[str(k)] = {
                "有效日数": len(cells),
                "均等权收益": _mean([c["等权收益"] for c in cells]),
                "均命中率": _mean([c["命中率"] for c in cells]),
                "均α": _mean([c.get("α") for c in cells]),
                "总踏空": sum(c.get("踏空", 0) for c in all_cells),
            }
        top_sum[str(N)] = per_win

    ic_sum: dict = {}
    for k in windows:
        dim_stat: dict = {}
        for dim in _IC_DIMS:
            ics = [d["rank_ic"].get(str(k), {}).get("ic", {}).get(dim) for d in days]
            ics = [x for x in ics if isinstance(x, (int, float))]
            正 = sum(1 for x in ics if x > 0)
            负 = sum(1 for x in ics if x < 0)
            dim_stat[dim] = {
                "均IC": _mean(ics), "有效日": len(ics),
                "正向日": 正, "负向日": 负,
                "方向": ("正" if 正 > 负 else "负" if 负 > 正 else "混"),
            }
        ic_sum[str(k)] = dim_stat
    return {"top_metrics": top_sum, "rank_ic": ic_sum}


# ── 渲染 md 摘要 ──────────────────────────────────────────────────────
def render_md(res: dict) -> str:
    L = [f"# 金字塔骨架回测 · 口径={res['口径']}(entry_rule={res['entry_rule']}) · 区间={res['区间']}",
         f"记分执行={res['score_asof']}　交易日={res['trading_days']}　窗口=D+{res['windows']}",
         "",
         "## 组合指标汇总（跨日均值）"]
    for N in res["topns"]:
        L.append(f"### top{N}")
        for k in res["windows"]:
            s = res["汇总"]["top_metrics"].get(str(N), {}).get(str(k), {})
            L.append(f"- D+{k}: 有效{s.get('有效日数')}日 "
                     f"均收益={s.get('均等权收益')}% 命中率={s.get('均命中率')} α={s.get('均α')}%")
    L.append("")
    L.append("## rank IC 汇总（子分 vs forward 收益·Spearman·全池）")
    for k in res["windows"]:
        L.append(f"### D+{k}")
        for dim, st in res["汇总"]["rank_ic"].get(str(k), {}).items():
            L.append(f"- {dim}: 均IC={st['均IC']} 方向={st['方向']} "
                     f"(正{st['正向日']}/负{st['负向日']}/有效{st['有效日']}日)")
    L.append("")
    L.append("## 逐日明细")
    for d in res["days"]:
        L.append(f"### {d['as_of']}（池{d['池规模']}·计分{d['计分票数']}·否决{d['排雷否决数']}）")
        for N in res["topns"]:
            segs = []
            for k in res["windows"]:
                c = d["top_metrics"].get(str(N), {}).get(k) or d["top_metrics"].get(str(N), {}).get(str(k)) or {}
                if c.get("n"):
                    segs.append(f"D+{k}:{c['等权收益']}%/命中{c['命中率']}/α{c.get('α')}")
                else:
                    segs.append(f"D+{k}:无满窗(踏空{c.get('踏空',0)})")
            L.append(f"- top{N}: " + "　".join(segs))
    return "\n".join(L)


# ── 落盘 ──────────────────────────────────────────────────────────────
def save(res: dict, root: Optional[str] = None) -> dict:
    out_dir = os.path.join(data_root(root), "data", "analysis", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    stem = f"金字塔骨架回测_{res['口径']}_{res['区间']}"
    jpath = os.path.join(out_dir, stem + ".json")
    mpath = os.path.join(out_dir, stem + ".md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    with open(mpath, "w", encoding="utf-8") as f:
        f.write(render_md(res))
    return {"json": jpath, "md": mpath}


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="金字塔骨架回测底座（D2）")
    ap.add_argument("--start", required=True, help="区间起(D0 最早)")
    ap.add_argument("--end", required=True, help="区间止(D0 最晚)")
    ap.add_argument("--score-asof", default=None, help="记分执行日（数据现到哪天）；缺省=end")
    ap.add_argument("--entry-rule", default=DEFAULT_ENTRY_RULE, help="入场撮合口径 close|limit")
    ap.add_argument("--windows", default="1,3,5")
    ap.add_argument("--topns", default="15,30")
    ap.add_argument("--limit", type=int, default=None, help="只算池前 N 票（快跑/冒烟用）")
    ap.add_argument("--no-scan-kline", action="store_true")
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    ws = tuple(int(x) for x in a.windows.split(",") if x.strip())
    tn = tuple(int(x) for x in a.topns.split(",") if x.strip())
    res = run_backtest(a.start, a.end, a.data_root, a.entry_rule, a.score_asof,
                       ws, tn, scan_kline=not a.no_scan_kline, limit=a.limit)
    paths = save(res, a.data_root)
    print(render_md(res))
    print(f"\n[落盘] {paths['json']}\n         {paths['md']}")


if __name__ == "__main__":
    _cli()
