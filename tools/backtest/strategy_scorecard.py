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
import json
import logging
import os
from collections import defaultdict

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.config import settings

logger = logging.getLogger("backtest.strategy_scorecard")

# 方向文案 → 符号。看涨/看多 +1;看跌/看空 -1;中性 0(0/None 不计命中)。
_DIR = {"看多": 1, "看涨": 1, "多": 1, "看空": -1, "看跌": -1, "空": -1, "中性": 0}

# 已上线策略登记表:文件名(去 .json) → (策略ID, 展示名)。用于报告归类与稳定命名。
# 不在表内的 view 文件也会被通用提取器兜底处理(ID 用文件名),保证新策略不漏。
STRATEGY_SPECS: dict[str, tuple[str, str]] = {
    "策略0合议": ("0", "多专家合议(全A)"),
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

# 各策略"选股清单"可能的键名(按优先级)。通用提取器还会兜底扫 list[dict(code)]。
_PICK_KEYS = ("入选清单", "top", "rows", "排行", "达标清单", "达标")

_THIN_N = 30   # 样本 < 此阈值 → 标"薄样本,不足为凭"

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
            if stem in _SKIP_STEMS or (len(stem) == 6 and stem.isdigit()):
                continue   # 逐票 record / 面板 / 情绪等
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
def _md_report(agg: dict, laggards: dict, horizons=(1, 5)) -> str:
    lines = ["# 全策略上线以来·分策略成绩报告", "",
             f"> 生成于 {agg.get('生成于')}。{agg.get('口径')}",
             "> 历史观测≠未来保证;样本薄(<30)不足为凭。命中率=**方向准确率**(非 alpha,未减市场基线)。**非投资建议。**", "",
             "## 一、分策略成绩", "",
             "| 策略 | 预测日数 | 1日样本 | 1日命中% | 5日样本 | 5日命中%(期末) | 5日命中%(期内触及) | 5日均收益% | 薄样本 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for sid, e in sorted(agg.get("策略", {}).items()):
        c1, c5 = e.get("1日", {}), e.get("5日", {})
        lines.append(
            f"| {sid} {e['策略名']} | {e['预测日数']} | {c1.get('已到期样本',0)} | "
            f"{c1.get('命中率%_期末')} | {c5.get('已到期样本',0)} | {c5.get('命中率%_期末')} | "
            f"{c5.get('命中率%_期内触及')} | {c5.get('平均实现收益%')} | {e.get('薄样本')} |")

    double = laggards.get("双弱差生候选", [])
    obs_only = laggards.get("仅观测偏弱提示", [])
    lines += ["", "## 二、差生候选(回测偏弱 ∧ 观测也弱)", "",
              "判据:5日期末方向命中 < 45% **且**样本≥30 **且**既有回测结论偏弱。单一维度差不进此表。", ""]
    if not double:
        lines.append("暂无。")
    else:
        lines.append("| 策略 | 5日期末命中% | 5日期内触及% | 样本 | 既有回测结论 |")
        lines.append("|---|---|---|---|---|")
        for l in double:
            lines.append(f"| {l['strategy_id']} {l['策略名']} | {l['5日期末命中率%']} | "
                         f"{l.get('5日期内触及%')} | {l['样本']} | {l.get('回测结论') or '—'} |")
    lines += ["", "## 三、仅观测偏弱提示(回测未定弱,不判死)", ""]
    if not obs_only:
        lines.append("无。")
    else:
        lines.append("| 策略 | 5日期末命中% | 样本 | 既有回测结论 |")
        lines.append("|---|---|---|---|")
        for l in obs_only:
            lines.append(f"| {l['strategy_id']} {l['策略名']} | {l['5日期末命中率%']} | "
                         f"{l['样本']} | {l.get('回测结论') or '—'} |")
    lines += ["", "## 四、自审要点", "",
              "- **防未来函数**:horizon h 仅当 kline 存在 idx+h(≤最近交易日)才计入,pending 一律排除;",
              "  预测清单只读信号日 T 落盘的 view,实现收益取 T 之后价,双向无泄漏。",
              "- **样本量**:多数策略上线仅数日、5日窗口到期样本薄(1日样本远多于5日);薄样本已标注,勿据此判死。",
              "- **双口径**:期末=T→T+5 收盘累计收益方向;期内触及=窗口内最高(看多)/最低(看空)触及预测方向即算。",
              "  期内命中率天然远高于期末(触及门槛低),二者并列用于区分'方向对但回落'与'完全没动对'。",
              "- **命中率≠alpha**:此处是方向准确率,未减市场同期基线;判死仍以既有全A回测(IC/Alpha/超额)为准,",
              "  观测命中低只作**佐证**。口径 close[idx+h]/close[idx]-1 与 forward_scorecard 完全一致,无漂移。"]
    return "\n".join(lines) + "\n"


def run(analysis_dir=_DEFAULT_ANALYSIS, out_json=_DEFAULT_JSON, out_md=_DEFAULT_MD,
        horizons=(1, 5), backtest_verdicts=None):
    rows = build_rows(analysis_dir, horizons=horizons)
    agg = aggregate(rows, horizons)
    laggards = flag_laggards(agg, backtest_verdicts)
    agg["差生分档"] = laggards
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
    return agg


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis-dir", default=_DEFAULT_ANALYSIS)
    ap.add_argument("--out-json", default=_DEFAULT_JSON)
    ap.add_argument("--out-md", default=_DEFAULT_MD)
    ap.add_argument("--horizon", default="1,5")
    a = ap.parse_args()
    run(analysis_dir=a.analysis_dir, out_json=a.out_json, out_md=a.out_md,
        horizons=tuple(int(x) for x in a.horizon.split(",")))
