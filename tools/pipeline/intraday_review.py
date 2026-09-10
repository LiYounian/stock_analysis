"""午盘选股复盘(当日 15:xx 对当日午盘选出的票记「下午 α」)。

权威设计:docs/计划/2026-09-10_午盘选股迭代_复盘闭环与侧重点重构_设计.md。

一句话:午盘选股 11:30 选出、当日收盘 15:05 落盘 → **午盘票当天就能验下午表现(D-0 当日闭环)**。
这是午盘选股独有、盘后 eod-review(复盘 D-1)拿不到的时段,也是它区别于盘后选股的核心价值。

## 口径(与项目统一)
- **买入 5 只是主评价对象**(午盘选股价值=买入选得准);**规避 3 只是纠偏参照**,不作独立主指标。
  切分复用 `intraday_screen.split_buy_avoid`(输出与复盘**单一真源**,不重复定义)。
- **下午涨跌%** = (当日收盘价 − 11:30 冻结价)/11:30 冻结价 × 100。11:30 冻结价读午盘节点落盘的
  快照(`intraday_screen.noon_snapshot_path`);收盘价读主档 K 线当日行。
- **下午等权基准** = 全A的「下午涨跌%」向量的等权均值,复用唯一真源
  `market_forecast.breadth.equal_weight_mean_pct`(不自算)。
- **下午 α** = 个股下午涨跌% − 下午等权基准(与盘后 α「个股涨跌 − 全A等权」同构,只是换成下午口径)。

## 防未来红线
- 收盘只取主档 date == as_of 的当日行(绝不用 as_of 之后的行);11:30 冻结价来自采集时刻冻结的快照。
- 快照与收盘都 ≤ as_of;基准与个股同口径同源。⚠️ 测试环境研究模拟,**非投资建议**。

## 产物
- `docs/每日分析/复盘/午盘_<date>.md`:买入组/规避组逐票下午 α 记分表(列名兼容 web `_alpha_col`)+
  记分小结(买入组均值 α/命中率、分离度)。**独立档,节标题避开「逐票收盘记分」**,不与盘后复盘表竞争。
- 追加一行结论到经验沉淀 inbox `经验沉淀/_待并入.md`,由盘尾 eod-review 统一并版(D8)。
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from tools.analysis.market_forecast import breadth as B
from tools.collectors import calendar as cal
from tools.collectors import market
from tools.config import settings
from tools.pipeline import intraday_screen as isr
from tools.store import repo as store

logger = logging.getLogger("pipeline.intraday_review")

REVIEW_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "复盘"
INBOX_PATH = settings.PROJECT_ROOT / "docs" / "每日分析" / "经验沉淀" / "_待并入.md"
MD_PREFIX = "午盘"                    # 产出 复盘/午盘_<date>.md(区别于盘后 复盘/<date>.md)
MIN_COVERAGE = 0.60                   # 下午基准取样率下限:低于此标降级、基准不可当全市场口径

# 收盘判定阈值(下午 α,单位 pp):买入组期望正 α。判定仅为可读标签,不改记分。
_STRONG_PP = 1.0


# ————————————————————————————————————————————————
# 取数:11:30 快照 + 当日收盘 → 下午涨跌%
# ————————————————————————————————————————————————
def load_noon_snapshot(as_of: str, *, root: Path | None = None) -> dict:
    """读午盘节点落盘的 11:30 快照 → {code: price_11:30}。缺文件 → 空 dict(上层降级)。"""
    path = isr.noon_snapshot_path(as_of, out_root=root)
    if not path.exists():
        logger.warning("午盘 11:30 快照缺失:%s(复盘无法算下午口径)", path)
        return {}
    import json
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {c: q.get("price") for c, q in (payload.get("quotes") or {}).items()
            if q.get("price") is not None}


def close_of(code: str, as_of: str, *, load_kline_fn=None) -> float | None:
    """主档 K 线里 date == as_of 的当日收盘价。防未来:只取当日行,不碰 as_of 之后的行。"""
    load_kline_fn = load_kline_fn or market.load_kline
    try:
        df = load_kline_fn(code)
    except Exception as e:                                  # noqa: BLE001 缺档/异常 → 无收盘
        logger.debug("load_kline 失败 %s:%s", code, e)
        return None
    if df is None or "date" not in getattr(df, "columns", []) or df.empty:
        return None
    ts = pd.Timestamp(as_of)
    row = df[pd.to_datetime(df["date"]) == ts]
    if row.empty:
        return None
    c = row.iloc[-1]["close"]
    return None if pd.isna(c) else float(c)                 # NaN 收盘(停牌/缺值)→ 无收盘


def afternoon_pct(price_1130: float | None, close: float | None) -> float | None:
    """下午涨跌% = (收盘 − 11:30 冻结价)/11:30 冻结价 × 100。任一缺失/NaN/冻结价≤0 → None。"""
    if price_1130 is None or close is None or pd.isna(price_1130) or pd.isna(close):
        return None
    if price_1130 <= 0:
        return None
    return (close / price_1130 - 1.0) * 100.0


def afternoon_benchmark(snapshot: dict, as_of: str, *, load_kline_fn=None,
                        min_coverage: float = MIN_COVERAGE) -> dict:
    """全A下午等权基准:对快照全A逐票算下午涨跌% → equal_weight_mean_pct(唯一真源)。

    分母 = 实际取到收盘的只数(与 breadth 一致:取不到的票不进当日 listed)。取样率不足 → degraded。
    """
    pcts: list[float] = []
    for code, price in snapshot.items():
        p = afternoon_pct(price, close_of(code, as_of, load_kline_fn=load_kline_fn))
        if p is not None:
            pcts.append(p)
    sampled = len(pcts)
    universe = len(snapshot)
    coverage = (sampled / universe) if universe else 0.0
    mean_pct = B.equal_weight_mean_pct(pcts, total=sampled) if sampled else float("nan")
    degraded = coverage < min_coverage or sampled == 0
    return {"下午等权基准": mean_pct, "样本": sampled, "全A": universe,
            "取样率": coverage, "degraded": degraded}


# ————————————————————————————————————————————————
# 逐票记分 + 组小结
# ————————————————————————————————————————————————
def _verdict(alpha: float | None, group: str) -> str:
    """收盘判定标签(可读,不改记分)。买入组期望正 α、规避组期望负 α。"""
    if alpha is None:
        return "—(无收盘/停牌)"
    if group == "买入":
        if alpha >= _STRONG_PP:
            return "✅ 强正 α"
        return "◻️ 正 α(跑赢下午等权)" if alpha > 0 else "⚠️ 负 α(跑输下午等权)"
    # 规避组
    if alpha <= -_STRONG_PP:
        return "✅ 强负 α(规避正确)"
    return "◻️ 负 α(规避正确)" if alpha < 0 else "⚠️ 正 α(错杀,规避有偏差)"


def score_group(items: list[dict], snapshot: dict, as_of: str, benchmark: float,
                group: str, *, load_kline_fn=None) -> list[dict]:
    """给一组票(买入/规避)逐票算 下午涨跌%/下午 α/判定。"""
    out: list[dict] = []
    for x in items:
        code = x.get("code")
        close = close_of(code, as_of, load_kline_fn=load_kline_fn)
        pct = afternoon_pct(snapshot.get(code), close)
        alpha = None if (pct is None or pd.isna(benchmark)) else pct - benchmark
        out.append({"code": code, "name": x.get("name", ""), "完整分": x.get("完整分"),
                    "消息面方向": x.get("消息面方向"), "下午涨跌%": pct, "下午α": alpha,
                    "判定": _verdict(alpha, group)})
    return out


def summarize(买入: list[dict], 规避: list[dict]) -> dict:
    """组小结:买入组均值 α/命中率(主指标)、规避组均值 α/命中率、分离度(辅助纠偏)。"""
    def _mean(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def _hit(rows, positive):
        vals = [r["下午α"] for r in rows if r.get("下午α") is not None]
        if not vals:
            return None
        good = sum(1 for v in vals if (v > 0) == positive)
        return round(good / len(vals), 4)

    买入均值α = _mean(买入, "下午α")
    规避均值α = _mean(规避, "下午α")
    分离度 = (round(买入均值α - 规避均值α, 4)
             if (买入均值α is not None and 规避均值α is not None) else None)
    return {"买入均值α": 买入均值α, "买入命中率": _hit(买入, True),
            "规避均值α": 规避均值α, "规避命中率": _hit(规避, False),
            "分离度": 分离度}


# ————————————————————————————————————————————————
# 渲染 + inbox
# ————————————————————————————————————————————————
def _fmt_pp(v: float | None) -> str:
    """α 格式 +X.XXpp/−X.XXpp(Unicode 负号,与盘后 _fmt_alpha 兼容,便于将来接 web)。"""
    if v is None:
        return "—"
    s = f"{abs(v):.2f}"
    return f"+{s}pp" if v >= 0 else f"−{s}pp"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{abs(v):.2f}"
    return f"+{s}%" if v >= 0 else f"−{s}%"


def _group_table(as_of: str, rows: list[dict]) -> list[str]:
    lines = [f"| 代码/名称 | {as_of} 下午涨跌% | α vs 全A等权 | 收盘判定 |", "|---|---|---|---|"]
    for r in rows:
        name = f"{r['code']} {r['name']}".strip()
        lines.append(f"| {name} | {_fmt_pct(r['下午涨跌%'])} | {_fmt_pp(r['下午α'])} | {r['判定']} |")
    if not rows:
        lines.append("| — | — | — | 无票 |")
    return lines


def render_review_md(as_of: str, 买入: list[dict], 规避: list[dict], summ: dict,
                     bench: dict, *, out_dir: Path | None = None) -> Path:
    """写 复盘/午盘_<date>.md。节标题避开「逐票收盘记分」,不与盘后复盘表竞争 web 选表。"""
    out_dir = out_dir or REVIEW_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{MD_PREFIX}_{as_of}.md"

    L: list[str] = []
    L.append(f"# 午盘选股复盘 · {as_of}(下午 α · D-0 当日闭环)")
    L.append("")
    L.append("> ⚠️ 测试环境研究模拟,**非投资建议**。防未来函数:11:30 冻结价来自采集时刻冻结的快照,"
             "收盘只取主档当日行(≤ 当日),下午基准与个股同口径同源。")
    L.append(f"> 口径:**下午涨跌% = (当日收盘 − 11:30 冻结价)/11:30 冻结价**;"
             f"**α = 个股下午涨跌% − 全A下午等权基准**。")
    deg = "(⚠️取样率不足,基准仅参考)" if bench.get("degraded") else ""
    L.append(f"> 下午等权基准 **{_fmt_pct(bench.get('下午等权基准'))}**{deg} "
             f"(样本 {bench.get('样本')}/{bench.get('全A')},取样率 {bench.get('取样率', 0):.0%})。")
    L.append("> **买入 5 只是主评价对象**(午盘选股价值=买入选得准);**规避 3 只是纠偏参照**,不作独立主指标。")
    L.append("")

    L.append("## 买入组 · 下午 α 记分(主评价对象)")
    L.append("")
    L += _group_table(as_of, 买入)
    L.append("")
    L.append("## 规避组 · 下午 α 记分(纠偏参照)")
    L.append("")
    L += _group_table(as_of, 规避)
    L.append("")

    L.append("## 记分小结")
    L.append("")
    L.append(f"- **买入组(主)**:均值 α {_fmt_pp(summ.get('买入均值α'))}、"
             f"命中率(α>0){_fmt_ratio(summ.get('买入命中率'))}。")
    L.append(f"- 规避组(纠偏):均值 α {_fmt_pp(summ.get('规避均值α'))}、"
             f"命中率(α<0){_fmt_ratio(summ.get('规避命中率'))}。")
    L.append(f"- 方向分离度(买入均值α − 规避均值α):**{_fmt_pp(summ.get('分离度'))}** "
             f"——用于暴露/纠正模型系统性偏差,非一票否决主闸门。")
    L.append("- ⚠️ 单日样本不足以判定;预注册闸门在滚动 ≥10 交易日池化样本上评(见设计 §4)。")
    L.append("")
    L.append(f"> 本节由**午盘选股复盘节点**自动生成。基准口径:全A下午等权(11:30→收盘),"
             f"复用 `breadth.equal_weight_mean_pct` 唯一真源。⚠️ 非投资建议。")

    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    logger.info("午盘复盘 md 写出 → %s(买入 %d/规避 %d)", path, len(买入), len(规避))
    return path


def _fmt_ratio(v: float | None) -> str:
    return "—" if v is None else f"{v:.0%}"


def append_experience_inbox(as_of: str, summ: dict, *, inbox: Path | None = None) -> None:
    """往经验沉淀 inbox 追加一行结论(描述性约束),由盘尾 eod-review 统一并版(D8)。

    缺文件/缺表头 → 不硬造整份 inbox(避免误覆盖),只在已有表格末尾追加一行。
    """
    inbox = inbox or INBOX_PATH
    if not inbox.exists():
        logger.warning("经验 inbox 缺失,跳过追加:%s", inbox)
        return
    entry = ("午盘选股复盘以「买入 5 只下午 α」为主评价、规避 3 只作纠偏参照;单日不判定,"
             "滚动 ≥10 交易日看买入组均值 α>0 且命中率≥55%,分离度塌陷则回查模型偏差。")
    evidence = (f"{as_of}:买入均值α {_fmt_pp(summ.get('买入均值α'))}、命中率"
                f"{_fmt_ratio(summ.get('买入命中率'))};分离度 {_fmt_pp(summ.get('分离度'))}")
    row = f"| {as_of} | {entry} | {evidence} | 午盘选股复盘节点 |\n"
    with inbox.open("a", encoding="utf-8") as f:
        f.write(row)
    logger.info("已向经验 inbox 追加午盘复盘结论行:%s", inbox)


# ————————————————————————————————————————————————
# 节点主入口
# ————————————————————————————————————————————————
def run_intraday_review(as_of: str | None = None, *, snapshot_root: Path | None = None,
                        out_dir: Path | None = None, load_kline_fn=None,
                        write_inbox: bool = True, inbox: Path | None = None,
                        view_fn=None) -> dict:
    """午盘选股复盘节点主入口(读午盘 view + 11:30 快照 + 当日收盘 → 下午 α 记分 → 产出)。

    view_fn:注入桩(测试);默认读 store view「候选池消息面确认」(午盘节点落盘的那份)。
    """
    if as_of is None:
        as_of = store.active_date() or store._today()

    snapshot = load_noon_snapshot(as_of, root=snapshot_root)

    view_fn = view_fn or (lambda: _load_view(as_of))
    view = view_fn()
    reranked = (view or {}).get("重排") or []
    买入, 规避 = isr.split_buy_avoid(reranked)

    bench = afternoon_benchmark(snapshot, as_of, load_kline_fn=load_kline_fn)
    base = bench["下午等权基准"]
    买入s = score_group(买入, snapshot, as_of, base, "买入", load_kline_fn=load_kline_fn)
    规避s = score_group(规避, snapshot, as_of, base, "规避", load_kline_fn=load_kline_fn)
    summ = summarize(买入s, 规避s)

    path = render_review_md(as_of, 买入s, 规避s, summ, bench, out_dir=out_dir)
    if write_inbox:
        append_experience_inbox(as_of, summ, inbox=inbox)

    report = {"as_of": as_of, "买入": len(买入s), "规避": len(规避s),
              "下午等权基准": base, "小结": summ, "基准": bench, "产出": str(path)}
    logger.info("午盘选股复盘完成:as_of=%s,买入均值α=%s,分离度=%s,产出=%s",
                as_of, summ.get("买入均值α"), summ.get("分离度"), path)
    return report


def _load_view(as_of: str):
    try:
        return store.get_view("候选池消息面确认", date=as_of)
    except FileNotFoundError:
        logger.warning("午盘候选 view 缺失(as_of=%s),复盘无可记分的票", as_of)
        return None


def _main(argv: list[str] | None = None) -> int:
    """CLI:python -m tools.pipeline.intraday_review [--date YYYY-MM-DD] [--force] [--no-inbox]。

    非交易日跳过退 0。--force 跳过交易日判断(联调)。
    """
    ap = argparse.ArgumentParser(description="午盘选股复盘(下午 α · D-0 当日闭环)")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD(默认今日)")
    ap.add_argument("--force", action="store_true", help="跳过交易日判断")
    ap.add_argument("--no-inbox", action="store_true", help="不向经验 inbox 追加结论行")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    as_of = args.date or store._today()
    if not args.force and not cal.is_trading_day(as_of):
        logger.info("非交易日 %s,午盘复盘跳过(退 0)", as_of)
        return 0
    rep = run_intraday_review(as_of, write_inbox=not args.no_inbox)
    logger.info("完成:%s", {k: rep[k] for k in ("as_of", "买入", "规避", "产出") if k in rep})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
