"""午盘全A量价重筛(纯 Python 轻量流水,快/经济,无逐票 LLM)。

## 为什么有这个模块(2026-09-16)

午盘研判(`daily-stock-noon-analysis`)只盯**盯盘集~19 只**、不重筛全 A → 盘中全 A 里
爆发的强势动量(如 09-16 算力/CPO 涨停成片)午盘**无采集、无重筛、够不着**,两次踏空。
既有 `intraday_screen.py`(2026-09-08)虽做全 A 午盘选股,但走**完整收盘 screener+逐票 LLM
消息面**(十几分钟量级、烧 LLM),定位是"深研判"。本模块是它的**互补件**:

  **纯量价、不逐票 LLM、不跑收盘闭环维、板块-agnostic**——只吃一份 11:30 冻结的全 A 快照,
  算动量/突破/放量三类量价因子、横截面打分排序,**秒级**出「强势候选」。用户明确要求午盘
  **正式**做全 A 重筛(非 shadow 门控),本产出即正式午盘量价选股,供尾盘 14:30~14:57 回踩限价参与。

## 数据流(午休 ~11:35 触发,抢在 13:00 开盘前)

1. **全A午盘快照**:`gtimg_quote.fetch_quotes(全A)`(实测 ~16s)→ 冻结落
   `data/intraday/<date>/T1145_full.json`。全 A 代码复用 `screen_forward_common.universe_codes`
   (已落地主档、离线、排北交所),**不重拉 universe、不打 akshare**。
2. **轻量重筛**:在快照上算量价因子(半日涨幅/量比/换手/贴日内高/集合竞价高开/日内不破位),
   横截面百分位打分 → 动量·突破·放量三类加权合成 → 排序。
3. **过滤**:流动性门(半日成交额下限)+ **涨停不可买剔除**(午盘已封板买不进 → 只标"已封板·观察次日")
   + 与当日盯盘集去重(避免与盯盘研判重复)。
4. **产出**:`docs/每日分析/选股/午盘全A选股_<date>.md`(「午盘全A重筛·强势候选」节 + 机读 PICKS 锚点)
   + 结构化 JSON `data/intraday/<date>/noon_fullA_screen.json`。标注"半日 provisional·尾盘复核·防未来≤11:30"。

## 防未来红线(硬约束)

- 决策**只用 ≤11:30 冻结信息**:快照在采集时刻冻结(morning session 11:30 收盘 → 快照天然只含 ≤11:30);
  `screen()` 纯函数**只吃传入 quotes**、绝不联网、绝不读当日/未来行。
- **涨停不可买**:午盘已封板的票(`breadth.is_limit_hit`)**不进可买候选**(够不着=不算数),
  只在「已封板·观察次日」旁列留痕。倾向对齐:不追高开涨停、强势票尾盘**回踩限价**参与(不追高)。

## 诚实边界

- **量能仅累计半日**:volume/amount/turnover 只到 11:30,量能类因子系统性偏低;量比(vol_ratio)
  由源方给出、可比性更好。横截面**百分位**打分对"全体半日偏低"不敏感(比的是相对强弱),故仍稳。
- **无法严格历史回测**:项目无历史全 A 午盘快照 → 本重筛信号只能上线后 **forward 记分**核实
  (尾盘模拟建仓 position_ledger → 次日绝对收益)。用户已明示正式上线不搞 shadow 门控,故先上线产出、
  并行 forward 记分复核。
- **板块-agnostic**:本线只做全 A 量价重筛;板块定向(占优板块里选先锋/中军/补涨)由另一条
  「板块预测系统」提供,`screen()` 预留 `prev_high_map`(突破昨高)与 `sector_hint` 接口待接入。

⚠️ 测试环境研究模拟,**非投资建议**;只读行情、不下单。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from tools.analysis.market_forecast import breadth as B
from tools.collectors import calendar as cal
from tools.collectors import gtimg_quote
from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("pipeline.noon_fullA_screen")

SCRIPT_VERSION = "1.0.0"
SOURCE = "qt.gtimg.cn"
SLOT = "T1145_full"                 # 午休全A快照槽位(名义 11:45,冻结价=11:30 收盘)
FREEZE_LABEL = "11:30 午休冻结"

OUT_ROOT = settings.PROJECT_ROOT / "data" / "intraday"
SNAPSHOT_NAME = "T1145_full.json"           # 全A午盘快照(冻结原始行情)
SCREEN_JSON_NAME = "noon_fullA_screen.json"  # 重筛结构化产物(候选+因子+过滤)
SELECTION_DIR = settings.PROJECT_ROOT / "docs" / "每日分析" / "选股"
# 产出 md 前缀。**刻意区别于** intraday_screen.py 的 `日内全A_<date>.md`(LLM 深研判)与
# 盯盘研判的 `日内_<date>.md`——本模块是纯量价轻筛,独立文件名并存、互不覆盖。
MD_PREFIX = "午盘全A选股"
LOG_PATH = settings.PROJECT_ROOT / "logs" / "noon_fullA_screen.log"

# 机读 PICKS 锚点(与 intraday_snapshot.parse_pick_codes 同格式:HTML 注释,给人不可见、给机器权威)。
_ANCHOR_TMPL = "<!-- PICKS: {codes} -->"

# ————————————————————————————————————————————————
# 配置(集中真源 THRESHOLDS['午盘全A重筛'];读不到 → 硬默认)
# ————————————————————————————————————————————————
_HARD_DEFAULTS = {
    "流动性门_半日成交额万元下限": 5000.0,   # 半日成交额 ≥ 5000 万元(万元口径,gtimg amount_wan)
    "强势候选条数": 8,                       # 「午盘全A重筛·强势候选」输出条数(主评价对象)
    "已封板观察条数": 12,                     # 「已封板·观察次日」旁列留痕条数
    # 三类量价维的合成权重(和会被归一化,不必严格 1.0)。动量为主、放量次之、突破再次。
    "权重": {"动量": 0.45, "放量": 0.30, "突破": 0.25},
    # 极端高开阈值(百分点):集合竞价高开 ≥ 此且贴顶 → 打"高开票·尾盘回踩再参与"标(不追高)。
    "极端高开pct": 5.0,
}


def cfg() -> dict:
    """午盘全A重筛配置(THRESHOLDS['午盘全A重筛']);缺失/异常 → 硬默认(逐键回落)。"""
    base = dict(_HARD_DEFAULTS)
    try:
        from tools.config import strategy as _strategy
        user = _strategy.THRESHOLDS.get("午盘全A重筛", {}) or {}
    except Exception:                                   # noqa: BLE001 配置缺失不阻断重筛
        user = {}
    for k, v in user.items():
        if k == "权重" and isinstance(v, dict):
            merged = dict(_HARD_DEFAULTS["权重"]); merged.update(v); base["权重"] = merged
        else:
            base[k] = v
    return base


# ————————————————————————————————————————————————
# 路径
# ————————————————————————————————————————————————
def snapshot_path(date: str, *, out_root: Path | None = None) -> Path:
    """全A午盘快照落盘路径 data/intraday/<date>/T1145_full.json。"""
    return (out_root or OUT_ROOT) / date / SNAPSHOT_NAME


def screen_json_path(date: str, *, out_root: Path | None = None) -> Path:
    """重筛结构化产物路径 data/intraday/<date>/noon_fullA_screen.json。"""
    return (out_root or OUT_ROOT) / date / SCREEN_JSON_NAME


def md_path(date: str, *, out_dir: Path | None = None) -> Path:
    """产出 md 路径 docs/每日分析/选股/午盘全A选股_<date>.md。"""
    return (out_dir or SELECTION_DIR) / f"{MD_PREFIX}_{date}.md"


def _write_atomic(path: Path, payload: dict) -> None:
    """先写 .tmp 再 replace —— 避免下游读到写一半的 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ————————————————————————————————————————————————
# 全A午盘快照(冻结原始行情)
# ————————————————————————————————————————————————
def universe_codes() -> list[str]:
    """全A代码(复用 backtest.screen_forward_common:已落地主档、离线、排北交所)。

    刻意不走 collectors.universe(打 akshare):午盘要快/经济,用已落地主档即可。
    """
    from tools.backtest import screen_forward_common as sfc
    return sfc.universe_codes()


def capture_snapshot(date: str, *, codes: list[str] | None = None,
                     quotes: dict | None = None, force: bool = False,
                     out_root: Path | None = None) -> tuple[dict, Path]:
    """冻结全A午盘快照并落盘。返回 (payload, path)。

    幂等:同日快照已存在且非 force → 直接读回不重抓(保持"文件存在 ⇒ 内容可用")。
    防未来:`captured_at` 写**真实抓取时刻**;快照只含此刻及之前的行情。
    quotes 可注入(测试/联调,免联网);None → 现拉 gtimg。
    """
    path = snapshot_path(date, out_root=out_root)
    if path.exists() and not force and quotes is None:
        logger.info("全A午盘快照已存在,直接读回(要重抓加 force):%s", path)
        return json.loads(path.read_text(encoding="utf-8")), path

    codes = codes if codes is not None else universe_codes()
    captured_at = datetime.now().astimezone()          # 真实抓取时刻(抓取前取,不事后编)
    if quotes is None:
        logger.info("拉全A午盘快照:%d 只(gtimg)", len(codes))
        quotes = gtimg_quote.fetch_quotes(codes)
    quotes = {c: q for c, q in quotes.items() if q and q.get("price") is not None}
    coverage = (len(quotes) / len(codes)) if codes else 0.0
    payload = {
        "date": date, "slot": SLOT, "freeze_label": FREEZE_LABEL,
        "captured_at": captured_at.isoformat(timespec="seconds"),
        "quotes": quotes,
        "meta": {
            "source": SOURCE, "script_version": SCRIPT_VERSION,
            "script": "tools.pipeline.noon_fullA_screen",
            "universe_n": len(codes), "sampled_n": len(quotes),
            "coverage": round(coverage, 4),
            "note": "全A午盘冻结快照(≤11:30);price=当日临时收盘(11:30)。研究模拟,非投资建议。",
        },
    }
    _write_atomic(path, payload)
    logger.info("全A午盘快照落盘 → %s(%d/%d 只,覆盖 %.0f%%,captured_at=%s)",
                path, len(quotes), len(codes), coverage * 100, payload["captured_at"])
    return payload, path


# ————————————————————————————————————————————————
# 量价因子(只吃单票快照 quote;不联网、不读历史)
# ————————————————————————————————————————————————
def compute_factors(quote: dict, *, prev_high: float | None = None) -> dict | None:
    """从一只票的 gtimg 午盘快照算量价因子。price/prev_close 缺失 → None(不猜)。

    因子(全部 ≤11:30 冻结,单位见注释):
      半日涨幅   pct_chg(%)                         —— 动量
      贴日内高   price/high ∈(0,1],越近 1 越强(不回落) —— 动量(质量)
      量比       vol_ratio(源方给,半日可比性好)      —— 放量
      换手率     turnover(%,半日累计,系统性偏低)     —— 放量
      集合竞价高开 (open−prev_close)/prev_close×100(%) —— 突破(开盘强度)
      日内位置   (price−low)/(high−low)∈[0,1],越近 1 越"上午没破位" —— 突破/防守
      突破昨高   prev_high 给定时:high>prev_high 且 price>prev_high → 1 否则 0(可选,板块线接入前默认缺省)
    """
    price = quote.get("price")
    prev_close = quote.get("prev_close")
    if price is None or prev_close in (None, 0):
        return None
    price = float(price); prev_close = float(prev_close)
    open_ = quote.get("open"); high = quote.get("high"); low = quote.get("low")
    open_ = float(open_) if open_ is not None else price
    high = float(high) if high is not None else max(price, open_)
    low = float(low) if low is not None else min(price, open_)

    pct = quote.get("pct_chg")
    pct = float(pct) if pct is not None else (price / prev_close - 1.0) * 100.0
    close_to_high = price / high if high > 0 else None
    gap_open = (open_ - prev_close) / prev_close * 100.0
    rng = high - low
    intraday_pos = (price - low) / rng if rng > 0 else 1.0
    break_prev_high = None
    if prev_high is not None:
        break_prev_high = 1.0 if (high > float(prev_high) and price > float(prev_high)) else 0.0

    return {
        "半日涨幅": round(pct, 3),
        "贴日内高": round(close_to_high, 4) if close_to_high is not None else None,
        "量比": round(float(quote["vol_ratio"]), 3) if quote.get("vol_ratio") is not None else None,
        "换手率": round(float(quote["turnover"]), 3) if quote.get("turnover") is not None else None,
        "集合竞价高开": round(gap_open, 3),
        "日内位置": round(intraday_pos, 4),
        "突破昨高": break_prev_high,
        "成交额万元": round(float(quote["amount_wan"]), 1) if quote.get("amount_wan") is not None else None,
        "现价": round(price, 3),
    }


# ————————————————————————————————————————————————
# 横截面打分(百分位,对"全体半日偏低"不敏感)
# ————————————————————————————————————————————————
def _pct_rank(values: list[float | None]) -> list[float | None]:
    """横截面百分位排名 ∈[0,1](并列取平均秩);None 保持 None(不参与、不沉底为 0)。

    经验分布:val 的秩 = 严格小于它的个数 + 0.5×等于它的个数,除以有效样本数。
    单样本 → 0.5(无区分度)。这样打分只反映"相对强弱",天然免疫量能半日系统性偏低。
    """
    idx_val = [(i, v) for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    n = len(idx_val)
    if n == 0:
        return out
    if n == 1:
        out[idx_val[0][0]] = 0.5
        return out
    vals = [v for _, v in idx_val]
    for i, v in idx_val:
        less = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        out[i] = (less + 0.5 * equal) / n
    return out


def _score_pool(rows: list[dict], weights: dict) -> None:
    """给打分池内每行**原地**写入 动量分/放量分/突破分/量价综合分(0~100)。

    三类维各由若干因子的横截面百分位均值构成;综合分 = 三类维加权(权重归一化)后 ×100。
    某因子全池皆 None → 该因子不计;某维无任何有效因子 → 该维记 None、权重顺移到其余维。
    """
    if not rows:
        return
    # 每个因子的横截面百分位(在打分池内)。
    ranks: dict[str, list[float | None]] = {}
    for key in ("半日涨幅", "贴日内高", "量比", "换手率", "集合竞价高开", "日内位置", "突破昨高"):
        ranks[key] = _pct_rank([r["factors"].get(key) for r in rows])

    def _dim(i: int, keys: list[str]) -> float | None:
        vals = [ranks[k][i] for k in keys if ranks[k][i] is not None]
        return sum(vals) / len(vals) if vals else None

    for i, r in enumerate(rows):
        动量 = _dim(i, ["半日涨幅", "贴日内高"])
        放量 = _dim(i, ["量比", "换手率"])
        突破 = _dim(i, ["集合竞价高开", "日内位置", "突破昨高"])
        parts = [("动量", 动量), ("放量", 放量), ("突破", 突破)]
        wsum = sum(weights.get(name, 0.0) for name, v in parts if v is not None)
        if wsum > 0:
            composite = sum(weights.get(name, 0.0) * v for name, v in parts if v is not None) / wsum
        else:
            composite = 0.0
        r["动量分"] = round(动量 * 100, 2) if 动量 is not None else None
        r["放量分"] = round(放量 * 100, 2) if 放量 is not None else None
        r["突破分"] = round(突破 * 100, 2) if 突破 is not None else None
        r["量价综合分"] = round(composite * 100, 2)


# ————————————————————————————————————————————————
# 过滤 + 打分 + 排序(纯函数:只吃 quotes,不联网、不读当日/未来)
# ————————————————————————————————————————————————
def is_limit_up(code: str, quote: dict) -> bool:
    """午盘是否已涨停封板(复用 breadth.is_limit_hit 启发式,与历史广度同口径)。

    pct 缺失时由 price/prev_close 推;任一入参缺 → is_limit_hit 判 False(缺失就是缺失)。
    """
    price = quote.get("price"); high = quote.get("high"); low = quote.get("low")
    pct = quote.get("pct_chg")
    if pct is None and quote.get("prev_close"):
        pct = (float(price) / float(quote["prev_close"]) - 1.0) * 100.0 if price is not None else None
    return B.is_limit_hit(code, pct, price, high, low, up=True)


def _name_of(code: str, quote: dict) -> str:
    return quote.get("name") or ""


def screen(quotes: dict, *, as_of: str, exclude_codes: set[str] | None = None,
           prev_high_map: dict | None = None, conf: dict | None = None) -> dict:
    """在全A午盘快照上做量价重筛。**纯函数**:只吃 quotes(+可选历史昨高),不联网、不读未来。

    步骤:
      1. 逐票算因子;price/prev_close 缺 → 丢(记 skipped)。
      2. **涨停剔除**:午盘已封板 → 移出可买候选,进「已封板·观察次日」旁列(够不着=不算数)。
      3. **去重盯盘集**:exclude_codes(当日盯盘集)命中 → 移出(避免与盯盘研判重复)。
      4. **流动性门**:半日成交额 < 下限 → 移出打分池(流动性不足不纳)。
      5. 剩余为**打分池**,横截面百分位打分(动量/放量/突破),按量价综合分降序 → 强势候选。

    返回 dict:候选/已封板/过滤计数/参数/市场环境(全部 ≤11:30 冻结)。
    """
    conf = conf or cfg()
    exclude_codes = exclude_codes or set()
    prev_high_map = prev_high_map or {}
    liq_min = float(conf.get("流动性门_半日成交额万元下限", 5000.0))
    weights = conf.get("权重", _HARD_DEFAULTS["权重"])

    sealed: list[dict] = []          # 已封板·观察次日(涨停剔除)
    pool: list[dict] = []            # 打分池(可买候选)
    skipped = 0
    n_dedup = 0
    n_illiq = 0

    for code, q in quotes.items():
        if not q or q.get("price") is None:
            skipped += 1
            continue
        f = compute_factors(q, prev_high=prev_high_map.get(code))
        if f is None:
            skipped += 1
            continue
        row = {"code": code, "name": _name_of(code, q), "factors": f}
        if is_limit_up(code, q):                       # ② 涨停不可买 → 旁列留痕
            row["封板"] = True
            sealed.append(row)
            continue
        if code in exclude_codes:                       # ③ 与盯盘集去重
            n_dedup += 1
            continue
        amt = f.get("成交额万元")
        if amt is None or amt < liq_min:                # ④ 流动性门
            n_illiq += 1
            continue
        pool.append(row)

    _score_pool(pool, weights)                          # ⑤ 横截面打分
    pool.sort(key=lambda r: (r.get("量价综合分") if r.get("量价综合分") is not None else -1.0),
              reverse=True)
    # 已封板旁列按半日涨幅降序(纯留痕,不打综合分)。
    sealed.sort(key=lambda r: (r["factors"].get("半日涨幅") or float("-inf")), reverse=True)

    # 极端高开标注(倾向对齐:不追高开涨停;强势票尾盘回踩再参与)。
    extreme_gap = float(conf.get("极端高开pct", 5.0))
    for r in pool:
        gap = r["factors"].get("集合竞价高开")
        pos = r["factors"].get("日内位置")
        r["尾盘参考限价"] = r["factors"].get("现价")   # 回踩不追高:限价≈现价或更低
        r["高开回踩标"] = bool(gap is not None and gap >= extreme_gap
                             and pos is not None and pos >= 0.8)

    breadth = _breadth_from_quotes(quotes)
    return {
        "as_of": as_of, "slot": SLOT, "freeze_label": FREEZE_LABEL,
        "候选": pool, "已封板": sealed,
        "市场环境": breadth,
        "过滤": {"总票": len(quotes), "打分池": len(pool), "已封板剔除": len(sealed),
                "盯盘集去重": n_dedup, "流动性门剔除": n_illiq, "无效跳过": skipped,
                "流动性下限万元": liq_min},
        "权重": weights,
    }


def _breadth_from_quotes(quotes: dict) -> dict:
    """从午盘快照算全市场广度(涨/跌/平、中位涨幅、涨停家数)——只用 ≤11:30 冻结数据。"""
    ups = downs = flats = 0
    pcts: list[float] = []
    lu = 0
    for code, q in quotes.items():
        p = q.get("pct_chg")
        if p is None and q.get("prev_close") and q.get("price") is not None:
            p = (float(q["price"]) / float(q["prev_close"]) - 1.0) * 100.0
        if p is not None:
            pcts.append(float(p))
            if p > 0:
                ups += 1
            elif p < 0:
                downs += 1
            else:
                flats += 1
        if is_limit_up(code, q):
            lu += 1
    med = None
    if pcts:
        s = sorted(pcts)
        n = len(s)
        med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return {"上涨": ups, "下跌": downs, "平盘": flats, "样本": len(pcts),
            "中位涨幅": round(med, 3) if med is not None else None, "涨停家数": lu}


# ————————————————————————————————————————————————
# 盯盘集(当日已盯的票)—— 复用 intraday_snapshot 的标的解析,与盯盘研判去重
# ————————————————————————————————————————————————
def watch_set(date: str) -> set[str]:
    """当日盯盘集(上一交易日选股 md ∪ 自选池)——复用 intraday_snapshot.resolve_targets。

    任何异常(日历/文件缺失)→ 空集(不阻断重筛,只是不去重)。
    """
    try:
        from tools.pipeline import intraday_snapshot as isnap
        codes, _ = isnap.resolve_targets(date)
        return set(codes)
    except Exception as e:                              # noqa: BLE001 去重失败不阻断
        logger.warning("盯盘集解析失败(%s),本次不去重", e)
        return set()


# ————————————————————————————————————————————————
# 产出:午盘全A选股_<date>.md(+ PICKS 锚点)与结构化 JSON
# ————————————————————————————————————————————————
def _fmt(v, dash: str = "—") -> str:
    return dash if v is None else str(v)


def render_md(as_of: str, result: dict, *, out_dir: Path | None = None) -> Path:
    """把重筛结果渲染为 `午盘全A选股_<date>.md`(强势候选表 + 已封板旁列 + PICKS 锚点)。"""
    conf = cfg()
    n_strong = int(conf.get("强势候选条数", 8))
    n_sealed = int(conf.get("已封板观察条数", 12))
    候选 = result.get("候选", [])
    已封板 = result.get("已封板", [])
    strong = 候选[:n_strong]
    breadth = result.get("市场环境", {})
    过滤 = result.get("过滤", {})

    pick_codes = [r["code"] for r in strong]
    lines: list[str] = []
    lines.append(_ANCHOR_TMPL.format(codes=",".join(pick_codes) if pick_codes else "none"))
    lines.append(f"# 午盘全A重筛 · 强势候选 · {as_of}")
    lines.append("")
    lines.append(f"> 口径:**全A午盘量价重筛**(纯量价、不逐票 LLM、板块-agnostic)。数据时点 **{FREEZE_LABEL}**"
                 f"(price=11:30 临时收盘)。**半日 provisional**,尾盘 14:30~14:57 **回踩限价**参与、复核后下手。")
    lines.append("> **防未来 ≤11:30**:只用午盘冻结快照;涨停不可买(已封板剔除);量能仅累计半日,量价因子取"
                 "横截面**百分位**(比相对强弱,免疫半日系统性偏低)。⚠️ 研究模拟,**非投资建议**。")
    lines.append("> 倾向对齐:不反动量、不追高开涨停;强势票尾盘**回踩≤现价不追高**参与。"
                 "板块定向(占优板块选先锋/中军/补涨)由「板块预测系统」另线接入,本表暂**板块-agnostic**。")
    lines.append("")

    # 市场环境
    lines.append("## 市场环境(≤11:30)")
    lines.append(f"- 涨/跌/平:{breadth.get('上涨')}/{breadth.get('下跌')}/{breadth.get('平盘')}"
                 f"(样本 {breadth.get('样本')});中位涨幅 {_fmt(breadth.get('中位涨幅'))}%;"
                 f"涨停家数 {breadth.get('涨停家数')}")
    lines.append(f"- 过滤漏斗:总票 {过滤.get('总票')} → 已封板剔除 {过滤.get('已封板剔除')}、"
                 f"盯盘集去重 {过滤.get('盯盘集去重')}、流动性门剔除 {过滤.get('流动性门剔除')}"
                 f"(半日成交额<{_fmt(过滤.get('流动性下限万元'))}万元)、无效跳过 {过滤.get('无效跳过')} "
                 f"→ 打分池 {过滤.get('打分池')}")
    lines.append("")

    # 强势候选表
    lines.append(f"## 午盘全A重筛 · 强势候选(Top {len(strong)} · 主评价对象)")
    lines.append("")
    lines.append("> 排序=量价综合分(动量 0.45 / 放量 0.30 / 突破 0.25 横截面百分位加权,权重见配置)。"
                 "尾盘参考限价=回踩不追高(≤现价)。「高开回踩」标=集合竞价大幅高开且贴顶,尤需等回踩。")
    lines.append("")
    lines.append("| 序 | 代码 | 名称 | 量价综合分 | 动量 | 放量 | 突破 | 半日涨幅% | 量比 | 换手% | 高开% | 贴日内高 | 尾盘参考限价 | 标注 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(strong, 1):
        f = r["factors"]
        tag = "高开回踩" if r.get("高开回踩标") else ""
        lines.append(
            f"| {i} | {r['code']} | {r.get('name', '')} | {_fmt(r.get('量价综合分'))} "
            f"| {_fmt(r.get('动量分'))} | {_fmt(r.get('放量分'))} | {_fmt(r.get('突破分'))} "
            f"| {_fmt(f.get('半日涨幅'))} | {_fmt(f.get('量比'))} | {_fmt(f.get('换手率'))} "
            f"| {_fmt(f.get('集合竞价高开'))} | {_fmt(f.get('贴日内高'))} "
            f"| {_fmt(r.get('尾盘参考限价'))} | {tag} |")
    if not strong:
        lines.append("| — | — | _本次无强势候选_ | | | | | | | | | | | |")
    lines.append("")

    # 已封板·观察次日
    lines.append(f"## 已封板 · 观察次日(涨停不可买,{min(len(已封板), n_sealed)} 只留痕)")
    lines.append("")
    lines.append("> 午盘已涨停封板 = 尾盘够不着(不进可买候选),仅留痕次日观察是否连板/高开。")
    lines.append("")
    lines.append("| 序 | 代码 | 名称 | 半日涨幅% | 量比 | 换手% |")
    lines.append("|---|---|---|---|---|---|")
    for i, r in enumerate(已封板[:n_sealed], 1):
        f = r["factors"]
        lines.append(f"| {i} | {r['code']} | {r.get('name', '')} | {_fmt(f.get('半日涨幅'))} "
                     f"| {_fmt(f.get('量比'))} | {_fmt(f.get('换手率'))} |")
    if not 已封板:
        lines.append("| — | — | _无封板票_ | | | |")
    lines.append("")

    # 完整候选台账(折叠,全序供复盘取数)
    lines.append("<details>")
    lines.append(f"<summary>完整打分台账(共 {len(候选)} 只)</summary>")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 量价综合分 | 动量 | 放量 | 突破 | 半日涨幅% | 成交额万元 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(候选, 1):
        f = r["factors"]
        lines.append(f"| {i} | {r['code']} | {r.get('name', '')} | {_fmt(r.get('量价综合分'))} "
                     f"| {_fmt(r.get('动量分'))} | {_fmt(r.get('放量分'))} | {_fmt(r.get('突破分'))} "
                     f"| {_fmt(f.get('半日涨幅'))} | {_fmt(f.get('成交额万元'))} |")
    lines.append("")
    lines.append("</details>")
    lines.append("")
    lines.append("## forward 记分(诚实边界)")
    lines.append("> 无历史全A午盘快照 → 本重筛信号**不能严格历史回测**,只能上线后 forward 记分核实"
                 "(尾盘模拟建仓 → 次日绝对收益)。用户已明示正式上线不搞 shadow 门控:先上线产出、并行 forward 复核。")
    lines.append("")

    out = md_path(as_of, out_dir=out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    logger.info("午盘全A重筛 md 写出 → %s(强势候选 %d,已封板 %d)", out, len(strong), len(已封板))
    return out


def persist_json(as_of: str, result: dict, *, out_root: Path | None = None) -> Path:
    """把重筛结果落成结构化 JSON(候选+因子+过滤+市场环境),供复盘/forward 记分机读。"""
    conf = cfg()
    n_strong = int(conf.get("强势候选条数", 8))
    候选 = result.get("候选", [])
    payload = {
        "as_of": as_of, "slot": SLOT, "freeze_label": FREEZE_LABEL,
        "note": "全A午盘量价重筛(≤11:30 冻结,provisional·尾盘复核)。研究模拟,非投资建议。",
        "强势候选代码": [r["code"] for r in 候选[:n_strong]],
        "已封板代码": [r["code"] for r in result.get("已封板", [])],
        "候选": 候选,
        "已封板": result.get("已封板", []),
        "市场环境": result.get("市场环境", {}),
        "过滤": result.get("过滤", {}),
        "权重": result.get("权重", {}),
        "meta": {"script": "tools.pipeline.noon_fullA_screen", "script_version": SCRIPT_VERSION,
                 "强势候选条数": n_strong},
    }
    path = screen_json_path(as_of, out_root=out_root)
    _write_atomic(path, payload)
    logger.info("午盘全A重筛 JSON 落盘 → %s(候选 %d)", path, len(候选))
    return path


# ————————————————————————————————————————————————
# 节点主入口
# ————————————————————————————————————————————————
def run_noon_fullA_screen(as_of: str | None = None, *, codes: list[str] | None = None,
                          quotes: dict | None = None, force: bool = False,
                          exclude_codes: set[str] | None = None,
                          prev_high_map: dict | None = None,
                          write_md: bool = True, persist: bool = True,
                          out_root: Path | None = None,
                          out_dir: Path | None = None) -> dict:
    """午盘全A量价重筛节点主入口(快照 → 重筛 → 产出 md+JSON)。

    Args:
        as_of: 目标交易日(None → store active_date 或今日)。
        codes/quotes: 全A代码/预取快照(测试/联调可注入;None → 现拉)。
        force: 快照已存在时强制重抓。
        exclude_codes: 去重集(None → 当日盯盘集 watch_set)。
        prev_high_map: {code: 昨日最高}(板块线接入前默认 None → 突破昨高因子缺省)。
        write_md/persist: 是否写 md / JSON(测试可关)。

    返回 screen() 结果 + 产出路径。
    """
    if as_of is None:
        as_of = store.active_date() or store._today()
    store.set_active_date(as_of)

    payload, snap_path = capture_snapshot(as_of, codes=codes, quotes=quotes,
                                          force=force, out_root=out_root)
    snap_quotes = payload["quotes"]
    if exclude_codes is None:
        exclude_codes = watch_set(as_of)

    result = screen(snap_quotes, as_of=as_of, exclude_codes=exclude_codes,
                    prev_high_map=prev_high_map)
    result["快照落盘"] = str(snap_path)
    result["盯盘集去重数"] = len(exclude_codes)

    if write_md:
        result["产出md"] = str(render_md(as_of, result, out_dir=out_dir))
    if persist:
        result["产出json"] = str(persist_json(as_of, result, out_root=out_root))

    logger.info("午盘全A量价重筛完成:as_of=%s,快照 %d 只,强势候选 %d(共打分池 %d),已封板 %d",
                as_of, len(snap_quotes), min(len(result["候选"]), int(cfg().get("强势候选条数", 8))),
                len(result["候选"]), len(result["已封板"]))
    return result


# ————————————————————————————————————————————————
# CLI
# ————————————————————————————————————————————————
def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="午盘全A量价重筛(11:30 冻结口径,纯量价、快/经济)")
    ap.add_argument("--date", default=None, help="目标交易日 YYYY-MM-DD(默认今日)")
    ap.add_argument("--force", action="store_true", help="快照已存在也强制重抓;并跳过交易日判断")
    ap.add_argument("--no-md", action="store_true", help="不写 午盘全A选股_<date>.md")
    ap.add_argument("--no-json", action="store_true", help="不写结构化 JSON")
    args = ap.parse_args(argv)
    _setup_logging()
    as_of = args.date or store._today()
    if not args.force and not cal.is_trading_day(as_of):
        logger.info("非交易日 %s,午盘全A重筛跳过(退 0)", as_of)
        return 0
    try:
        rep = run_noon_fullA_screen(as_of, force=args.force,
                                    write_md=not args.no_md, persist=not args.no_json)
    except Exception as e:                              # noqa: BLE001 兜底进日志,非 0 退出
        logger.exception("午盘全A重筛异常退出:%s: %s", type(e).__name__, e)
        return 1
    logger.info("完成:%s", {k: rep.get(k) for k in ("as_of", "产出md", "产出json") if k in rep})
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
