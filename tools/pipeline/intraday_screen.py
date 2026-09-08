"""午盘全A选股(午休时点对全A现挑票)。

权威设计:docs/计划/2026-09-08_午盘全A选股_设计.md(已实测可行,总耗时 ~25min)。

一句话:把「消息面三段式」搬到午休时点,让策略在「收盘历史 + 今日午盘 bar」上跑,
产出午休冻结(11:30)口径的全A午盘候选。与收盘 screenall 并存、互不影响。

## 三段式(= 复用收盘 run_screen_all + candidate_message,只换两处)
- 阶段1 · 纯数据初筛(午休冻结):拉全A gtimg 行情快照(11:30 冻结)→ **内存注入今日午盘 bar**
  → `run_screen_all(no_llm, no_fetch, skip_strategies={策略11})` 跑各数据策略出榜。
- 阶段2 · 消息面精选(仅 shortlist):`candidate_message.run_candidate_message_enrich`——各策略
  top-K∪ 取 shortlist(≤策略数×10,~50)→ 采新闻+news_ai+情绪+事件 → 可解释线性映射回灌重排。
- 产出:`docs/每日分析/选股/日内_<date>.md`(全A午盘候选 + 买入排序 + 消息面确认)。

## 核心新能力:午盘 K 线注入(本模块的命门)
现 screener 经 `market.load_kline`/`load_kline_recent` 读 `data/master/kline`(收盘历史,最新=前一日
收盘)。午盘要让策略「看到今天到 11:30 的走势」:
  · 拉全A午盘行情(gtimg 快照:今日 open/high/low/最新价/量额)。
  · 为每只票在**内存**里把「今日午盘 bar」**追加/覆盖**为当日 bar,当日未收盘 → 用**午休冻结价
    (即 11:30 价)当临时收盘**;标注「未收盘·午盘价」。
  · 做法 = 进程内 monkeypatch `market.load_kline`(`load_kline_recent` 内部走同一 module 全局,
    一并生效),包一层:原读主档 → 丢掉 ≥as_of 的行(去重/覆盖)→ append 午盘 bar。
  · **绝不写污染 `data/master/kline` 收盘档**:patch 只作用于**返回的 DataFrame 副本**,
    每次 `load_kline` 都重新 `read_parquet` 出新对象,改它不落盘、不共享(测试锁死)。

## 防未来红线
决策只用 ≤11:30 冻结信息;午盘 bar 的 close = 11:30 冻结价,date = as_of,**绝不含 as_of 之后的行**;
新闻/情绪采集层各自 as_of 锚定(复用,不放宽)。⚠️ 研究模拟,非投资建议。

## 单位口径(gtimg → master 主档)
master 主档:volume=股、amount=元、turnover/pct_chg=百分数值。gtimg:volume=手、amount=万元。
注入时换算:volume×100(手→股)、amount_wan×10000(万元→元);turnover/pct_chg 直接用(同为百分数)。
**诚实边界**:午盘 volume/amount 只累计到 11:30(约半日),量能类信号会天然偏低——量比(vol_ratio)
由源方给出、可比性更好;午盘选股以价形/趋势/横截面为主,量能类信号请结合「半日」语境读。
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import os
from pathlib import Path

import pandas as pd

from tools.collectors import calendar as cal
from tools.collectors import gtimg_quote
from tools.collectors import market
from tools.collectors import universe
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("pipeline.intraday_screen")

SLOT = "noon"                 # 午休时段
FREEZE_LABEL = "11:30 午休冻结"
# 午盘裁剪的策略(与 run_screen_all 内 label 精确一致;非alpha、占~10min,午休省成本)。
INTRADAY_SKIP_STRATEGIES: set[str] = {"策略11·指标条件化状态排序"}
# 主档 K 线列(单一真源:tools.collectors.market.load_kline 的落盘 schema)。
_MASTER_COLS = ["date", "open", "high", "low", "close", "volume", "amount", "turnover", "pct_chg"]

SELECTION_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"
# 产出文件名前缀。**刻意不用 `日内_<date>.md`**:该文件名已被既有「午盘研判(盯盘集~19只)」
# 节点(daily-stock-noon-analysis)占用,且 intraday_watch.py 从中解析盯盘候选——全A午盘选股若
# 同名写入会**覆盖污染**那条流水。故本节点默认写 `日内全A_<date>.md`(全A午盘选股,与盯盘研判并存)。
# 设计文档写的是 `日内_<date>.md`;此为规避冲突的安全默认,是否改回由统筹拍板(见回执决策点)。
MD_PREFIX = "日内全A"


# ————————————————————————————————————————————————
# 午盘 K 线注入(核心新能力)
# ————————————————————————————————————————————————
def midday_bar_row(code: str, quote: dict, as_of: str) -> pd.DataFrame | None:
    """把一只票的 gtimg 午盘快照 → 一行「今日午盘 bar」DataFrame(对齐 master 主档 schema)。

    close = 11:30 冻结价(quote['price'],午休即当日最新价);open/high/low 取当日区间,
    缺失回落 close。volume 手→股(×100)、amount 万元→元(×10000)。price 缺失 → None(不注入)。
    """
    price = quote.get("price")
    if price is None:
        return None
    open_ = quote.get("open")
    high = quote.get("high")
    low = quote.get("low")
    if open_ is None:
        open_ = price
    if high is None:
        high = max(price, open_)
    if low is None:
        low = min(price, open_)
    vol_hand = quote.get("volume")
    amt_wan = quote.get("amount_wan")
    row = {
        "date": pd.Timestamp(as_of),
        "open": float(open_),
        "high": float(high),
        "low": float(low),
        "close": float(price),                                  # 午休冻结价当临时收盘
        "volume": float(vol_hand) * 100 if vol_hand is not None else None,   # 手→股
        "amount": float(amt_wan) * 10000 if amt_wan is not None else None,   # 万元→元
        "turnover": quote.get("turnover"),                      # 百分数,同口径
        "pct_chg": quote.get("pct_chg"),                        # 百分数,同口径
    }
    return pd.DataFrame([[row[c] for c in _MASTER_COLS]], columns=_MASTER_COLS)


def inject_midday_bar(df: pd.DataFrame, code: str, quotes: dict, as_of: str) -> pd.DataFrame:
    """在**内存副本**上把今日午盘 bar 追加/覆盖进主档 df;无 quote → 原样返回(停牌等)。

    防未来 + 覆盖语义:先丢掉主档里 date ≥ as_of 的行(避免收盘档若已含当日/未来行造成双 bar 或
    引入未来),再 append 午盘 bar → 保证当日只有一根「午盘 bar」、且最后一根 = as_of。
    **不改传入 df 之外的任何落盘文件**;传入 df 本身也不 inplace 改(先过滤生成新对象)。
    """
    q = quotes.get(code)
    if not q:
        return df                                              # 停牌/无快照:用收盘历史(最新=前一日)
    bar = midday_bar_row(code, q, as_of)
    if bar is None:
        return df
    ts = pd.Timestamp(as_of)
    if "date" in df.columns:
        kept = df[pd.to_datetime(df["date"]) < ts]             # 只保留严格早于 as_of 的历史
    else:
        kept = df
    return pd.concat([kept, bar], ignore_index=True)


@contextlib.contextmanager
def midday_injection(quotes: dict, as_of: str):
    """进程内 monkeypatch `market.load_kline`,让所有 screener/serialize 读到「历史+午盘 bar」。

    退出时**无条件**还原(即使异常)。`load_kline_recent` 内部调 module 全局 `load_kline`,
    patch module 属性即一并生效。**不触碰磁盘主档**。
    """
    orig = market.load_kline

    def _patched(code: str) -> pd.DataFrame:
        df = orig(code)                                        # 每次重新 read_parquet,改副本不落盘
        return inject_midday_bar(df, code, quotes, as_of)

    market.load_kline = _patched
    logger.info("午盘 K 线注入:已挂载(as_of=%s,快照 %d 只)", as_of, len(quotes))
    try:
        yield
    finally:
        market.load_kline = orig
        logger.info("午盘 K 线注入:已还原 market.load_kline")


def fetch_universe_quotes(codes: list[str]) -> dict:
    """拉全A午盘行情快照(gtimg 批量;停牌/异常票不出现在返回里)。失败抛给上层由 _main 决定降级。"""
    logger.info("拉全A午盘行情快照:%d 只(gtimg)", len(codes))
    quotes = gtimg_quote.fetch_quotes(codes)
    logger.info("午盘行情快照命中:%d/%d 只", len(quotes), len(codes))
    return quotes


# ————————————————————————————————————————————————
# 市场环境(≤11:30 广度,仅用冻结快照)
# ————————————————————————————————————————————————
def breadth_from_quotes(quotes: dict) -> dict:
    """从午盘快照算全市场广度(涨/跌家数、中位涨幅)——只用 ≤11:30 冻结数据。"""
    ups = downs = flats = 0
    pcts: list[float] = []
    for q in quotes.values():
        p = q.get("pct_chg")
        if p is None:
            continue
        pcts.append(p)
        if p > 0:
            ups += 1
        elif p < 0:
            downs += 1
        else:
            flats += 1
    med = float(pd.Series(pcts).median()) if pcts else None
    return {"上涨": ups, "下跌": downs, "平盘": flats, "样本": len(pcts),
            "中位涨幅": round(med, 3) if med is not None else None}


# ————————————————————————————————————————————————
# 产出 日内_<date>.md
# ————————————————————————————————————————————————
def render_intraday_md(as_of: str, *, breadth: dict | None = None,
                       out_dir: Path | None = None) -> Path:
    """读候选池消息面确认 view → 写 `docs/每日分析/选股/日内_<date>.md`(全A午盘候选+买入排序)。

    确定性渲染(不跑 LLM):买入排序 = candidate_message 回灌后的完整分榜。缺 view → 写降级 stub。
    """
    out_dir = out_dir or SELECTION_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{MD_PREFIX}_{as_of}.md"

    try:
        view = store.get_view("候选池消息面确认", date=as_of)
    except FileNotFoundError:
        view = None

    lines: list[str] = []
    lines.append(f"# 全A午盘选股 · 日内_{as_of}")
    lines.append("")
    lines.append(f"> 数据时点:**{FREEZE_LABEL}**(午盘 bar 为**未收盘·午盘价**,close=11:30 冻结价)。"
                 f"口径:**全A午盘选股**(午休时点对全A现挑,区别于「盯已选票」的盯盘研判)。")
    lines.append("> 决策只用 ≤11:30 冻结信息;午盘量能仅累计半日,量能类信号请结合半日语境读。"
                 "⚠️ 研究模拟,**非投资建议**。")
    lines.append(f"> 阶段1 裁策略:{'、'.join(sorted(INTRADAY_SKIP_STRATEGIES))}(非alpha,午休省成本)。")
    lines.append("")

    if breadth:
        lines.append(f"## 市场环境(≤11:30)")
        lines.append(f"- 涨/跌/平:{breadth.get('上涨')}/{breadth.get('下跌')}/{breadth.get('平盘')}"
                     f"(样本 {breadth.get('样本')});中位涨幅 {breadth.get('中位涨幅')}%")
        lines.append("")

    if not view or not view.get("重排"):
        lines.append("## 午盘候选")
        lines.append("")
        lines.append("_本次无候选(策略/合议无 view 或候选池为空),降级空跑。_")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("日内选股 md 写出(降级空跑)→ %s", path)
        return path

    scored = view["重排"]
    lines.append(f"## 午盘候选 · 买入排序(消息面回灌后完整分,共 {len(scored)} 只)")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 完整分 | 数据面综合分 | 消息面方向 | 消息面分 | 候选来源 | 理由 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for x in scored:
        reasons = "；".join((x.get("理由") or [])[:3]) or "—"
        srcs = "、".join(x.get("候选来源") or []) or "—"
        lines.append(
            f"| {x.get('候选排名', '')} | {x.get('code', '')} | {x.get('name', '')} "
            f"| {x.get('完整分', '')} | {x.get('数据面综合分', '')} | {x.get('消息面方向', '')} "
            f"| {x.get('消息面分', '')} | {srcs} | {reasons} |")
    lines.append("")

    stat = view.get("统计", {})
    lines.append(f"## 台账")
    lines.append(f"- 候选池规模:{view.get('候选池规模')};上限命中:{view.get('上限命中')}")
    lines.append(f"- 消息面统计:看多 {stat.get('看多', 0)} / 看空 {stat.get('看空', 0)} / "
                 f"中性 {stat.get('中性', 0)} / 全弃权 {stat.get('全弃权', 0)}(有发声 {stat.get('有发声', 0)})")
    lines.append(f"- 回灌参数:{view.get('回灌参数')}")
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("日内选股 md 写出 → %s(%d 只候选)", path, len(scored))
    return path


# ————————————————————————————————————————————————
# 节点主入口
# ————————————————————————————————————————————————
def run_intraday_screen(as_of: str | None = None, *, universe_limit: int | None = None,
                        stage2_no_llm: bool = False, quotes: dict | None = None,
                        codes: list[str] | None = None,
                        run_screen_all_fn=None, cand_msg_fn=None,
                        write_md: bool = True) -> dict:
    """午盘全A选股节点主入口(阶段1 数据初筛 + 阶段2 消息面精选 + 产出 日内 md)。

    Args:
        as_of: 目标日(None → store active_date 或今日)。
        universe_limit: 全A取前 N(小样本联调用;None=全量)。
        stage2_no_llm: 阶段2 消息面是否也跳 LLM(dry-run 用;默认 False=跑 news_ai+情绪)。
        quotes: 预取的行情快照(测试/联调可注入;None → 现拉 gtimg)。
        codes: 全A代码(测试可注入;None → universe.universe_codes)。
        run_screen_all_fn / cand_msg_fn: 可注入桩(测试);默认走真实实现(惰性 import tools.run)。
        write_md: 是否写 日内_<date>.md(测试可关)。

    阶段1 恒 no_llm=True + no_fetch=True + 裁策略11(数据初筛);阶段2 独立跑 candidate_message
    (默认带 LLM)——两段分离保证「阶段1不跑LLM、阶段2消息面才跑LLM」。
    """
    if as_of is None:
        as_of = store.active_date() or store._today()
    store.set_active_date(as_of)

    if codes is None:
        codes = universe.universe_codes(limit=universe_limit)
    if quotes is None:
        quotes = fetch_universe_quotes(codes)
    breadth = breadth_from_quotes(quotes)

    if run_screen_all_fn is None or cand_msg_fn is None:
        from tools import run as _run
        from tools.pipeline import candidate_message as _cmsg
        run_screen_all_fn = run_screen_all_fn or _run.run_screen_all
        cand_msg_fn = cand_msg_fn or _cmsg.run_candidate_message_enrich

    report: dict = {"as_of": as_of, "slot": SLOT, "全A": len(codes),
                    "快照命中": len(quotes), "市场环境": breadth,
                    "裁策略": sorted(INTRADAY_SKIP_STRATEGIES)}

    # 阶段1+阶段2 全程在午盘注入下跑(serialize 也读午盘 bar → record 反映午盘)。
    prev_confirm = os.environ.get("CANDIDATE_MSG_CONFIRM")
    with midday_injection(quotes, as_of):
        # 阶段1:纯数据初筛(no_llm + no_fetch + 裁策略11)。关掉 run_screen_all 内置的消息面节点
        # (CANDIDATE_MSG_CONFIRM=0),改由阶段2 独立跑(以便阶段2 带 LLM,而阶段1 数据段 no_llm)。
        os.environ["CANDIDATE_MSG_CONFIRM"] = "0"
        try:
            stage1 = run_screen_all_fn(codes, as_of, no_llm=True, no_fetch=True,
                                       skip_strategies=INTRADAY_SKIP_STRATEGIES)
        finally:
            if prev_confirm is None:
                os.environ.pop("CANDIDATE_MSG_CONFIRM", None)
            else:
                os.environ["CANDIDATE_MSG_CONFIRM"] = prev_confirm
        report["阶段1"] = {"union": stage1.get("union"), "llm_subset": stage1.get("llm_subset"),
                          "各策略入选": stage1.get("各策略入选")}

        # 阶段2:消息面精选(仅 shortlist,candidate_message 内部各策略 top-K∪ ≤策略数×10 有界)。
        stage2 = cand_msg_fn(as_of, no_llm=stage2_no_llm)
        report["阶段2"] = {"候选池规模": stage2.get("候选池规模"), "统计": stage2.get("统计")}

    if write_md:
        path = render_intraday_md(as_of, breadth=breadth)
        report["产出"] = str(path)
    logger.info("午盘全A选股完成:as_of=%s,全A %d,候选池 %d,产出 %s",
                as_of, len(codes), report.get("阶段2", {}).get("候选池规模"),
                report.get("产出"))
    return report


def _main(argv: list[str] | None = None) -> int:
    """CLI:python -m tools.pipeline.intraday_screen [--date YYYY-MM-DD] [--universe N]
    [--stage2-no-llm] [--force] [--no-md]。

    非交易日跳过退 0(launchd 只认周一~周五,节假日仍触发)。--force 跳过交易日判断(联调)。
    """
    ap = argparse.ArgumentParser(description="午盘全A选股(11:30 午休冻结口径)")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD(默认今日)")
    ap.add_argument("--universe", type=int, default=None, help="全A取前 N(小样本联调)")
    ap.add_argument("--stage2-no-llm", action="store_true", help="阶段2 消息面也跳 LLM(dry-run)")
    ap.add_argument("--force", action="store_true", help="跳过交易日判断")
    ap.add_argument("--no-md", action="store_true", help="不写 日内_<date>.md")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    as_of = args.date or store._today()
    if not args.force and not cal.is_trading_day(as_of):
        logger.info("非交易日 %s,午盘选股跳过(退 0)", as_of)
        return 0
    rep = run_intraday_screen(as_of, universe_limit=args.universe,
                              stage2_no_llm=args.stage2_no_llm, write_md=not args.no_md)
    logger.info("完成:%s", {k: rep[k] for k in ("as_of", "全A", "快照命中", "产出") if k in rep})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
