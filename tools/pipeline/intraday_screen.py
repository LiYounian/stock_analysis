"""午盘全A选股(午休时点对全A现挑票)。

权威设计:docs/计划/2026-09-08_午盘全A选股_设计.md(设计估总耗时 ~25min)。
⚠️ 耗时修正(2026-09-08):早期冒烟阶段1 直调完整 run_screen_all 实测 **1h28min**——设计的
~25min 只有在阶段1 走 `lean=True`(跳数值面深采/事件/因子/前瞻回测/龙虎榜等收盘重活)时才成立。
现阶段1 已恒传 lean=True(见 run_intraday_screen);详见 docs/日志/开发日志.md「阶段1 瘦身」条。

一句话:把「消息面三段式」搬到午休时点,让策略在「收盘历史 + 今日午盘 bar」上跑,
产出午休冻结(11:30)口径的全A午盘候选。与收盘 screenall 并存、互不影响。

## 三段式(= 复用收盘 run_screen_all + candidate_message,只换两处)
- 阶段1 · 纯数据初筛(午休冻结):拉全A gtimg 行情快照(11:30 冻结)→ **内存注入今日午盘 bar**
  → `run_screen_all(no_llm, no_fetch, skip_strategies={策略11}, lean=True)` 跑各数据策略出榜。
  lean=True 跳过收盘才需要的重活(数值面深采/事件/因子/合议/panel/前瞻回测/龙虎榜),只出候选 union
  ——午盘阶段1 从 ~1.5h 瘦身回十几分钟量级(消息面精选由阶段2 在 shortlist 上自采)。
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
import json
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

# 午盘 11:30 全A快照落盘目录(gitignored:data/intraday/;与 intraday_snapshot.py 同根,不入库)。
# 复盘节点(intraday_review)当日 15:xx 读它算「下午涨跌 / 下午等权基准」——快照在采集时刻冻结,
# 防未来天然成立(只含 ≤11:30 冻结价)。**不写主档、不污染 data/master**。
NOON_SNAPSHOT_DIR = settings.PROJECT_ROOT / "data" / "intraday"
NOON_SNAPSHOT_NAME = "noon_screen_snapshot.json"

# ————————————————————————————————————————————————
# 输出侧重点:买入精选 / 规避精选条数(用户 2026-09-10 拍板 D5)
# ————————————————————————————————————————————————
# 买入 5 只是**主评价对象**——午盘选股的价值就看「买入选得准不准」。
# 规避 3 只是**纠偏参照**,不是独立主指标:作用是暴露/纠正模型系统性偏差(防止把烂票也捧上去),
# 靠买入-规避的方向对照来纠偏。切分口径是**输出侧(render)与复盘侧(intraday_review)的单一真源**,
# 用同一函数避免两处漂移(见 docs/计划/2026-09-10_午盘选股迭代_复盘闭环与侧重点重构_设计.md §3.2)。
# 条数集中在配置真源 THRESHOLDS['午盘选股'](便于 §4 历史标定后预注册);读不到 → 回落硬默认 5/3。
def noon_cfg() -> dict:
    """午盘选股配置(THRESHOLDS['午盘选股']);缺失/异常 → 空 dict(调用方用硬默认)。"""
    try:
        from tools.config import strategy as _strategy
        return _strategy.THRESHOLDS.get("午盘选股", {}) or {}
    except Exception:                                          # noqa: BLE001 配置缺失不阻断选股
        return {}


N_BUY = int(noon_cfg().get("买入条数", 5))
N_AVOID = int(noon_cfg().get("规避条数", 3))
_BEARISH = {"看空"}          # 看空方向:排除出买入、优先进规避


def _full_score(x: dict) -> float:
    """取完整分用于排序;缺失/非数值沉底(-inf),不参与买入头部。"""
    s = x.get("完整分")
    return float(s) if isinstance(s, (int, float)) else float("-inf")


def split_buy_avoid(reranked: list[dict] | None,
                    n_buy: int = N_BUY, n_avoid: int = N_AVOID) -> tuple[list[dict], list[dict]]:
    """把消息面回灌后的完整分榜(view「候选池消息面确认」的「重排」)切成【今日可买入】+【今日规避】。

    单一真源:选股输出与复盘取数共用本函数。切分规则(D5 拍板):
    - 买入:**排除看空**后,按完整分降序取 Top n_buy(主评价对象)。
    - 规避:**看空票优先**(按完整分升序、最弱在前),不足 n_avoid 则从「买入未取」的剩余票里
      按完整分升序补最低分,凑满 n_avoid(纠偏参照)。
    - 买入 / 规避保证**不相交**(先取买入,规避只从剩余里取)。
    输入通常已按完整分降序(candidate_message 阶段3 保证),本函数不依赖该前置、自行稳定排序。
    """
    ranked = sorted(list(reranked or []), key=_full_score, reverse=True)
    非看空 = [x for x in ranked if x.get("消息面方向") not in _BEARISH]
    买入 = 非看空[:n_buy]
    买入_codes = {x.get("code") for x in 买入}
    剩余 = [x for x in ranked if x.get("code") not in 买入_codes]

    看空票 = sorted((x for x in 剩余 if x.get("消息面方向") in _BEARISH), key=_full_score)
    规避 = 看空票[:n_avoid]
    if len(规避) < n_avoid:                                     # 看空不足 → 补最低分票
        规避_codes = {x.get("code") for x in 规避}
        补 = sorted((x for x in 剩余 if x.get("code") not in 规避_codes), key=_full_score)
        规避 += 补[: n_avoid - len(规避)]
    return 买入, 规避


def action_tag(x: dict, group: str) -> str:
    """行动/时效标注(首版规则,待 §4 闸门在历史样本上标定)。

    诚实边界:午盘量能仅累计半日,「尾盘可买」不做重量能承诺,以价形/趋势/横截面+消息面为主。
    - 买入 · 消息面看多/看涨 → 「尾盘可买」;买入 · 中性 → 「次日观察」。
    - 规避 → 「仅规避」。
    """
    if group == "规避":
        return "仅规避"
    return "尾盘可买" if x.get("消息面方向") in {"看多", "看涨"} else "次日观察"


# ————————————————————————————————————————————————
# D-0 交易计划(P0-2,2026-09-13 复盘补):给买入票补「可执行的当日进出场纪律」
# ————————————————————————————————————————————————
# 缺口:午盘买入表原只有「尾盘可买/次日观察」,无止损位/目标位 → 无法执行、无法回答「达没达目标」。
# 修整方向不是调目标数值,而是补一套 **D-0 专用**进出场规则:止损/止盈按**当日波动自适应锚定**
# (ATR/振幅,非拍脑袋固定百分比),**收盘无条件了结**(午盘选股定位=尾盘前当日买卖,天然了结点=收盘)。
def trade_plan_cfg() -> dict:
    """午盘交易计划配置(THRESHOLDS['午盘选股']['交易计划']);缺失/异常 → 空 dict(调用方用硬默认)。"""
    return (noon_cfg().get("交易计划", {}) or {})


def _atr_pct(code: str, as_of: str, *, load_kline_fn=None, window: int = 14) -> float | None:
    """历史日线 ATR(真实波幅均值)占最近收盘价的百分比,作波动锚。

    防未来红线:只用 `date < as_of` 的**完整**历史日线(绝不含午盘 bar / 当日 / 未来行);
    真实波幅 TR = max(high−low, |high−prev_close|, |low−prev_close|)。历史不足(<2 根)→ None。
    """
    load_kline_fn = load_kline_fn or market.load_kline
    try:
        df = load_kline_fn(code)
    except Exception:                                          # noqa: BLE001 缺档/异常 → 无 ATR
        return None
    if df is None or "date" not in getattr(df, "columns", []) or df.empty:
        return None
    hist = df[pd.to_datetime(df["date"]) < pd.Timestamp(as_of)].tail(window + 1)
    if len(hist) < 2:
        return None
    high = hist["high"].astype(float).tolist()
    low = hist["low"].astype(float).tolist()
    close = hist["close"].astype(float).tolist()
    trs = [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
           for i in range(1, len(hist))]
    trs = [t for t in trs[-window:] if t == t]                 # 去 NaN
    last_close = close[-1]
    if not trs or last_close is None or last_close <= 0 or last_close != last_close:
        return None
    return sum(trs) / len(trs) / last_close * 100.0


def _intraday_range_pct(quote: dict | None) -> float | None:
    """当日(≤11:30 半日)振幅占现价的百分比 = (high−low)/price×100。缺失 → None。

    诚实边界:午盘只累计半日,振幅系统性偏低,仅作 ATR 不可算时的回退锚。
    """
    if not quote:
        return None
    hi, lo, price = quote.get("high"), quote.get("low"), quote.get("price")
    if hi is None or lo is None or price is None or price <= 0:
        return None
    return (float(hi) - float(lo)) / float(price) * 100.0


def compute_trade_plan(code: str, quote: dict | None, as_of: str, *,
                       load_kline_fn=None, cfg: dict | None = None) -> dict | None:
    """给一只午盘买入票算 D-0 交易计划(进场/止损/止盈/了结)。price 缺失(停牌)→ None。

    波动锚优先级:ATR(N 日真实波幅%)→ 当日半日振幅% → 缺省锚(均不可算时)。
    止损距离 = 止损倍数×锚,夹在 [止损下限, 止损上限];止盈距离 = 止盈倍数×锚。了结=当日收盘无条件平仓。
    """
    cfg = cfg if cfg is not None else trade_plan_cfg()
    price = (quote or {}).get("price")
    if price is None or price <= 0:
        return None
    window = int(cfg.get("ATR窗口", 14))
    atr_pct = _atr_pct(code, as_of, load_kline_fn=load_kline_fn, window=window)
    range_pct = _intraday_range_pct(quote)
    if atr_pct is not None and atr_pct > 0:
        anchor_pct, anchor_src = atr_pct, f"ATR{window}(日线真实波幅)"
    elif range_pct is not None and range_pct > 0:
        anchor_pct, anchor_src = range_pct, "当日半日振幅(历史不足回退)"
    else:
        anchor_pct, anchor_src = float(cfg.get("波动锚缺省pct", 3.0)), "缺省锚(无ATR/振幅)"

    k_stop = float(cfg.get("止损ATR倍数", 1.0))
    k_tgt = float(cfg.get("止盈ATR倍数", 1.5))
    lo_pct = float(cfg.get("止损下限pct", 1.5))
    hi_pct = float(cfg.get("止损上限pct", 7.0))
    up_tol = float(cfg.get("进场上浮容忍pct", 0.5))

    stop_dist = min(max(k_stop * anchor_pct, lo_pct), hi_pct)
    tgt_dist = k_tgt * anchor_pct
    price = float(price)
    stop_price = round(price * (1 - stop_dist / 100.0), 2)
    tgt_price = round(price * (1 + tgt_dist / 100.0), 2)
    entry_cap = round(price * (1 + up_tol / 100.0), 2)
    return {
        "现价11:30": round(price, 2),
        "进场触发": f"尾盘买入,现价≤{entry_cap}不追高;已跌破止损 {stop_price} 则放弃进场",
        "止损位": stop_price, "止损距离%": round(stop_dist, 2),
        "止盈位": tgt_price, "止盈距离%": round(tgt_dist, 2),
        "波动锚": f"{anchor_src} {anchor_pct:.2f}%",
        "了结": "当日收盘无条件平仓(D-0 当日买卖);盘中先触止损/止盈即离场;次日跳空低开破止损开盘即走",
    }


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


def noon_snapshot_path(as_of: str, *, out_root: Path | None = None) -> Path:
    """午盘 11:30 快照落盘路径(供落盘与复盘读取共用单一真源)。"""
    return (out_root or NOON_SNAPSHOT_DIR) / as_of / NOON_SNAPSHOT_NAME


def persist_noon_snapshot(quotes: dict, as_of: str, *, out_root: Path | None = None) -> Path:
    """把全A 11:30 冻结快照(gtimg,run_intraday_screen 已在内存持有)落盘,供当日午盘复盘取「下午」口径。

    只存 ≤11:30 冻结信息(price = 午休冻结价 = 当日临时收盘);防未来天然成立(采集时刻冻结)。
    原子写(tmp → replace)。gitignored 路径,不入库、不碰主档。
    """
    path = noon_snapshot_path(as_of, out_root=out_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = {code: {"price": q.get("price"), "open": q.get("open"),
                   "pct_chg": q.get("pct_chg"), "turnover": q.get("turnover")}
            for code, q in quotes.items() if q.get("price") is not None}
    payload = {"as_of": as_of, "slot": SLOT, "freeze_label": FREEZE_LABEL,
               "note": "11:30 午休冻结价(gtimg);price=当日临时收盘,防未来只含≤11:30。供当日午盘复盘算下午涨跌/下午等权基准。",
               "count": len(kept), "quotes": kept}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("午盘 11:30 快照落盘 → %s(%d 只)", path, len(kept))
    return path


# 快照样本率下限:低于此视为采集异常 → 拉取重试 / 落盘后告警(与复盘取样率下限对齐,单一口径)。
# P0-2/P0-1(2026-09-13 复盘):堵「快照缺失/半空 → 复盘 α=None、丢当日样本、10日闸门永远凑不齐」。
SNAPSHOT_MIN_COVERAGE = 0.60


def snapshot_self_check(as_of: str, universe_n: int, *, out_root: Path | None = None,
                        min_coverage: float = SNAPSHOT_MIN_COVERAGE) -> dict:
    """落盘后自检:11:30 快照是否存在 + 样本率是否达标(不达标 → ok=False,上层告警不静默降级)。

    universe_n = 本次全A只数(样本率分母)。文件缺失/解析失败/样本率不足 → ok=False + reason。
    """
    path = noon_snapshot_path(as_of, out_root=out_root)
    if not path.exists():
        return {"ok": False, "reason": "快照文件未生成", "count": 0, "coverage": 0.0,
                "universe": universe_n, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                                     # noqa: BLE001 解析失败也算自检不过
        return {"ok": False, "reason": f"快照解析失败:{e}", "count": 0, "coverage": 0.0,
                "universe": universe_n, "path": str(path)}
    count = int(payload.get("count") or 0)
    coverage = (count / universe_n) if universe_n else 0.0
    ok = count > 0 and coverage >= min_coverage
    return {"ok": ok, "count": count, "coverage": round(coverage, 4),
            "universe": universe_n, "min_coverage": min_coverage, "path": str(path),
            "reason": None if ok else f"样本率不足({count}/{universe_n}={coverage:.0%}<{min_coverage:.0%})"}


# ————————————————————————————————————————————————
# 产出 日内_<date>.md
# ————————————————————————————————————————————————
def render_intraday_md(as_of: str, *, breadth: dict | None = None,
                       out_dir: Path | None = None, quotes: dict | None = None,
                       load_kline_fn=None) -> Path:
    """读候选池消息面确认 view → 写 `docs/每日分析/选股/日内_<date>.md`(全A午盘候选+买入排序)。

    确定性渲染(不跑 LLM):买入排序 = candidate_message 回灌后的完整分榜。缺 view → 写降级 stub。
    quotes 传入时(P0-2)给买入组渲染「D-0 交易计划」表(进场/止损/止盈/了结);缺则跳过该表(向后兼容)。
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
    买入, 规避 = split_buy_avoid(scored)

    # 【今日可买入】(5 只,主评价对象)——聚焦「今日未结束交易日内可买入」。
    lines.append(f"## 今日可买入(精选 {len(买入)} 只 · 主评价对象)")
    lines.append("")
    lines.append("> 口径:完整分 Top(排除看空)。买入组是午盘选股的**主评价对象**,复盘只看「买入选得准不准」。")
    lines.append("")
    lines.append("| 序 | 代码 | 名称 | 完整分 | 数据面综合分 | 消息面方向 | 消息面分 | 行动/时效 | 候选来源 | 理由 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, x in enumerate(买入, 1):
        reasons = "；".join((x.get("理由") or [])[:3]) or "—"
        srcs = "、".join(x.get("候选来源") or []) or "—"
        lines.append(
            f"| {i} | {x.get('code', '')} | {x.get('name', '')} | {x.get('完整分', '')} "
            f"| {x.get('数据面综合分', '')} | {x.get('消息面方向', '')} | {x.get('消息面分', '')} "
            f"| {action_tag(x, '买入')} | {srcs} | {reasons} |")
    if not 买入:
        lines.append("| — | — | _无非看空候选_ | | | | | | | |")
    lines.append("")

    # 【D-0 交易计划】(仅买入组;P0-2 补「可执行纪律」缺口)——止损/止盈按当日波动锚定、收盘强制了结。
    if quotes is not None and 买入:
        lines.append("## 今日可买入 · D-0 交易计划(进场/止损/止盈/了结)")
        lines.append("")
        lines.append("> 午盘选股定位=**尾盘前当日买卖**:止损/止盈按**当日波动(ATR·振幅)自适应锚定**"
                     "(非拍脑袋固定百分比),**当日收盘无条件了结**;盘中先触止损/止盈即离场,"
                     "次日跳空低开破止损开盘即走(#20 跳空保护)。⚠️ 研究模拟,**非投资建议**;"
                     "量能仅累计半日,进场以价形/趋势为主。")
        lines.append("")
        lines.append("| 序 | 代码 | 名称 | 现价(11:30) | 进场触发 | 止损位 | 止盈位 | 波动锚 | 了结纪律 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, x in enumerate(买入, 1):
            code = x.get("code", "")
            tp = compute_trade_plan(code, (quotes or {}).get(code), as_of,
                                    load_kline_fn=load_kline_fn)
            if tp is None:
                lines.append(f"| {i} | {code} | {x.get('name', '')} | — | "
                             f"_无午盘快照(停牌?),无法定价位_ | — | — | — | 当日收盘了结 |")
                continue
            lines.append(
                f"| {i} | {code} | {x.get('name', '')} | {tp['现价11:30']} | {tp['进场触发']} "
                f"| {tp['止损位']}(−{tp['止损距离%']}%) | {tp['止盈位']}(+{tp['止盈距离%']}%) "
                f"| {tp['波动锚']} | {tp['了结']} |")
        lines.append("")

    # 【今日规避】(3 只,纠偏参照)。
    lines.append(f"## 今日规避(精选 {len(规避)} 只 · 纠偏参照)")
    lines.append("")
    lines.append("> 口径:看空优先,不足则补最低分。规避组**不是主指标**,用于暴露/纠正模型系统性偏差。")
    lines.append("")
    lines.append("| 序 | 代码 | 名称 | 完整分 | 数据面综合分 | 消息面方向 | 消息面分 | 行动/时效 | 候选来源 | 理由 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for i, x in enumerate(规避, 1):
        reasons = "；".join((x.get("理由") or [])[:3]) or "—"
        srcs = "、".join(x.get("候选来源") or []) or "—"
        lines.append(
            f"| {i} | {x.get('code', '')} | {x.get('name', '')} | {x.get('完整分', '')} "
            f"| {x.get('数据面综合分', '')} | {x.get('消息面方向', '')} | {x.get('消息面分', '')} "
            f"| {action_tag(x, '规避')} | {srcs} | {reasons} |")
    if not 规避:
        lines.append("| — | — | _无规避候选_ | | | | | | | |")
    lines.append("")

    # 完整候选台账(全序,折叠)——保留全量供复盘取数 + 审计留痕(不丢数据,只改呈现重心)。
    lines.append("<details>")
    lines.append(f"<summary>完整候选台账 · 买入排序(消息面回灌后完整分,共 {len(scored)} 只)</summary>")
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
    lines.append("</details>")
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
# 旁路机读候选(D1):供午盘 Claude 逐票深度分析消费的 noon 冻结、close 不覆盖机读产物
# ————————————————————————————————————————————————
# 缘由:view「候选池消息面确认」落盘路径与收盘 run_screen_all 共享 → 收盘会**覆盖**午盘版
# (post-hoc/复盘/验证读到的是收盘版,不可靠)。故在午盘末尾**额外**落一份独立文件名、noon 冻结
# 的机读候选,给 daily-stock-noon-analysis(Claude 深度)稳定消费。
# **纯旁路铁律:不动阶段1/阶段2 主逻辑、不改评分、不写主档、gitignored;缺 view → 不写(返回 None),
# 不阻断主流程。** 消费侧 read_noon_candidates 优先读它、缺失回退解析 日内全A_<date>.md 台账。
NOON_CANDIDATES_NAME = "noon_candidates.json"            # 全量(阶段2 消息面回灌后)
# 阶段1 早产候选(数据面综合分排序,免等阶段2 LLM 消息面 batch):供午盘 Claude 任务 ~11:50 就开跑。
# 统筹 2026-09-14 拍板 Q2:门控只等阶段1(~11:50)、按数据面综合分选 Top-N、SKILL 自采 per-stock 消息面
# → 窗口 11:50→13:00 有 ~70min、解耦 12:42 全量瓶颈。数据面综合分 = candidate_message 的 council
# (base 专家组=默认组剔除消息面专家),no_llm 跳三层情绪即得(完整分≈数据面综合分)。
NOON_CANDIDATES_STAGE1_NAME = "noon_candidates_stage1.json"

# 只保留 Claude 深度分析需要的字段(机读稳,避免 view 内部字段漂移带出)。
_CAND_FIELDS = ("候选排名", "code", "name", "完整分", "数据面综合分",
                "消息面方向", "消息面分", "候选来源", "理由")


def _slim_candidate(x: dict) -> dict:
    """截取候选的机读稳定字段(理由拼成一段,与 md 台账口径一致)。"""
    d: dict = {}
    for k in _CAND_FIELDS:
        v = x.get(k)
        if k == "理由":
            v = "；".join((v or [])[:3]) if isinstance(v, list) else (v or "")
        elif k == "候选来源":
            v = "、".join(v) if isinstance(v, list) else (v or "")
        d[k] = v
    return d


def noon_candidates_path(as_of: str, *, out_root: Path | None = None,
                         filename: str = NOON_CANDIDATES_NAME) -> Path:
    """旁路机读候选落盘路径(与 11:30 快照同根 data/intraday/,gitignored,单一真源)。

    filename=NOON_CANDIDATES_NAME(全量)/ NOON_CANDIDATES_STAGE1_NAME(阶段1 早产)。
    """
    return (out_root or NOON_SNAPSHOT_DIR) / as_of / filename


def persist_noon_candidates(as_of: str, *, view: dict | None = None,
                            breadth: dict | None = None,
                            out_root: Path | None = None,
                            stage: str = "full",
                            filename: str | None = None) -> Path | None:
    """把午盘候选池(view「候选池消息面确认」重排 + 买入/规避切分)落成 noon 冻结机读 JSON。

    纯旁路:view 缺失/无重排 → 返回 None(不写、不阻断)。原子写。**不碰主档、不改现有产物。**
    切分复用 split_buy_avoid(与 render_intraday_md 单一真源,避免两处漂移)。
    stage="full"(阶段2 消息面回灌后)/ "stage1"(阶段1 数据面综合分排序,免等 LLM batch)。
    """
    if view is None:
        try:
            view = store.get_view("候选池消息面确认", date=as_of)
        except FileNotFoundError:
            view = None
    reranked = (view or {}).get("重排") if view else None
    if not reranked:
        logger.info("旁路候选(%s):无 view/重排,跳过落盘(as_of=%s)", stage, as_of)
        return None
    买入, 规避 = split_buy_avoid(reranked)
    fname = filename or (NOON_CANDIDATES_STAGE1_NAME if stage == "stage1" else NOON_CANDIDATES_NAME)
    note = ("全A午盘候选池(≤11:30 冻结),供午盘逐票深度分析消费;close 不覆盖此文件。研究模拟,非投资建议。"
            if stage != "stage1" else
            "全A午盘候选池·阶段1早产(≤11:30 冻结,数据面综合分排序,未含阶段2消息面回灌;完整分≈数据面综合分)。"
            "供午盘任务 ~11:50 门控/取数,Claude 自采 per-stock 消息面深挖。close 不覆盖。研究模拟,非投资建议。")
    payload = {
        "as_of": as_of, "slot": SLOT, "freeze_label": FREEZE_LABEL, "stage": stage,
        "note": note,
        "候选池规模": (view or {}).get("候选池规模"),
        "market_breadth": breadth,
        "买入代码": [x.get("code") for x in 买入],
        "规避代码": [x.get("code") for x in 规避],
        "买入": [_slim_candidate(x) for x in 买入],
        "规避": [_slim_candidate(x) for x in 规避],
        "台账": [_slim_candidate(x) for x in reranked],
        "统计": (view or {}).get("统计"),
    }
    path = noon_candidates_path(as_of, out_root=out_root, filename=fname)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.info("旁路候选落盘(%s)→ %s(台账 %d 只,买入 %d,规避 %d)",
                stage, path, len(reranked), len(买入), len(规避))
    return path


def parse_intraday_all_md(as_of: str, *, selection_dir: Path | None = None,
                          top: int | None = None) -> dict | None:
    """回退路径:解析 `日内全A_<date>.md` 的候选表 → 结构化候选(机读稳退化路径)。

    从「今日可买入」表取买入代码、「今日规避」表取规避代码、「完整候选台账」取全序台账。
    文件不存在 → None。仅解析既有 noon 冻结 md,不产生任何副作用。
    """
    sel = selection_dir or SELECTION_DIR
    path = sel / f"{MD_PREFIX}_{as_of}.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    def _cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    section = None            # 当前所在小节:buy/avoid/ledger/None
    买入, 规避, 台账 = [], [], []
    for ln in lines:
        s = ln.strip()
        if s.startswith("## 今日可买入") and "D-0" not in s:
            section = "buy"; continue
        if s.startswith("## 今日可买入 · D-0"):
            section = None; continue          # D-0 计划表不是候选列表
        if s.startswith("## 今日规避"):
            section = "avoid"; continue
        if "完整候选台账" in s:
            section = "ledger"; continue
        if s.startswith("## ") or s.startswith("</details>"):
            section = None; continue
        if section is None or not s.startswith("|"):
            continue
        cells = _cells(s)
        if len(cells) < 3:
            continue
        head = cells[0]
        # 跳过表头/分隔行(首列非纯数字排名)。
        if not head.isdigit():
            continue
        code = cells[1]
        name = cells[2] if len(cells) > 2 else ""
        rec = {"候选排名": int(head), "code": code, "name": name}
        # 台账列序: 排名|代码|名称|完整分|数据面综合分|消息面方向|消息面分|候选来源|理由
        if section == "ledger" and len(cells) >= 9:
            rec.update({"完整分": cells[3], "数据面综合分": cells[4],
                        "消息面方向": cells[5], "消息面分": cells[6],
                        "候选来源": cells[7], "理由": cells[8]})
        if section == "buy":
            买入.append(rec)
        elif section == "avoid":
            规避.append(rec)
        elif section == "ledger":
            台账.append(rec)
    if not (买入 or 台账):
        return None
    return {"source": "md", "as_of": as_of,
            "台账": 台账[:top] if top else 台账,
            "买入代码": [x["code"] for x in 买入],
            "规避代码": [x["code"] for x in 规避],
            "买入": 买入, "规避": 规避}


def _read_candidates_json(path: Path, as_of: str, top: int | None) -> dict | None:
    """读一份旁路候选 JSON → 统一结构;损坏 → None。"""
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                                      # noqa: BLE001 损坏 → None
        logger.warning("旁路候选 JSON 解析失败(%s):%s", path.name, e)
        return None
    台账 = payload.get("台账") or []
    return {"source": "json", "stage": payload.get("stage", "full"), "as_of": as_of,
            "台账": 台账[:top] if top else 台账,
            "买入代码": payload.get("买入代码") or [],
            "规避代码": payload.get("规避代码") or [],
            "买入": payload.get("买入") or [], "规避": payload.get("规避") or []}


def read_noon_candidates(as_of: str, *, root: Path | None = None,
                         selection_dir: Path | None = None,
                         top: int | None = None, stage: str = "auto") -> dict | None:
    """供午盘 Claude 深度分析消费:读旁路 JSON,缺失/损坏回退解析 日内全A_<date>.md 台账。

    stage:
      - "stage1":只读阶段1早产候选(数据面综合分排序,~11:50 就绪)——午盘任务门控/取数用。
      - "full"  :只读全量候选(阶段2 消息面回灌后)。
      - "auto"(默认):全量优先 → 阶段1 → md 台账(取最富的可得源)。
    返回 {"source","stage","as_of","台账","买入代码","规避代码","买入","规避"};都无 → None。
    top 给定时台账截断 Top-N(买入/规避不截)。只读,无副作用。
    """
    full_path = noon_candidates_path(as_of, out_root=root, filename=NOON_CANDIDATES_NAME)
    s1_path = noon_candidates_path(as_of, out_root=root, filename=NOON_CANDIDATES_STAGE1_NAME)
    if stage == "stage1":
        got = _read_candidates_json(s1_path, as_of, top)
        return got if got is not None else parse_intraday_all_md(as_of, selection_dir=selection_dir, top=top)
    if stage == "full":
        got = _read_candidates_json(full_path, as_of, top)
        return got if got is not None else parse_intraday_all_md(as_of, selection_dir=selection_dir, top=top)
    # auto:全量优先 → 阶段1 → md
    for p in (full_path, s1_path):
        got = _read_candidates_json(p, as_of, top)
        if got is not None:
            return got
    return parse_intraday_all_md(as_of, selection_dir=selection_dir, top=top)


# ————————————————————————————————————————————————
# 节点主入口
# ————————————————————————————————————————————————
def run_intraday_screen(as_of: str | None = None, *, universe_limit: int | None = None,
                        stage2_no_llm: bool = False, quotes: dict | None = None,
                        codes: list[str] | None = None,
                        run_screen_all_fn=None, cand_msg_fn=None,
                        write_md: bool = True, persist_snapshot: bool = True,
                        persist_candidates: bool = True,
                        emit_stage1_candidates: bool = True,
                        snapshot_root: Path | None = None) -> dict:
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
        # P0-1:覆盖率过低疑似采集异常 → 重试一次拉取(取更全的一次),避免快照空/半空丢当日复盘样本。
        cov = (len(quotes) / len(codes)) if codes else 0.0
        if cov < SNAPSHOT_MIN_COVERAGE:
            logger.warning("午盘行情覆盖率过低 %.0f%%(%d/%d),重试一次拉取", cov * 100, len(quotes), len(codes))
            retry = fetch_universe_quotes(codes)
            if len(retry) > len(quotes):
                quotes = retry
    breadth = breadth_from_quotes(quotes)

    # D1:落盘 11:30 全A快照,供当日午盘复盘(intraday_review)算「下午」口径。已在内存,零额外采集。
    snap_path = persist_noon_snapshot(quotes, as_of, out_root=snapshot_root) if persist_snapshot else None
    # P0-1:落盘后自检(存在性+样本率),不达标即 ERROR 告警(不静默降级)——复盘届时会走降级回退,但先在此暴露。
    snap_check = snapshot_self_check(as_of, len(codes), out_root=snapshot_root) if persist_snapshot else None
    if snap_check and not snap_check["ok"]:
        logger.error("⚠️ 午盘 11:30 快照自检未通过:%s(复盘将走降级口径,请关注采集健康)", snap_check["reason"])

    if run_screen_all_fn is None or cand_msg_fn is None:
        from tools import run as _run
        from tools.pipeline import candidate_message as _cmsg
        run_screen_all_fn = run_screen_all_fn or _run.run_screen_all
        cand_msg_fn = cand_msg_fn or _cmsg.run_candidate_message_enrich

    report: dict = {"as_of": as_of, "slot": SLOT, "全A": len(codes),
                    "快照命中": len(quotes), "市场环境": breadth,
                    "快照落盘": str(snap_path) if snap_path else None,
                    "快照自检": snap_check,
                    "裁策略": sorted(INTRADAY_SKIP_STRATEGIES)}

    # 阶段1+阶段2 全程在午盘注入下跑(serialize 也读午盘 bar → record 反映午盘)。
    prev_confirm = os.environ.get("CANDIDATE_MSG_CONFIRM")
    with midday_injection(quotes, as_of):
        # 阶段1:纯数据初筛(no_llm + no_fetch + 裁策略11)。关掉 run_screen_all 内置的消息面节点
        # (CANDIDATE_MSG_CONFIRM=0),改由阶段2 独立跑(以便阶段2 带 LLM,而阶段1 数据段 no_llm)。
        os.environ["CANDIDATE_MSG_CONFIRM"] = "0"
        try:
            stage1 = run_screen_all_fn(codes, as_of, no_llm=True, no_fetch=True,
                                       skip_strategies=INTRADAY_SKIP_STRATEGIES, lean=True)
        finally:
            if prev_confirm is None:
                os.environ.pop("CANDIDATE_MSG_CONFIRM", None)
            else:
                os.environ["CANDIDATE_MSG_CONFIRM"] = prev_confirm
        report["阶段1"] = {"union": stage1.get("union"), "llm_subset": stage1.get("llm_subset"),
                          "各策略入选": stage1.get("各策略入选")}

        # 阶段1.5 旁路(统筹 Q2):早产「数据面综合分」候选(no_llm 跳三层情绪 batch),供午盘任务 ~11:50
        # 门控/取数。**纯旁路:阶段1/阶段2 语义不变。** no_llm 采的新闻当日缓存 → 阶段2 全量复用不重采。
        # 失败只 warning、不阻断(阶段2 全量仍会产出)。
        if emit_stage1_candidates:
            try:
                cand_msg_fn(as_of, no_llm=True)               # 落 view「候选池消息面确认」(数据面综合分,情绪弃权)
                s1_path = persist_noon_candidates(as_of, breadth=breadth, out_root=snapshot_root,
                                                  stage="stage1")
                report["阶段1候选"] = str(s1_path) if s1_path else None
            except Exception as e:                            # noqa: BLE001 早产旁路不阻断主流程
                logger.warning("阶段1早产候选失败(不阻断,阶段2全量仍产出):%s", e)
                report["阶段1候选"] = None

        # 阶段2:消息面精选(仅 shortlist,candidate_message 内部各策略 top-K∪ ≤策略数×10 有界)。
        stage2 = cand_msg_fn(as_of, no_llm=stage2_no_llm)
        report["阶段2"] = {"候选池规模": stage2.get("候选池规模"), "统计": stage2.get("统计")}

    if write_md:
        # 传 quotes 给 render:买入组产 D-0 交易计划(P0-2);此处在注入已还原后跑,load_kline 读净收盘历史算 ATR。
        path = render_intraday_md(as_of, breadth=breadth, quotes=quotes)
        report["产出"] = str(path)

    # D1 旁路:额外落 noon 冻结机读候选(close 不覆盖),供午盘 Claude 逐票深度分析稳定消费。
    # 纯旁路:失败/无 view 只记 warning、不阻断主流程(选股产物已落盘)。
    if persist_candidates:
        try:
            cand_path = persist_noon_candidates(as_of, breadth=breadth, out_root=snapshot_root)
            report["旁路候选"] = str(cand_path) if cand_path else None
        except Exception as e:                                  # noqa: BLE001 旁路不阻断主流程
            logger.warning("旁路候选落盘失败(不阻断):%s", e)
            report["旁路候选"] = None
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
    ap.add_argument("--read-candidates", action="store_true",
                    help="只读:打印当日午盘机读候选(旁路JSON优先,回退解析日内全A_ md),供 Claude 深度任务消费")
    ap.add_argument("--stage", choices=["auto", "stage1", "full"], default="auto",
                    help="配合 --read-candidates:stage1=阶段1早产(~11:50门控用)/full=全量/auto=全量优先")
    ap.add_argument("--top", type=int, default=None, help="配合 --read-candidates:台账截断 Top-N")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    as_of = args.date or store._today()

    # 只读模式:不跑选股,只吐当日机读候选(供 daily-stock-noon-analysis 门控/取数)。
    if args.read_candidates:
        cand = read_noon_candidates(as_of, top=args.top, stage=args.stage)
        print(json.dumps(cand, ensure_ascii=False, indent=2) if cand is not None
              else json.dumps({"as_of": as_of, "候选": None,
                               "note": "当日无旁路候选JSON且无日内全A_md(intraday_screen未就绪?)"},
                              ensure_ascii=False))
        return 0 if cand is not None else 2
    if not args.force and not cal.is_trading_day(as_of):
        logger.info("非交易日 %s,午盘选股跳过(退 0)", as_of)
        return 0
    rep = run_intraday_screen(as_of, universe_limit=args.universe,
                              stage2_no_llm=args.stage2_no_llm, write_md=not args.no_md)
    logger.info("完成:%s", {k: rep[k] for k in ("as_of", "全A", "快照命中", "产出") if k in rep})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
