"""策略0「多专家合议」逐专家预测准确性记分卡(分析·非投资建议)。

背景:screen_council 的全A最小记录只填 signals(trend/reversal/ob_os),故合议默认 9 专家里
只有 **技术趋势 / 超买超卖 / 拐点** 三个在场发声,其余(资金流/情绪三层/多因子/事件驱动/板块轮动/财报)
因无对应数据天然弃权(置信度0)。本模块单独评估这三个在场专家各自的**方向命中**与**对市场基准的超额**,
定位"谁更准、谁在拖后腿",为权重调整建议提供数据支撑。

两条口径(互为交叉验证):
  A) 观测口径:读 data/analysis/<日期>/策略0合议.json 的 top 清单里每票 council.default.归因,
     取 置信度>0 且 方向≠中性 的 (专家,方向) → 前向 1/5 日实现收益方向命中。样本=历史 top20,量小。
  B) 历史全A回测口径:从 master 随机抽 N 票 × 抽样交易日,现算 technical.compute → 三专家方向,
     配前向 1/5 日收益方向命中。样本大、无幸存者偏差(抽样在信号日之前,不看未来)。

方向命中 = sign(前向收益) == sign(专家方向)。市场基准(每样本):该(日,期限)抽样宇宙里 h 日收益>0 占比 P_up;
看多样本基线正确率=P_up、看空样本=1−P_up。超额 = 命中率 − 基线正确率(>0=比"随机同向"更准)。

防未来函数:专家方向只用 ≤信号日 的 K 线(technical.compute 取截至该 bar);前向收益取信号日之后价,
h 仅当 idx+h < len 才计(未到期排除)。与 strategy_scorecard 完全同口径。

用法:python -m tools.backtest.expert_scorecard [--sample N] [--step K] [--start-idx S]
                                              [--seed SEED] [--analysis-dir DIR] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random

import numpy as np

from tools.analysis import experts
from tools.collectors import market
from tools.pipeline.screen_council import build_min_record
from tools.store import repo as store

logger = logging.getLogger("backtest.expert_scorecard")

_DIR = {"看多": 1, "看空": -1, "中性": 0}
ACTIVE_EXPERTS = ["技术趋势", "超买超卖", "拐点"]   # 全A合议中实际在场的三专家
MIN_BARS = 60


# ───────────────────────── 前向收益 ─────────────────────────
def _fwd_dir(close: np.ndarray, idx: int, h: int) -> int | None:
    """信号日 idx 的 h 日期末收益方向(+1 涨 / -1 跌 / 0 平);未到期或无效 → None。"""
    if idx + h >= len(close) or close[idx] <= 0:
        return None
    r = close[idx + h] / close[idx] - 1.0
    return int(np.sign(r))


def _fwd_ret(close: np.ndarray, idx: int, h: int) -> float | None:
    if idx + h >= len(close) or close[idx] <= 0:
        return None
    return float(close[idx + h] / close[idx] - 1.0) * 100.0


# ───────────────────────── 聚合器 ─────────────────────────
class ExpertAgg:
    """按 (专家, 期限) 累加:命中数/样本数/收益和/基线正确率和。"""

    def __init__(self):
        self.d: dict = {}   # (expert, h) -> dict

    def add(self, expert: str, h: int, direction: int, fwd_dir: int,
            fwd_ret: float, p_up: float | None):
        k = (expert, h)
        c = self.d.setdefault(k, {"n": 0, "hit": 0, "ret_sum": 0.0,
                                  "base_sum": 0.0, "base_n": 0,
                                  "n_long": 0, "n_short": 0})
        c["n"] += 1
        # 方向命中(0 收益按未命中处理,与 sign 一致:sign(0)=0≠±1)
        c["hit"] += int(np.sign(fwd_dir) == np.sign(direction))
        c["ret_sum"] += fwd_ret
        c["n_long"] += int(direction > 0)
        c["n_short"] += int(direction < 0)
        if p_up is not None:
            base_correct = p_up if direction > 0 else (1.0 - p_up)
            c["base_sum"] += base_correct
            c["base_n"] += 1

    def report(self) -> dict:
        out = {}
        for (expert, h), c in sorted(self.d.items()):
            n = c["n"]
            if n == 0:
                continue
            hit = round(c["hit"] / n * 100, 1)
            base = round(c["base_sum"] / c["base_n"] * 100, 1) if c["base_n"] else None
            out.setdefault(expert, {})[f"{h}日"] = {
                "样本": n, "命中率%": hit,
                "平均收益%": round(c["ret_sum"] / n, 3),
                "基线正确率%": base,
                "超额%": (round(hit - base, 1) if base is not None else None),
                "看多样本": c["n_long"], "看空样本": c["n_short"],
            }
        return out


# ───────────────────────── B) 历史全A回测 ─────────────────────────
def backtest_historical(sample: int = 400, step: int = 20, start_idx: int = 250,
                        seed: int = 7, horizons=(1, 5)) -> dict:
    """从 master 抽 sample 票,每票每 step 个交易日取一个信号日,现算三专家方向 → 前向命中。

    基线:每(信号日, h)先在抽样宇宙统计 P_up(收益>0 占比),作方向基线。抽样宇宙=本次抽的票,
    是 master 的随机子集,近似市场涨跌面。防未来函数:信号日前算方向、之后取收益。
    """
    codes = sorted(store.list_master_codes())
    rng = random.Random(seed)
    picks = rng.sample(codes, min(sample, len(codes)))

    # 预载 K 线 + 每票信号日下标
    loaded: dict[str, tuple] = {}   # code -> (close, kdf, [signal_idx...])
    for c in picks:
        try:
            df = market.load_kline(c).reset_index(drop=True)
        except Exception:                       # noqa: BLE001
            continue
        if len(df) < start_idx + max(horizons) + 5:
            continue
        close = df["close"].to_numpy(float)
        idxs = list(range(start_idx, len(df) - max(horizons), step))
        loaded[c] = (close, df, idxs)
    logger.info("历史回测:抽样 %d 票,可用 %d 票", len(picks), len(loaded))

    # 第一遍:市场方向基线 P_up[(date_idx_key, h)] —— 用 (code 无关的) 全宇宙同日
    # 这里"同日"用交易日日期字符串对齐(各票日历一致,前复权同源)。
    up: dict = {}    # (date_str, h) -> [up, total]
    for c, (close, df, idxs) in loaded.items():
        dates = df["date"].astype(str).str.slice(0, 10).tolist()
        for idx in idxs:
            for h in horizons:
                if idx + h >= len(close) or close[idx] <= 0:
                    continue
                key = (dates[idx], h)
                cell = up.setdefault(key, [0, 0])
                cell[1] += 1
                if close[idx + h] > close[idx]:
                    cell[0] += 1

    def p_up(date_str: str, h: int) -> float | None:
        cell = up.get((date_str, h))
        return (cell[0] / cell[1]) if cell and cell[1] else None

    # 合议综合方向 A/B 权重方案(tau=0.2,置信度加权分母,与 council.convene 同口径)。
    # 每方案只对三在场专家配权;其余弃权(置信度0)本就不入分子分母。
    TAU = 0.2
    WEIGHT_SCHEMES = {
        "等权(现状1.0/1.0/1.0)": {"技术趋势": 1.0, "超买超卖": 1.0, "拐点": 1.0},
        "建议(趋势0.5/超卖1.5/拐点1.0)": {"技术趋势": 0.5, "超买超卖": 1.5, "拐点": 1.0},
        "反转倾斜(趋势0.3/超卖2.0/拐点1.2)": {"技术趋势": 0.3, "超买超卖": 2.0, "拐点": 1.2},
    }
    comp = {name: ExpertAgg() for name in WEIGHT_SCHEMES}   # 复用 ExpertAgg:专家名槽存"综合"

    # 第二遍:逐票逐信号日算三专家方向 + 命中
    agg = ExpertAgg()
    strength_buckets: dict = {}   # 技术趋势 强度分档命中(看趋势强度是否单调)
    n_points = 0
    for c, (close, df, idxs) in loaded.items():
        dates = df["date"].astype(str).str.slice(0, 10).tolist()
        for idx in idxs:
            sub = df.iloc[:idx + 1].reset_index(drop=True)
            if len(sub) < MIN_BARS:
                continue
            rec = build_min_record(c, sub)
            if rec is None:
                continue
            n_points += 1
            verdicts = []   # (name, direction, strength, conf) 供合议 A/B
            for name in ACTIVE_EXPERTS:
                v = experts.build(name, rec, sub)
                verdicts.append((name, _DIR.get(v.方向, 0), float(v.强度), float(v.置信度)))
                if v.置信度 <= 0 or v.方向 == "中性":
                    continue
                direction = _DIR.get(v.方向, 0)
                if direction == 0:
                    continue
                for h in horizons:
                    fd = _fwd_dir(close, idx, h)
                    fr = _fwd_ret(close, idx, h)
                    if fd is None or fr is None:
                        continue
                    pu = p_up(dates[idx], h)
                    agg.add(name, h, direction, fd, fr, pu)
                    if name == "技术趋势" and h == 5:
                        b = round(abs(v.强度), 1)
                        sb = strength_buckets.setdefault(b, {"n": 0, "hit": 0})
                        sb["n"] += 1
                        sb["hit"] += int(np.sign(fd) == np.sign(direction))
            # 合议综合方向 A/B:每方案算 S=Σ(强度×置信度×权重)/Σ(权重×置信度)→ 综合方向
            for sname, wmap in WEIGHT_SCHEMES.items():
                num = den = 0.0
                for (name, d, s, conf) in verdicts:
                    w = wmap.get(name, 1.0)
                    num += s * conf * w
                    den += w * conf
                S = (num / den) if den > 0 else 0.0
                cdir = 1 if S >= TAU else (-1 if S <= -TAU else 0)
                if cdir == 0:
                    continue
                for h in horizons:
                    fd = _fwd_dir(close, idx, h)
                    fr = _fwd_ret(close, idx, h)
                    if fd is None or fr is None:
                        continue
                    comp[sname].add("综合", h, cdir, fd, fr, p_up(dates[idx], h))
    rep = agg.report()
    sb_out = {str(k): {"样本": v["n"], "命中率%": round(v["hit"] / v["n"] * 100, 1)}
              for k, v in sorted(strength_buckets.items()) if v["n"]}
    comp_out = {sname: a.report().get("综合", {}) for sname, a in comp.items()}
    return {"口径": "历史全A回测(master 随机抽样,信号日前算方向/之后取收益,防未来函数)",
            "参数": {"抽样票数": len(loaded), "step": step, "start_idx": start_idx,
                     "seed": seed, "信号点数": n_points},
            "逐专家": rep, "技术趋势强度分档(5日期末命中)": sb_out,
            "合议综合方向_权重AB": comp_out}


# ───────────────────────── A) 观测口径 ─────────────────────────
def backtest_observation(analysis_dir: str, horizons=(1, 5)) -> dict:
    """读历史 策略0合议.json,从每票 council.default.归因 取在场专家方向 → 前向命中。"""
    agg = ExpertAgg()
    book: dict[str, tuple] = {}     # code -> (close, {date:idx})

    def get(code: str):
        if code not in book:
            try:
                df = market.load_kline(code).reset_index(drop=True)
                dmap = {str(x)[:10]: i for i, x in enumerate(df["date"].tolist())}
                book[code] = (df["close"].to_numpy(float), dmap)
            except Exception:                    # noqa: BLE001
                book[code] = None
        return book[code]

    # 收集所有 (date, code) 以便算基线;先扫 view
    picks: list[tuple] = []     # (date, code, expert, direction)
    dates_seen: set = set()
    if os.path.isdir(analysis_dir):
        for d in sorted(os.listdir(analysis_dir)):
            path = os.path.join(analysis_dir, d, "策略0合议.json")
            if not (d[:4].isdigit() and os.path.isfile(path)):
                continue
            try:
                view = json.load(open(path, encoding="utf-8"))
            except Exception:                    # noqa: BLE001
                continue
            as_of = str(view.get("as_of") or d)[:10]
            dates_seen.add(as_of)
            for t in view.get("top", []):
                code = str(t.get("code") or "")
                gy = ((t.get("council") or {}).get("default") or {}).get("归因", [])
                for a in gy:
                    exp = a.get("专家")
                    if exp not in ACTIVE_EXPERTS:
                        continue
                    if (a.get("置信度") or 0) <= 0 or a.get("方向") == "中性":
                        continue
                    direction = _DIR.get(a.get("方向"), 0)
                    if direction:
                        picks.append((as_of, code, exp, direction))

    # 基线 P_up:用出现过的 code 集合近似(观测宇宙小,标注样本薄)
    codes_all = sorted({c for _, c, _, _ in picks})
    up: dict = {}
    for c in codes_all:
        rec = get(c)
        if rec is None:
            continue
        close, dmap = rec
        for dt in dates_seen:
            idx = dmap.get(dt)
            if idx is None or close[idx] <= 0:
                continue
            for h in horizons:
                if idx + h >= len(close):
                    continue
                cell = up.setdefault((dt, h), [0, 0])
                cell[1] += 1
                if close[idx + h] > close[idx]:
                    cell[0] += 1

    for (dt, code, exp, direction) in picks:
        rec = get(code)
        if rec is None:
            continue
        close, dmap = rec
        idx = dmap.get(dt)
        if idx is None:
            continue
        for h in horizons:
            fd = _fwd_dir(close, idx, h)
            fr = _fwd_ret(close, idx, h)
            if fd is None or fr is None:
                continue
            cell = up.get((dt, h))
            pu = (cell[0] / cell[1]) if cell and cell[1] else None
            agg.add(exp, h, direction, fd, fr, pu)

    return {"口径": "观测口径(历史 策略0合议 top 清单 council 归因;样本=历史top,量小)",
            "预测日数": len(dates_seen), "逐专家": agg.report()}


def run(sample=400, step=20, start_idx=250, seed=7, analysis_dir=None,
        out=None, horizons=(1, 5)) -> dict:
    hist = backtest_historical(sample, step, start_idx, seed, horizons)
    obs = backtest_observation(analysis_dir, horizons) if analysis_dir else {"跳过": "未给 analysis_dir"}
    result = {"生成于": __import__("pandas").Timestamp.now().strftime("%Y-%m-%d %H:%M"),
              "在场专家": ACTIVE_EXPERTS,
              "说明": "全A合议最小记录只填 signals,故仅三专家在场;其余弃权(置信度0)不评。非投资建议。",
              "A_观测口径": obs, "B_历史全A回测": hist}
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    # 控制台摘要
    print("\n===== 逐专家准确率记分卡 =====")
    print("[B 历史全A回测]", hist["参数"])
    for exp, cells in hist["逐专家"].items():
        for hk, c in cells.items():
            print(f"  {exp} {hk}: n={c['样本']} 命中={c['命中率%']}% 基线={c['基线正确率%']}% "
                  f"超额={c['超额%']}% 均收益={c['平均收益%']}% (多{c['看多样本']}/空{c['看空样本']})")
    print("  技术趋势强度分档(5日):", hist["技术趋势强度分档(5日期末命中)"])
    print("  [合议综合方向 权重A/B]")
    for sname, cells in hist.get("合议综合方向_权重AB", {}).items():
        parts = []
        for hk, c in cells.items():
            parts.append(f"{hk} n={c['样本']} 命中={c['命中率%']}% 超额={c['超额%']}%")
        print(f"    {sname}: " + " | ".join(parts))
    if "逐专家" in obs:
        print("[A 观测口径] 预测日数", obs.get("预测日数"))
        for exp, cells in obs["逐专家"].items():
            for hk, c in cells.items():
                print(f"  {exp} {hk}: n={c['样本']} 命中={c['命中率%']}% 超额={c['超额%']}%")
    if out:
        print("→", out)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略0 逐专家预测准确率记分卡")
    ap.add_argument("--sample", type=int, default=400)
    ap.add_argument("--step", type=int, default=20)
    ap.add_argument("--start-idx", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--analysis-dir", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--horizon", default="1,5")
    a = ap.parse_args()
    run(sample=a.sample, step=a.step, start_idx=a.start_idx, seed=a.seed,
        analysis_dir=a.analysis_dir, out=a.out,
        horizons=tuple(int(x) for x in a.horizon.split(",")))
