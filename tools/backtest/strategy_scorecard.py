"""全策略上线以来·分策略成绩记分卡:每日各策略 view 的当日预测方向 → 配"到期实现收益" → 命中率。

与 forward_scorecard 的区别:后者是**逐票 record**(点位情绪/趋势评级)的前向记分;本模块是
**逐策略 view**(`data/analysis/<日期>/<策略>.json` 的选股清单)的方向命中校验,用于回收
每个**已上线策略**上线以来的 1日/5日 成绩,并挑"回测差 ∧ 观测也差"的差生候选。

数据源:
  · 预测 = 每日策略 view JSON。`as_of` = 预测日 T;清单里每个 code = 当日预测标的。
    多数策略是纯多头选股(选出=看多,方向 +1);`策略0合议` 每票带 `综合方向`,
    `指标条件化状态排序` 的 `排行` 按 1日/5日/10日 分组、每票带 `方向`。
  · 实现收益 = tools.collectors.market.load_kline(code) 的前复权 close/high/low。
    预测日定位为 kline 里 date==T 的下标 idx;horizon h 实现收益 = close[idx+h]/close[idx]-1。

三条硬规则(见 docs/计划/策略成绩记分卡计划.md,单测锁死):
  1. 每个 (预测日, 期限) 独立评判 + 防未来函数:h 仅当 idx+h < len(kline) 才算已到期,否则
     pending 排除出统计。kline 最大日期 = 最近交易日 ≤ 今天 → 天然无未来函数。
  2. 中途翻转不反算:逐 (日期) 独立评估,T 日预测只对 T→T+h 窗口打分,后续日的另一次预测不回溯。
  3. 5日"涨"命中双口径:期末(close[idx+h]/close[idx]-1 方向感知)+ 期内(触及:5日内 max(high)>入场
     或 min(low)<入场,方向感知)。1日只报期末。

无未来函数:预测清单只读信号日 T 落盘的 view(策略自身已保证只用 ≤T 数据);实现收益取 T 之后价。
产物只写 worktree 本地。非投资建议。

用法:python -m tools.backtest.strategy_scorecard [--analysis-dir DIR] [--out-json PATH]
                                                 [--out-md PATH] [--horizon 1,5]
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import os

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.config import settings
from tools.store import repo as store


def _load_universe() -> list[str]:
    """base-rate 宇宙:全 master 已落地代码(稳定基线)。缺失时返回空(窗口分层降级为无基准)。"""
    try:
        return store.list_master_codes()
    except Exception as e:   # noqa: BLE001
        logger.warning("list_master_codes 失败,base-rate 宇宙为空: %s", str(e)[:60])
        return []

logger = logging.getLogger("backtest.strategy_scorecard")

# 方向文案 → 符号。看涨/看多 +1;看跌/看空 -1;中性 0(0/None 不计命中)。
_DIR = {"看多": 1, "看涨": 1, "多": 1, "看空": -1, "看跌": -1, "空": -1, "中性": 0}

# 已上线策略登记表:文件名(去 .json) → (策略ID, 展示名)。用于报告归类与稳定命名。
# 不在表内的 view 文件也会被通用提取器兜底处理(ID 用文件名),保证新策略不漏。
STRATEGY_SPECS: dict[str, tuple[str, str]] = {
    "策略0合议": ("0", "多专家合议(全A)"),
    # S01/箱体3 已下线(见 _RETIRED_STEMS,扫描时跳过);登记项仅留作历史 view 命名。
    "趋势深跌反包": ("S01", "趋势深跌反包"),
    "放量后缩量回踩": ("S02", "放量后缩量回踩"),
    "箱体形态": ("3", "箱体形态"),
    "动量组合": ("4", "动量组合(A腿)"),
    "半导体多因子": ("5", "半导体多因子"),
    "最强选股": ("9", "最强选股"),
    "反转低换手组合": ("10", "反转低换手组合"),
    "指标条件化状态排序": ("11", "指标条件化状态排序"),
    "最大范围选股": ("S03", "最大范围选股"),
    "量价放量": ("S04", "量价放量"),
    "SEPA合格池": ("SEPA-合格", "SEPA 趋势模板·合格池"),
    "SEPA观察池": ("SEPA-观察", "SEPA 趋势模板·观察池"),
    "形态选股": ("形态", "形态选股(RS/杯柄等)"),
}

# 非策略 view 的文件名前缀/名(逐票 record、面板、情绪等),跳过。
_SKIP_STEMS = {"backtest", "panel", "screen", "sentiment", "sentiment_policy",
               "factor", "news_ai", "SEPA雷达", "形态选股回测汇总", "选股分析报告"}

# 已下线策略 view stem:全史深诊断显著负,已从生产链路摘除,记分卡不再当在产策略统计
# (即便历史目录仍有其 view 也跳过)。screener 代码存档保留;诊断见 docs/计划/。
_RETIRED_STEMS = {"趋势深跌反包", "趋势深跌反包回测", "箱体形态", "箱体形态回测"}

# 各策略"选股清单"可能的键名(按优先级)。通用提取器还会兜底扫 list[dict(code)]。
_PICK_KEYS = ("入选清单", "top", "rows", "排行", "达标清单", "达标")

_THIN_N = 30   # 样本 < 此阈值 → 标"薄样本,不足为凭"

# 滚动时间窗:窗名 → 交易日数 N。按"预测日 T ∈ [最新交易日−N+1, 最新交易日]"划窗。
# 观测目前仅约 3 周(≈19 交易日),近一季/近一年数据远不足 → 报告如实标注"数据不足"。
WINDOWS: dict[str, int] = {"近一周": 5, "近一月": 20, "近一季": 60, "近一年": 250}

# 交易日历参考票(高流动、几乎每日成交):用其 kline 的 date 列近似全市场交易日历,
# 定"最新交易日"与"最近 N 个交易日"。取并集抗个别票停牌缺日。
_CALENDAR_REF_CODES = ("000001", "600000", "600519", "000002", "601318", "600036")

# 既有回测结论(权威来源:docs/策略/策略总览_定义计算与回测.md 一页总表 + 各策略回测效果段)。
# 只做"差生候选"的**回测维度门**,不作单独判据。含 "弱"/"差" 视为回测偏弱(🔴 或经济上极小/不显著)。
DEFAULT_BACKTEST_VERDICTS: dict[str, str] = {
    "0": "弱:IC 5/10/20日均不显著微负,无 edge(可带 alpha 的资金流/情绪/基本面维度全弃权)",
    "S01": "弱:旧口径胜率12.6%、均−0.34%、p=0.15 显著平庸,参数敏感性判定救不活",
    "S02": "中:子集胜率31.6%、Alpha+0.14% 略正但不足定论(非全A alpha,回测未判负)",
    "3": "弱:胜率31%、Alpha+0.32% 经济上极小大概率不显著、即死61%",
    "4": "弱:IC 5/10/20日显著为负(A股短期反转,选强动量反跑输)",
    "S03": "中:样本级 T+10 均+1.13%>baseline 但胜率偏低、非全A alpha",
    "10": "中:前向观测中,可交易池5-10日net转正但有幸存者偏差水分,未定论",
    "11": "弱:聚合无 alpha(条件化 Brier≈无条件、聚类t不显著),仅作状态参考",
    "SEPA-合格": "弱:聚合无正向 edge(超额 T+10 −1.33%/T+20 −2.11%、胜率38–45%),宜作 regime 前置门非独立 alpha",
    "SEPA-观察": "弱:聚合无正向 edge(同趋势模板),宜作 regime 前置门非独立 alpha",
}

_DEFAULT_ANALYSIS = str(settings.PROJECT_ROOT / "data" / "analysis")
_DEFAULT_JSON = str(settings.PROJECT_ROOT / "data" / "analysis" / "backtest" / "strategy_scorecard.json")
_DEFAULT_MD = str(settings.PROJECT_ROOT / "docs" / "策略成绩报告.md")


# ────────────────────────────── 预测提取 ──────────────────────────────
def _dir_from(obj: dict) -> int:
    """从一个 pick/view dict 里解析方向符号:综合方向/方向 文案 → ±1/0;缺失 → +1(纯多头选股默认看多)。"""
    for k in ("综合方向", "方向"):
        v = obj.get(k)
        if v is not None:
            return _DIR.get(str(v), 0)
    return 1


def extract_picks(view: dict) -> list[dict]:
    """从一个策略 view JSON 提取当日预测清单:返回 [{code, dir, horizons}]。

    · 普通清单(入选清单/top/rows/…):每票 dir 读该票或整表的方向文案,缺失→+1(多头选股)。
      horizons=None 表示该预测对所有 horizon 生效(同一方向)。
    · 排行(指标条件化):dict{1日/5日/10日→list},每票每 horizon 独立方向,horizons=[该期]。
    """
    picks: list[dict] = []
    top_dir_txt = view.get("方向")   # 整表方向(如 最强选股/量价放量:'看多')
    top_dir = _DIR.get(str(top_dir_txt), None) if top_dir_txt is not None else None

    # ---- 排行式(按 horizon 分组,每票每期独立方向)----
    rank = view.get("排行")
    if isinstance(rank, dict) and any(str(h).endswith("日") for h in rank):
        for hkey, lst in rank.items():
            try:
                h = int(str(hkey).replace("日", ""))
            except ValueError:
                continue
            if not isinstance(lst, list):
                continue
            for it in lst:
                if isinstance(it, dict) and it.get("code"):
                    picks.append({"code": str(it["code"]), "dir": _dir_from(it), "horizons": [h]})
        return picks

    # ---- 普通清单:取第一个 list[dict(code)] 键 ----
    lst = None
    for k in _PICK_KEYS:
        v = view.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("code"):
            lst = v
            break
    if lst is None:   # 兜底:扫任意 list[dict(code)]
        for v in view.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("code"):
                lst = v
                break
    if not lst:
        return picks

    for it in lst:
        code = it.get("code")
        if not code:
            continue
        d = _dir_from(it)
        if d == 1 and top_dir is not None:   # 单票无方向字段时,用整表方向兜底(仍多为 +1)
            d = top_dir if "综合方向" not in it and "方向" not in it else d
        picks.append({"code": str(code), "dir": d, "horizons": None})
    return picks


# ────────────────────────────── 实现收益 ──────────────────────────────
class KlineBook:
    """按 code 缓存 kline + date→idx 映射(一次加载多次查),避免逐票重复读盘。"""

    def __init__(self, loader=market.load_kline):
        self._loader = loader
        self._cache: dict[str, tuple] = {}   # code → (close[], high[], low[], {date:idx}) 或 None

    def get(self, code: str):
        if code not in self._cache:
            try:
                df = self._loader(code).reset_index(drop=True)
                idx = ({str(x)[:10]: i for i, x in enumerate(df["date"].tolist())}
                       if "date" in df.columns else {})
                self._cache[code] = (df["close"].to_numpy(float),
                                     df["high"].to_numpy(float),
                                     df["low"].to_numpy(float), idx)
            except Exception as e:   # noqa: BLE001
                logger.debug("kline 加载失败 %s: %s", code, str(e)[:60])
                self._cache[code] = None
        return self._cache[code]


def forward_returns(code: str, date: str, direction: int, horizons, book: KlineBook) -> dict:
    """算某票在预测日 date、方向 direction 下各 horizon 的实现收益与命中(方向感知,双口径)。

    返回 {h: {matured, r, hit_end, hit_intra}}。matured=False(pending/无数据)→ r/hit 全 None。
    无未来函数:h 仅当 idx+h < len(close) 才 matured(kline 无未来价)。direction=0 → hit 记 None。
    """
    out = {h: {"matured": False, "r": None, "hit_end": None, "hit_intra": None} for h in horizons}
    rec = book.get(code)
    if rec is None:
        return out
    close, high, low, dmap = rec
    idx = dmap.get(str(date)[:10])
    if idx is None or close[idx] <= 0:
        return out
    entry = close[idx]
    for h in horizons:
        if idx + h >= len(close):
            continue   # 未到期 → pending,排除
        r = float(close[idx + h] / entry - 1.0) * 100.0
        cell = out[h]
        cell["matured"] = True
        cell["r"] = r
        if direction == 0:
            continue   # 中性预测不计方向命中
        cell["hit_end"] = int(np.sign(r) == np.sign(direction))
        # 期内(触及,方向感知):看多=窗口内 max(high)>入场;看空=min(low)<入场
        win_hi = float(np.max(high[idx + 1:idx + h + 1]))
        win_lo = float(np.min(low[idx + 1:idx + h + 1]))
        cell["hit_intra"] = int(win_hi > entry) if direction > 0 else int(win_lo < entry)
    return out


# ────────────────────────────── 逐票记分 ──────────────────────────────
def _iter_strategy_files(analysis_dir: str, dates=None):
    """遍历 analysis_dir 下各日期目录的策略 view 文件,yield (date, stem, path)。跳过非策略文件。"""
    root = analysis_dir
    if not os.path.isdir(root):
        return
    for d in sorted(os.listdir(root)):
        ddir = os.path.join(root, d)
        if not (os.path.isdir(ddir) and d[:4].isdigit()):
            continue
        if dates is not None and d not in dates:
            continue
        for fn in sorted(os.listdir(ddir)):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5]
            if stem in _SKIP_STEMS or stem in _RETIRED_STEMS or (len(stem) == 6 and stem.isdigit()):
                continue   # 逐票 record / 面板 / 情绪 / 已下线策略(S01/箱体3)
            yield d, stem, os.path.join(ddir, fn)


def build_rows(analysis_dir=_DEFAULT_ANALYSIS, dates=None, horizons=(1, 5),
               book: KlineBook | None = None) -> pd.DataFrame:
    """扫全 analysis_dir → 逐 (策略, 日期, 票, horizon) 一行,回填已到期实现收益 + 双口径命中。

    只处理登记表内或含选股清单的 view;缺 as_of 时用目录名作预测日。pending 行也保留(matured=False),
    便于自审统计 pending 占比,聚合时再按 matured 过滤。
    """
    book = book or KlineBook()
    rows = []
    for d, stem, path in _iter_strategy_files(analysis_dir, dates):
        if stem not in STRATEGY_SPECS and stem not in _PICK_KEYS:
            # 未登记文件:仍尝试解析,但只有能提出 picks 才纳入(通用兜底)
            pass
        try:
            view = json.load(open(path, encoding="utf-8"))
        except Exception as e:   # noqa: BLE001
            logger.warning("读取 %s 失败: %s", path, str(e)[:60])
            continue
        if not isinstance(view, dict):
            continue
        pred_date = str(view.get("as_of") or d)[:10]
        picks = extract_picks(view)
        if not picks:
            continue
        sid, sname = STRATEGY_SPECS.get(stem, (stem, stem))
        for p in picks:
            code, direction = p["code"], p["dir"]
            hs = p["horizons"] if p["horizons"] is not None else list(horizons)
            fr = forward_returns(code, pred_date, direction, hs, book)
            for h in hs:
                if h not in horizons:
                    continue
                cell = fr.get(h, {})
                rows.append({
                    "strategy_id": sid, "strategy": sname, "file": stem,
                    "date": pred_date, "code": code, "dir": direction, "h": h,
                    "matured": bool(cell.get("matured")),
                    "r": cell.get("r"), "hit_end": cell.get("hit_end"),
                    "hit_intra": cell.get("hit_intra"),
                })
    cols = ["strategy_id", "strategy", "file", "date", "code", "dir", "h",
            "matured", "r", "hit_end", "hit_intra"]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["strategy_id", "date", "code", "h"]).reset_index(drop=True) if not df.empty else df


# ────────────────────────────── 聚合 ──────────────────────────────
def _rate(series) -> float | None:
    s = series.dropna()
    return round(float(s.mean()) * 100, 1) if len(s) else None


def aggregate(rows: pd.DataFrame, horizons=(1, 5)) -> dict:
    """逐策略聚合成绩:样本量(1d/5d)、1日命中率、5日命中率(期末/期内)、薄样本标注。

    只统计**已到期 + 方向非中性(hit_end 非空)**的行。0/中性预测不计入命中分母。
    """
    result = {"生成于": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
              "口径": "方向命中;已到期且方向非中性才计;5日双口径(期末close/期内触及);非投资建议",
              "策略": {}}
    if rows.empty:
        return result
    for sid, g in rows.groupby("strategy_id"):
        name = g["strategy"].iloc[0]
        entry = {"策略名": name, "预测日数": int(g["date"].nunique()),
                 "日期范围": [g["date"].min(), g["date"].max()],
                 "总提示票次": int(len(g[g["h"] == g["h"].iloc[0]])) if len(g) else 0}
        thin_flags = []
        for h in horizons:
            gh = g[(g["h"] == h) & (g["matured"])]
            scored = gh.dropna(subset=["hit_end"])   # 方向非中性
            n = int(len(scored))
            pending = int((g[g["h"] == h]["matured"] == False).sum())  # noqa: E712
            cell = {"已到期样本": n, "pending": pending,
                    "命中率%_期末": _rate(scored["hit_end"]),
                    "平均实现收益%": round(float(scored["r"].mean()), 2) if n else None}
            if h == 5:
                cell["命中率%_期内触及"] = _rate(scored["hit_intra"])
            if 0 < n < _THIN_N:
                thin_flags.append(f"{h}日样本仅{n}")
            elif n == 0:
                thin_flags.append(f"{h}日无已到期方向样本")
            entry[f"{h}日"] = cell
        entry["薄样本"] = "; ".join(thin_flags) if thin_flags else "否"
        result["策略"][sid] = entry
    return result


# ──────────────────── 滚动时间窗 + base-rate 超额命中 ────────────────────
def build_trading_calendar(book: KlineBook, ref_codes=_CALENDAR_REF_CODES) -> list[str]:
    """用高流动参考票 kline 的 date 列并集近似全市场交易日历(升序,YYYY-MM-DD)。

    并集抗个别参考票停牌/缺日;calendar[-1]=最新交易日,calendar[-N:]=最近 N 个交易日。
    """
    dates: set[str] = set()
    for c in ref_codes:
        rec = book.get(c)
        if rec is not None:
            dates.update(rec[3].keys())   # rec[3] = {date_str: idx}
    return sorted(dates)


def build_market_baseline(pred_dates, horizons, universe, book: KlineBook) -> dict:
    """全市场方向基准:对每个 (预测日, 期限 h) 统计**全宇宙**已到期票中 h 日收益>0 的比例的原料。

    返回 {(date, h): [up_count, total_count]}。防未来函数:某票仅当其 kline 存在 idx+h 才计入
    total(与策略侧成熟度规则完全一致);total=0 表示该 (date,h) 全市场都未到期 → 基准率不可算。
    基准为**方向基准**(收益>0 视为"涨",与命中口径同为方向,对齐 5 日期末/1 日期末)。
    """
    base: dict = {(d, h): [0, 0] for d in pred_dates for h in horizons}
    date_set = set(pred_dates)
    for code in universe:
        rec = book.get(code)
        if rec is None:
            continue
        close, _high, _low, dmap = rec
        n = len(close)
        for d in date_set:
            idx = dmap.get(d)
            if idx is None or close[idx] <= 0:
                continue
            for h in horizons:
                if idx + h >= n:
                    continue   # 未到期 → 不计入(防未来函数)
                cell = base[(d, h)]
                cell[1] += 1
                if close[idx + h] > close[idx]:
                    cell[0] += 1
    return base


def _base_rate(dates, h, market_base) -> float | None:
    """对一组预测日 dates、期限 h,池化全市场 up/total → 方向基准率%(该批日的市场上涨占比)。"""
    up = tot = 0
    for d in dates:
        cell = market_base.get((d, h))
        if cell:
            up += cell[0]
            tot += cell[1]
    return round(up / tot * 100, 1) if tot else None


def aggregate_windows(rows: pd.DataFrame, calendar: list[str], market_base: dict,
                      horizons=(1, 5), windows=None) -> dict:
    """按滚动交易日窗分层聚合:每策略 × 每窗 × 每期限 → 命中率 + 市场基准率 + 超额命中。

    · 窗口成员 = 预测日 T ∈ calendar 最近 N 个交易日;数据不足 N 时如实标注"实为全部 M 日"。
    · 每窗只统计**已到期且方向非中性**样本(沿用主聚合成熟度规则)。
    · 基准率与超额:用**该策略在该窗该期限的已到期预测日集合**(完全相同的预测日+期限+防未来函数)
      去全市场算方向基准率;超额命中 = 策略期末命中率 − 基准率(>0 才叫跑赢市场同期)。
    """
    windows = windows or WINDOWS
    out = {"口径": "滚动交易日窗;每窗只计已到期且方向非中性;基准率=同预测日集合全市场方向上涨占比;"
                   "超额命中=策略期末命中率−基准率;非投资建议", "窗口": {}}
    if rows.empty or not calendar:
        for wname, N in windows.items():
            out["窗口"][wname] = {"窗口交易日数N": N, "数据充足": False,
                                  "实际覆盖交易日": 0, "说明": "无数据", "策略": {}}
        return out

    ncal = len(calendar)
    # 观测覆盖的交易日跨度 = 交易日历中 ≥ 最早预测日的交易日数(含最新交易日)。用于判"数据充足否"。
    # 用 bisect 而非 index:最早预测日可能落在**非交易日**(如周末批处理产出的 view),
    # 直接 index 会抛错并误退化成整段历史,导致长窗被误判"数据充足"。
    first_pred = str(rows["date"].min())
    span = ncal - bisect.bisect_left(calendar, first_pred)

    for wname, N in windows.items():
        win_dates = set(calendar[-N:])              # 最近 N 个交易日(不足则全给)
        sufficient = N <= span
        actual = min(N, span)
        note = (f"数据充足(窗内交易日≥{N})" if sufficient
                else f"数据不足 {N} 交易日,实为全部 {actual} 日(观测仅约 {span} 交易日)")
        wentry = {"窗口交易日数N": N, "数据充足": bool(sufficient),
                  "实际覆盖交易日": int(actual), "说明": note, "策略": {}}
        rw = rows[rows["date"].isin(win_dates)]
        for sid, g in rw.groupby("strategy_id"):
            name = g["strategy"].iloc[0]
            sentry = {"策略名": name}
            for h in horizons:
                gh = g[(g["h"] == h) & (g["matured"])]
                scored = gh.dropna(subset=["hit_end"])
                n = int(len(scored))
                pred_days = sorted(scored["date"].unique().tolist())
                brate = _base_rate(pred_days, h, market_base) if n else None
                hit_end = _rate(scored["hit_end"])
                cell = {"已到期样本": n, "预测日数": len(pred_days),
                        "命中率%_期末": hit_end, "基准率%": brate,
                        "超额命中%": (round(hit_end - brate, 1)
                                      if (hit_end is not None and brate is not None) else None)}
                if h == 5:
                    cell["命中率%_期内触及"] = _rate(scored["hit_intra"])
                if 0 < n < _THIN_N:
                    cell["薄样本"] = f"仅{n}"
                sentry[f"{h}日"] = cell
            wentry["策略"][sid] = sentry
        out["窗口"][wname] = wentry
    return out


# ────────────────────────────── 差生候选 ──────────────────────────────
def flag_laggards(agg: dict, backtest_verdicts: dict | None = None, weak_thresh=45.0) -> dict:
    """挑差生:分两档。返回 {"双弱差生候选": [...], "仅观测偏弱提示": [...]}。

    · 双弱差生候选 = "观测 5日期末命中率 < weak_thresh 且样本不薄(≥30)" ∧ "回测结论偏弱(含弱/差)"。
    · 仅观测偏弱提示 = 观测弱但回测结论不弱/缺失 → 只提示,**不判死**(约法:单一维度差别急着判死)。
    样本薄的策略一律不进任一档(不足为凭)。
    """
    bt_map = backtest_verdicts if backtest_verdicts is not None else DEFAULT_BACKTEST_VERDICTS
    double, obs_only = [], []
    for sid, e in agg.get("策略", {}).items():
        c5 = e.get("5日", {})
        n5 = c5.get("已到期样本", 0)
        hit5 = c5.get("命中率%_期末")
        if hit5 is None or n5 < _THIN_N:
            continue   # 样本薄 → 不判死
        if hit5 >= weak_thresh:
            continue
        bt = bt_map.get(sid)
        bt_weak = bt is not None and ("弱" in str(bt) or "差" in str(bt))
        item = {"strategy_id": sid, "策略名": e.get("策略名"),
                "5日期末命中率%": hit5, "5日期内触及%": c5.get("命中率%_期内触及"),
                "样本": n5, "回测结论": bt}
        if bt_weak:
            item["依据"] = "回测偏弱 ∧ 观测5日期末命中低(样本不薄)"
            double.append(item)
        else:
            item["依据"] = "仅观测偏弱(回测结论不弱/缺失)→ 提示,未判死"
            obs_only.append(item)
    return {"双弱差生候选": double, "仅观测偏弱提示": obs_only}


# ────────────────────────────── 产物 ──────────────────────────────
_COL_LEGEND_MAIN = [
    "### 列名说明(先读这个再看表)", "",
    "> 一句话:这张表回答\"某策略当天选出的票,往后 1 个 / 5 个交易日,**涨跌方向猜对了多少**\"。"
    "纯多头选股默认预测\"涨\"(除策略0/11带方向字段)。**命中率=方向准确率,不是收益、更不是 alpha。**", "",
    "| 列名 | 含义 | 怎么读 |",
    "|---|---|---|",
    "| **策略** | 策略编号 + 名称(与选股页 / `strategy.json` 一致) | — |",
    "| **预测日数** | 上线以来该策略累计**在多少个交易日**给出过选股 | 越大=积累越久 |",
    "| **1日样本** | 所有\"预测日 × 选出的票\"里,**已过 1 个交易日、能算实现收益、且方向非中性**的样本数 | **样本 <30 结论不可信** |",
    "| **1日命中%** | 上面 1 日样本里,**次日实际涨跌方向 = 预测方向**的比例 | 方向准确率;≈50% 相当于抛硬币 |",
    "| **5日样本** | 同理,已过 **5 个交易日**、能算、方向非中性的样本数 | 通常远少于 1 日(上线短、多数还没到期) |",
    "| **5日命中%(期末)** | **期末口径(严格)**:只看 T→T+5 **收盘累计收益**方向对不对 | 最能反映\"5 天后真站住了没\" |",
    "| **5日命中%(期内触及)** | **期内口径(宽松)**:5 日窗口内**任意一天触及**预测方向就算对 | 天然远高于期末 |",
    "| **5日均收益%** | 这些 5 日样本的**平均实现收益**本身(不是命中率;负值=平均在跌) | 与命中率互补:看幅度 |",
    "| **薄样本** | 样本太少不足下结论的标注;`None` = 该期限**还没有已到期的方向样本**(全部 pending) | 标了就别据此判死 |", "",
    "> **两个 5 日口径怎么合看**:期末低、期内高 = \"方向对但**回落没守住**\";两栏都低 = "
    "\"**根本没往预测方向动**\";两栏都高 = 真的走对了。", "",
]


def _md_windows(wins: dict, horizons=(1, 5)) -> list[str]:
    """渲染滚动时间窗分层 + base-rate 超额命中 section。"""
    lines = ["## 二、滚动时间窗分层 + 市场基准超额", "",
             f"> 宇宙 {wins.get('宇宙','—')};最新交易日 {wins.get('最新交易日','—')}。"
             f"{wins.get('口径','')}", ""]
    lines += [
        "### 列名说明(新增维度,先读这个)", "",
        "> 把\"上线以来\"再按**最近多少个交易日**切片,并给出**同期市场基准**,回答"
        "\"最近这段时间选得准不准、有没有跑赢大盘涨跌面\"。", "",
        "| 概念 | 含义 | 怎么读 |",
        "|---|---|---|",
        "| **窗口(近一周/一月/一季/一年)** | 按**交易日**数取最近 N 天:近一周=5 / 近一月=20 / 近一季=60 / 近一年=250。"
        "预测日 T 落在 [最新交易日−N+1, 最新交易日] 才进该窗 | N 越大回看越久 |",
        "| **数据充足** | 观测总跨度是否 ≥N 个交易日。**目前观测仅约 3 周(≈19 交易日)**,故近一季/近一年**数据不足**,"
        "如实标注\"实为全部 M 日\",**不等于真有一季/一年数据** | 标\"数据不足\"的窗只是\"把手上全部数据都算进来\",别当独立长窗读 |",
        "| **样本 / 预测日数** | 该窗内已到期且方向非中性的\"票次\"数 / 覆盖的预测交易日数 | <30 仍薄,不足为凭 |",
        "| **命中%(期末)** | 该窗样本的期末方向命中率(口径同上表) | — |",
        "| **基准率%** | **同一批预测日、同一期限、同样只用已到期**,去**全市场(全 master)**算的"
        "\"h 日收盘涨(收益>0)的股票占比\"——即大盘同期的\"上涨面\" | 这是\"随便买/买指数\"的方向基线 |",
        "| **超额命中%** | = 策略期末命中率 − 基准率。**>0 才叫跑赢市场同期**(方向上比大盘涨跌面更准) | 命中率高但基准更高 → 超额可能为负 |", "",
        "> 为什么要基准:\"近一周命中 40%\"单看像很差,但若同期全市场只有 30% 的票在涨,"
        "策略其实**跑赢了 10 个百分点**。基准率就是把\"大盘整体涨跌面\"摆在命中率旁边,让绝对命中率可被解读。", "",
        "> 口径注:基准为**方向基准**(收益>0 记\"涨\",与命中口径同为方向),对齐**期末**命中;"
        "5 日\"期内触及\"无自然基准,故超额只对期末算。短/中性预测占比小的纯多头策略,超额≈\"选股上涨面 − 大盘上涨面\"。", "",
    ]
    for wname, w in wins.get("窗口", {}).items():
        if not w:
            continue
        tag = "" if w.get("数据充足") else " ⚠️数据不足"
        lines += ["", f"### {wname}(N={w.get('窗口交易日数N')} 交易日{tag})", "",
                  f"> {w.get('说明','')}", ""]
        strat = w.get("策略", {})
        if not strat:
            lines.append("该窗内暂无已到期方向样本。")
            continue
        lines.append("| 策略 | 1日样本 | 1日命中% | 1日基准% | 1日超额% | "
                     "5日样本 | 5日命中%(期末) | 5日期内% | 5日基准% | 5日超额% |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for sid, e in sorted(strat.items()):
            c1, c5 = e.get("1日", {}), e.get("5日", {})
            lines.append(
                f"| {sid} {e['策略名']} | {c1.get('已到期样本',0)} | {c1.get('命中率%_期末')} | "
                f"{c1.get('基准率%')} | {c1.get('超额命中%')} | {c5.get('已到期样本',0)} | "
                f"{c5.get('命中率%_期末')} | {c5.get('命中率%_期内触及')} | "
                f"{c5.get('基准率%')} | {c5.get('超额命中%')} |")
    return lines


def _md_report(agg: dict, laggards: dict, horizons=(1, 5)) -> str:
    lines = ["# 全策略上线以来·分策略成绩报告", "",
             f"> 生成于 {agg.get('生成于')}。{agg.get('口径')}",
             "> 历史观测≠未来保证;样本薄(<30)不足为凭。命中率=**方向准确率**(非 alpha,未减市场基线)。**非投资建议。**", "",
             "## 一、分策略成绩(上线以来累计)", ""]
    lines += _COL_LEGEND_MAIN
    lines += ["| 策略 | 预测日数 | 1日样本 | 1日命中% | 5日样本 | 5日命中%(期末) | 5日命中%(期内触及) | 5日均收益% | 薄样本 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for sid, e in sorted(agg.get("策略", {}).items()):
        c1, c5 = e.get("1日", {}), e.get("5日", {})
        lines.append(
            f"| {sid} {e['策略名']} | {e['预测日数']} | {c1.get('已到期样本',0)} | "
            f"{c1.get('命中率%_期末')} | {c5.get('已到期样本',0)} | {c5.get('命中率%_期末')} | "
            f"{c5.get('命中率%_期内触及')} | {c5.get('平均实现收益%')} | {e.get('薄样本')} |")

    wins = agg.get("窗口分层")
    if wins:
        lines += [""] + _md_windows(wins, horizons)

    double = laggards.get("双弱差生候选", [])
    obs_only = laggards.get("仅观测偏弱提示", [])
    lines += ["", "## 三、差生候选(回测偏弱 ∧ 观测也弱)", "",
              "判据:5日期末方向命中 < 45% **且**样本≥30 **且**既有回测结论偏弱。单一维度差不进此表。", ""]
    if not double:
        lines.append("暂无。")
    else:
        lines.append("| 策略 | 5日期末命中% | 5日期内触及% | 样本 | 既有回测结论 |")
        lines.append("|---|---|---|---|---|")
        for l in double:
            lines.append(f"| {l['strategy_id']} {l['策略名']} | {l['5日期末命中率%']} | "
                         f"{l.get('5日期内触及%')} | {l['样本']} | {l.get('回测结论') or '—'} |")
    lines += ["", "## 四、仅观测偏弱提示(回测未定弱,不判死)", ""]
    if not obs_only:
        lines.append("无。")
    else:
        lines.append("| 策略 | 5日期末命中% | 样本 | 既有回测结论 |")
        lines.append("|---|---|---|---|")
        for l in obs_only:
            lines.append(f"| {l['strategy_id']} {l['策略名']} | {l['5日期末命中率%']} | "
                         f"{l['样本']} | {l.get('回测结论') or '—'} |")
    lines += ["", "## 五、自审要点", "",
              "- **防未来函数**:horizon h 仅当 kline 存在 idx+h(≤最近交易日)才计入,pending 一律排除;",
              "  预测清单只读信号日 T 落盘的 view,实现收益取 T 之后价,双向无泄漏。**基准率同样只用已到期收益**,",
              "  窗口边界按**交易日**(交易日历)划,不是自然日,故\"近一周\"= 最近 5 个交易日而非 7 个日历日。",
              "- **样本量**:多数策略上线仅数日、5日窗口到期样本薄(1日样本远多于5日);薄样本已标注,勿据此判死。",
              "- **双口径**:期末=T→T+5 收盘累计收益方向;期内触及=窗口内最高(看多)/最低(看空)触及预测方向即算。",
              "  期内命中率天然远高于期末(触及门槛低),二者并列用于区分'方向对但回落'与'完全没动对'。",
              "- **滚动窗数据不足**:观测仅约 3 周(≈19 交易日),**近一季(60)/近一年(250)数据远不够**,",
              "  报告标\"数据不足 N 交易日,实为全部 M 日\",这些窗只是\"把手上全部数据都算进来\",**绝不等于真有一季/一年数据**。",
              "- **超额命中口径**:策略命中率与基准率用**完全相同的预测日集合 + 同一期限 + 同样只计已到期**,",
              "  基准率=同期全市场(全 master)h 日收益>0 占比(方向基准);超额=策略期末命中 − 基准。",
              "  >0 才叫跑赢市场同期涨跌面。宇宙用全 master 作稳定基线(未按流动性/可交易性筛,口径从简、可复现)。",
              "- **命中率≠alpha**:此处是方向准确率;超额命中已扣\"大盘涨跌面\"这一方向基线,但仍非严格因子 alpha;",
              "  判死仍以既有全A回测(IC/Alpha/超额)为准,观测命中低 + 超额为负只作**佐证**。",
              "  口径 close[idx+h]/close[idx]-1 与 forward_scorecard 完全一致,无漂移。"]
    return "\n".join(lines) + "\n"


def run(analysis_dir=_DEFAULT_ANALYSIS, out_json=_DEFAULT_JSON, out_md=_DEFAULT_MD,
        horizons=(1, 5), backtest_verdicts=None, windows=None, universe=None,
        with_windows=True):
    book = KlineBook()
    rows = build_rows(analysis_dir, horizons=horizons, book=book)
    agg = aggregate(rows, horizons)
    laggards = flag_laggards(agg, backtest_verdicts)
    agg["差生分档"] = laggards

    if with_windows and not rows.empty:
        calendar = build_trading_calendar(book)
        pred_dates = sorted(rows["date"].unique().tolist())
        uni = universe if universe is not None else _load_universe()
        logger.info("base-rate 宇宙 %d 票 × 预测日 %d × 期限 %s", len(uni), len(pred_dates), horizons)
        market_base = build_market_baseline(pred_dates, horizons, uni, book)
        wins = aggregate_windows(rows, calendar, market_base, horizons, windows)
        wins["宇宙"] = f"全 master({len(uni)} 票)"
        wins["最新交易日"] = calendar[-1] if calendar else None
        agg["窗口分层"] = wins
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(agg, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(out_md) or ".", exist_ok=True)
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(_md_report(agg, laggards, horizons))
    print("\n===== 分策略成绩记分卡 =====")
    print(f"策略数 {len(agg.get('策略', {}))}  逐票行 {len(rows)}  → {out_json}")
    for sid, e in sorted(agg.get("策略", {}).items()):
        c1, c5 = e.get("1日", {}), e.get("5日", {})
        print(f"  {sid} {e['策略名']}: 1日 n={c1.get('已到期样本',0)} 命中={c1.get('命中率%_期末')}% | "
              f"5日 n={c5.get('已到期样本',0)} 期末={c5.get('命中率%_期末')}% "
              f"期内={c5.get('命中率%_期内触及')}%  [{e.get('薄样本')}]")
    dbl = laggards.get("双弱差生候选", [])
    if dbl:
        print("  差生候选(双弱):", ", ".join(f"{l['strategy_id']} {l['策略名']}" for l in dbl))
    obs = laggards.get("仅观测偏弱提示", [])
    if obs:
        print("  仅观测偏弱(不判死):", ", ".join(f"{l['strategy_id']} {l['策略名']}" for l in obs))
    wins = agg.get("窗口分层")
    if wins:
        print(f"  滚动窗(宇宙 {wins.get('宇宙')} 最新 {wins.get('最新交易日')}):")
        for wname, w in wins.get("窗口", {}).items():
            flag = "" if w.get("数据充足") else " [数据不足]"
            nstrat = len(w.get("策略", {}))
            print(f"    {wname} N={w.get('窗口交易日数N')}{flag}: 覆盖{nstrat}策略, {w.get('说明','')}")
    return agg


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis-dir", default=_DEFAULT_ANALYSIS)
    ap.add_argument("--out-json", default=_DEFAULT_JSON)
    ap.add_argument("--out-md", default=_DEFAULT_MD)
    ap.add_argument("--horizon", default="1,5")
    ap.add_argument("--windows", default=None,
                    help="覆盖窗口定义,如 '近一周=5,近一月=20';缺省用内置 4 窗")
    ap.add_argument("--no-windows", action="store_true", help="只出上线以来累计表,不算滚动窗+基准")
    a = ap.parse_args()
    wins = None
    if a.windows:
        wins = {}
        for tok in a.windows.split(","):
            k, _, v = tok.partition("=")
            if k.strip() and v.strip().isdigit():
                wins[k.strip()] = int(v.strip())
    run(analysis_dir=a.analysis_dir, out_json=a.out_json, out_md=a.out_md,
        horizons=tuple(int(x) for x in a.horizon.split(",")),
        windows=wins, with_windows=not a.no_windows)
