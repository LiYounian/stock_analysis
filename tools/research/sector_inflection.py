"""④ 消息驱动板块拐点 · 应用层（forward-tracked、non-gating advisory）。

策略见 docs/计划/2026-09-15_消息驱动板块拐点_策略计划.md。统筹裁决 scope：
  · **消费 regime 板块环境层，不重造**：价量温度 `thermometer.build_thermometer_panel`
    （拥挤/动量冷热，有历史）+ 情绪 `sentiment_judge.get_industry_sentiment_series`
    （净A度，forward-only，接调度前返回 {}）。板块冷热 = 拐点检测**一等输入**。
  · **拐点检测归本层**：regime 只产"当前冷热档"，本层在其序列上算"冷→热正拐点"。
  · **未 validated 前一律 non-gating advisory**：每日 shadow 记录板块拐点 + 成员进场候选，
    **只记录、不进任何生产选股决策**；反向用实际走势/每日选股结果校验本信号；
    攒够独立样本(≥120)+命中显著后，由统筹 surface 用户拍是否真 gate。
  · 技术外壳（缩量回踩+高开反包）单独已证伪(α−0.9%)，只在拐点板块内作成员择时。

无未来函数：panel/情绪均因果 as-of；成员信号只用 ≤date 收盘。
⚠️ 测试环境研究模拟，非投资建议。不合 main、不动生产选股。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from tools.analysis.industry_temp import thermometer, sentiment_judge
from tools.collectors import code_industry

logger = logging.getLogger("research.sector_inflection")

_档序 = {"冷": 0, "温": 1, "热": 2}


# ---------- 数据装配 ----------
def membership() -> dict:
    """code→申万一级 全A映射（regime 同源，单一真源）。"""
    return code_industry.load()


def _trading_dates_around(date: str, k: int, klines_dates: list[str]) -> list[str]:
    """取 ≤date 的最近 k+1 个交易日（升序）。klines_dates=某参考票的交易日列表。"""
    prior = [d for d in klines_dates if d <= date]
    return prior[-(k + 1):] if len(prior) > k else prior


# ---------- 拐点检测（在 regime 序列上做，消费不重造）----------
@dataclass
class Inflection:
    industry: str
    date: str
    价量拐点: bool = False
    价量依据: str = ""
    情绪拐点: bool = False
    情绪依据: str = ""
    动量档: str | None = None
    动量分位: float | None = None
    净A度: float | None = None

    @property
    def 正拐点(self) -> bool:
        return self.价量拐点 or self.情绪拐点


def vol_price_inflection(panel: pd.DataFrame, industry: str, dates: list[str]) -> tuple[bool, str, dict]:
    """价量正拐点：动量_时序档 由"冷"翻转向上（冷→温/热），或 动量_时序分位 因果上穿。
    panel = build_thermometer_panel(dates, membership) 已含全序列。dates 升序、末位=当日。"""
    sub = panel[panel["industry"] == industry].set_index("date").reindex(dates)
    q = sub["动量_时序分位"].astype(float)
    g = sub["动量_时序档"]
    if q.dropna().shape[0] < 3:
        return False, "史不足", {"动量档": g.iloc[-1] if len(g) else None, "动量分位": None}
    cur_q, prev_q = q.iloc[-1], q.iloc[-2]
    cur_g, prev_g = g.iloc[-1], g.iloc[-2]
    info = {"动量档": cur_g, "动量分位": None if pd.isna(cur_q) else round(float(cur_q), 3)}
    # (a) 档由冷翻上：昨"冷" 今非"冷"
    if prev_g == "冷" and cur_g in ("温", "热"):
        return True, f"动量档 冷→{cur_g}(分位{prev_q:.2f}→{cur_q:.2f})", info
    # (b) 分位从低位因果上穿(过去低于0.3、今上穿0.4，且抬升)
    if (not pd.isna(cur_q) and not pd.isna(prev_q)
            and prev_q < 0.30 and cur_q >= 0.40 and cur_q > prev_q):
        return True, f"动量分位低位上穿 {prev_q:.2f}→{cur_q:.2f}", info
    return False, "", info


def sentiment_inflection(industry: str, dates: list[str], shadow_dir: str,
                         delta_min: float = 0.15) -> tuple[bool, str, float | None]:
    """情绪正拐点：净A度 delta 由负转正/显著抬升。forward-only：shadow 无历史→返回 (False,'无情绪',None)。"""
    try:
        series = sentiment_judge.get_industry_sentiment_series(industry, dates, shadow_dir)
    except Exception as e:  # noqa: BLE001
        return False, f"情绪读取失败:{str(e)[:40]}", None
    if not series:
        return False, "无情绪(forward未积累)", None
    vals = [series.get(d) for d in dates if series.get(d) is not None]
    if len(vals) < 2:
        return False, "情绪样本<2", (vals[-1] if vals else None)
    cur, prev = vals[-1], vals[-2]
    if cur - prev >= delta_min and cur > 0:
        return True, f"净A度抬升 {prev:+.2f}→{cur:+.2f}", cur
    return False, "", cur


def detect(date: str, shadow_dir: str, ref_dates: list[str], lookback: int = 10,
           industries: list[str] | None = None) -> list[Inflection]:
    """检测 date 当日所有（或指定）行业的板块正拐点。ref_dates=交易日历(升序)。"""
    dates = _trading_dates_around(date, lookback, ref_dates)
    panel = thermometer.build_thermometer_panel(dates, membership())
    inds = industries or sorted(panel["industry"].dropna().unique().tolist())
    out = []
    for ind in inds:
        vp, vp_why, info = vol_price_inflection(panel, ind, dates)
        se, se_why, na = sentiment_inflection(ind, dates, shadow_dir)
        infl = Inflection(industry=ind, date=date, 价量拐点=vp, 价量依据=vp_why,
                          情绪拐点=se, 情绪依据=se_why, 动量档=info.get("动量档"),
                          动量分位=info.get("动量分位"), 净A度=na)
        if infl.正拐点:
            out.append(infl)
    return out


# ---------- 成员进场层（缩量回踩，技术外壳·只在拐点板块内用）----------
def member_pullback(df: pd.DataFrame, t: int) -> bool:
    """缩量回踩支撑(第 t 日)：缩量 + 温和整理 + 贴 MA20 + 上升趋势中。只用 ≤t。"""
    c = df["close"].to_numpy(); v = df["volume"].to_numpy(); pch = df["pct_chg"].to_numpy()
    if t < 25:
        return False
    ma20 = pd.Series(c).rolling(20).mean().to_numpy()
    vma20 = pd.Series(v).rolling(20).mean().to_numpy()
    ma20_5 = pd.Series(c).rolling(20).mean().shift(5).to_numpy()
    return bool(v[t] < 0.8 * vma20[t] and -4.0 <= pch[t] <= 1.5
                and ma20[t] * 0.97 <= c[t] <= ma20[t] * 1.10 and ma20[t] > ma20_5[t])


def select_members(industry: str, date: str, klines: dict, ref_dates: list[str],
                   top_n: int = 3) -> list[dict]:
    """拐点板块内的成员进场候选：当日缩量回踩支撑的票，给次日进场计划(高开反包·开盘价)。"""
    c2i = membership()
    cands = []
    for code, df in klines.items():
        if c2i.get(code) != industry:
            continue
        dd = df["date"].astype(str).to_numpy()
        idx = np.where(dd <= date)[0]
        if len(idx) == 0:
            continue
        t = int(idx[-1])
        if dd[t] != date:
            continue
        if member_pullback(df, t):
            cands.append({"code": code, "缩量回踩日": date,
                          "次日计划": "高开(≥1%)反包·开盘价买入",
                          "昨收": round(float(df["close"].iloc[t]), 2)})
    return cands[:top_n]


# ---------- 每日 shadow 记录器（non-gating advisory）----------
def run_daily_shadow(date: str, out_dir: str, shadow_dir: str, klines: dict,
                     ref_dates: list[str]) -> dict:
    """每日跑：检测板块拐点 + 各拐点板块成员候选 → 落盘 advisory（**只记录、不 gate**）。"""
    os.makedirs(out_dir, exist_ok=True)
    infls = detect(date, shadow_dir, ref_dates)
    advisory = {"date": date, "非validated": True, "non_gating": True,
                "板块拐点": [], "note": "forward-shadow advisory；未validated前不进任何生产选股决策"}
    for infl in infls:
        rec = asdict(infl)
        rec["成员候选"] = select_members(infl.industry, date, klines, ref_dates)
        advisory["板块拐点"].append(rec)
    path = os.path.join(out_dir, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(advisory, f, ensure_ascii=False, indent=2)
    logger.info("shadow advisory %s: %d 板块拐点", date, len(infls))
    return advisory


# ---------- 反馈校验（反向用实际走势校验拐点信号）----------
def evaluate_shadow(out_dir: str, klines: dict, horizons=(1, 5, 10)) -> dict:
    """读历史 advisory，用实际 K 线算：拐点板块 next-N 日行业指数收益 + 成员候选进场收益。
    non-gating 的反馈校验；样本不足只如实报 N。"""
    c2i = membership()
    ind_ret = {c: (df.set_index("date")["pct_chg"] / 100.0) for c, df in klines.items()}
    recs = []
    for fn in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if not fn.endswith(".json"):
            continue
        adv = json.load(open(os.path.join(out_dir, fn), encoding="utf-8"))
        for b in adv.get("板块拐点", []):
            recs.append((adv["date"], b["industry"], b.get("成员候选", [])))
    return {"样本数": len(recs), "说明": "forward 样本；≥120 且命中显著才谈 validate",
            "records": recs[:5]}
