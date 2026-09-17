#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D2 收敛机制 · 程序层原型 v1（闸门 / 排雷 / 排序 / 价位回填）——工程试算用，非选股产出，非投资建议。

依据：docs/计划/2026-09-17_金字塔选股_需求对齐与规划.md §10 D2 收敛机制。
本脚本只实现 **程序层**，不接任何 LLM：
  1. 全 A 广度池扫描（主档 K 线；688 科创板 volume 100× 缺陷校正）
  2. 硬排雷（涨停 / 极高位 / ST；有 per-stock 慢变字段时加 龙虎榜 / 财报红旗 / 供给面）
  3. 分层闸门 T1 / T2 / T3（作用于 D 当日已实现 bar）
  4. 量价自证排序（公开公式，0~100）
  5. 价位回填（D+1 开盘附近限价 + 回踩 MA5 加仓口径；单调性夹逼）
  6. 阈值敏感性试算（每个阈值 ±20%，报告池 / 各层数量变化）
  7. 外部票单覆盖统计（--named：任意代码清单在本口径下落到哪一档，只统计不评价）

数据铁律（A4 §8 H1/H2 未修）：
  · 唯一真值 = data/master/kline/<code>.parquet，as_of ≤ D，防未来
  · H2：688 volume 被 ×100 → 用 amount/close 反推股数；amount 缺失的行按"与邻近可信行量级比 >20×"判定 ÷100
  · H1：per-stock json 为午盘注入版 → 只取慢变字段 financing / lhb_veto / financial；禁用 snapshot 快变字段

所有阈值在 THRESH 写死为默认值，全部可由 CLI 覆盖（--set 键=值），并原样写入产物。

用法：
  python tools/experimental/pyramid_select_v1.py --date 2026-09-17 --data-root <主仓> \
      --out-json out.json [--out-csv out.csv] [--named 600000,000001] [--sensitivity] [--set 池量比=1.3 --set T1量比=1.8]
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ───────────────────────── 阈值（默认值；全部可被 --set 覆盖） ─────────────────────────
THRESH = {
    "最少K线根数": 120,          # 上市 <120 日代理
    "流动性_20日均额": 1e8,      # 元；近 20 日日均成交额下限
    "池量比": 1.2,               # 池：量比 ≥
    "池涨幅下限": 1.0,           # 池：涨跌幅% ≥
    "池涨幅上限": 9.0,           # 池：涨跌幅% <（未近涨停）
    "池pos60上限": 0.95,         # 池：pos60 <
    "T1量比": 1.5,
    "T2量比": 1.2,
    "T3量比": 1.0,
    "极高位pos60": 0.95,
    "极高位距高": -0.02,         # 距 20/60 日高 ≥ −2%
    "涨停判定_主板": 9.7,        # 涨跌幅% ≥
    "涨停判定_20%板": 19.5,
    "爆量量比": 3.0,
    "连续放量量比": 1.2,
}
RULES = {
    "宇宙": "全 A：代码前缀 00/30/60/68；剔 92x 北交所、ST/*ST/退市、as_of 前 K 线不足、当日无 bar、流动性不足",
    "量比口径": "当日成交量 / 前 5 日成交量均值（不含当日）；688 用 amount/close 反推股数",
    "池基础条件": "收阳(close>open) ∧ close>MA5 ∧ 池涨幅下限≤涨跌幅<池涨幅上限 ∧ 量比≥池量比 ∧ pos60<池pos60上限",
    "分层闸门": {
        "T1": "量比≥T1量比 ∧ 收阳 ∧ close>MA5 ∧ close>前日高",
        "T2": "量比≥T2量比 ∧ 收阳 ∧ close>MA5",
        "T3": "(收阳 ∧ close>MA5) ∨ 量比≥T3量比",
        "说明": "层级在池内判定；池条件与 T2 条件可能重叠，重叠时 T3 为空属正常（弱市日放宽池条件后 T3 才有意义）",
    },
    "硬排雷": {
        "涨停不可买": "涨跌幅 ≥ 涨停判定（主板 9.7 / 20% 板 19.5）",
        "极高位": "pos60 ≥ 极高位pos60 ∧ 距20日高 ≥ 极高位距高 ∧ 距60日高 ≥ 极高位距高",
        "龙虎榜否决": "per-stock lhb_veto.triggered（慢变字段）",
        "财报高危红旗": "per-stock financial.flags_detail 严重度=高 任一命中 或 评级=差",
        "供给面": "per-stock financing.固定一问 任一为 true",
        "ST": "名称含 ST 或 退",
        "覆盖边界": "无 per-stock json 的票，龙虎榜/财报/供给面 标『无档』不视为命中",
    },
    "量价自证评分(0~100)": {
        "放量分": "30 × clip((量比−1.0)/1.5, 0, 1)",
        "收阳幅度分": "20 × clip(涨跌幅% / 6, 0, 1)",
        "站上强度分": "20 × clip((close/MA5−1)/0.05, 0, 1)",
        "位置分": "15：0.30≤pos60≤0.80 满分；pos60<0.30 得 7.5；>0.80 得 0",
        "距60日高分": "15：距60日高 ≤ −8% 满分；≤ −4% 得 7.5；否则 0",
        "连续放量减分": "−10：近 3 日（含当日）量比均 > 连续放量量比",
        "爆量减分": "−15：量比 > 爆量量比",
        "说明": "全部由 K 线派生，不含任何 LLM 分；同分按量比降序",
    },
    "价位回填": {
        "首入场(D+1 开盘附近限价)": "挂单区间 [D 收, D 收×1.01]；不追高上限 D 收×1.02；止损 min(D 低, MA5)×0.99",
        "加仓(回踩 MA5 限价)": "加仓挂单 = MA5（MA5<收盘时给）；加仓止损 = min(MA20, 加仓价×0.98)",
        "单调性": "止损 < 挂单下限 ≤ 挂单上限 ≤ 不追高上限（程序夹逼）",
    },
    "评分档位含义(工程占位·待 forward 标定)": {"≥70": "量价自证强", "50~69": "中", "30~49": "弱", "<30": "不入候选"},
}


# ───────────────────────── 路径 ─────────────────────────
def detect_data_root() -> Path:
    here = Path(__file__).resolve()
    wt_root = here.parents[2]
    if (wt_root / "data/master/kline").is_dir():
        return wt_root
    try:
        common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=str(wt_root), text=True).strip()
        main_root = Path(common).resolve().parent
        if (main_root / "data/master/kline").is_dir():
            return main_root
    except Exception:
        pass
    return wt_root


# ───────────────────────── K 线读取与 688 校正 ─────────────────────────
def is_20pct_board(code: str) -> bool:
    return code.startswith(("300", "301", "688", "689"))


def fix_volume(df: pd.DataFrame, code: str) -> pd.Series:
    """H2 修正：688 用 amount/close 反推股数；amount 缺失行按量级判定 ÷100。"""
    vol = df["volume"].astype(float).copy()
    if not code.startswith("68"):
        return vol
    est = df["amount"].astype(float) / df["close"].astype(float)
    ok = est.notna() & (est > 0)
    fixed = vol.copy()
    fixed[ok] = est[ok]
    if ok.any():
        ref = float(np.nanmedian(est[ok].tail(20))) if ok.tail(20).any() else float(np.nanmedian(est[ok]))
        bad = (~ok) & (vol > 20 * ref)
        fixed[bad] = vol[bad] / 100.0
    else:
        fixed.iloc[-30:] = vol.iloc[-30:] / 100.0
    return fixed


def metrics(df: pd.DataFrame, code: str, asof: pd.Timestamp, th: dict) -> dict:
    df = df[pd.to_datetime(df["date"]) <= asof].reset_index(drop=True)
    if len(df) < th["最少K线根数"]:
        return {"skip": f"K线不足{th['最少K线根数']}根(n={len(df)})"}
    if pd.Timestamp(df["date"].iloc[-1]) != asof:
        return {"skip": f"当日无bar(last={pd.Timestamp(df['date'].iloc[-1]).date()})"}
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    v = fix_volume(df, code).to_numpy(float)
    amt = df["amount"].to_numpy(float)
    close, open_, high, low = float(c[-1]), float(o[-1]), float(h[-1]), float(lo[-1])
    prev_close, prev_high = float(c[-2]), float(h[-2])
    pct = (close / prev_close - 1) * 100
    ma5, ma20, ma60 = (float(np.mean(c[-n:])) for n in (5, 20, 60))

    def vr_at(k: int) -> float:
        b = float(np.mean(v[-6 - k:-1 - k]))
        return float(v[-1 - k] / b) if b > 0 else float("nan")

    vr = vr_at(0)
    hi60, lo60, hi20 = float(np.max(h[-60:])), float(np.min(lo[-60:])), float(np.max(h[-20:]))
    pos60 = (close - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
    amt20 = float(np.nanmean(amt[-20:])) if np.isfinite(amt[-20:]).any() else float(np.mean(v[-20:] * c[-20:]))
    lim = th["涨停判定_20%板"] if is_20pct_board(code) else th["涨停判定_主板"]
    return {
        "open": open_, "high": high, "low": low, "close": close, "prev_close": prev_close, "prev_high": prev_high,
        "pct": pct, "ma5": ma5, "ma20": ma20, "ma60": ma60, "vol_ratio": vr, "vr_hist3": [vr, vr_at(1), vr_at(2)],
        "pos60": pos60, "dist60high": close / hi60 - 1, "dist20high": close / hi20 - 1, "amt20": amt20,
        "收阳": close > open_, "站上MA5": close > ma5, "站上前日高": close > prev_high,
        "涨停": pct >= lim, "跌停": pct <= -lim, "ret5": c[-1] / c[-6] - 1, "ret20": c[-1] / c[-21] - 1,
    }


# ───────────────────────── 慢变字段 / 排雷 / 闸门 / 评分 / 价位 ─────────────────────────
def load_slow_fields(analysis_dir: Path, code: str) -> dict:
    p = analysis_dir / f"{code}.json"
    if not p.exists():
        return {"has_json": False}
    try:
        d = json.load(open(p))
    except Exception as e:  # noqa
        return {"has_json": False, "err": str(e)}
    fin = d.get("financing") or {}
    q = fin.get("固定一问") or {}
    lhb = d.get("lhb_veto") or {}
    fr = d.get("financial") or {}
    fd = fr.get("flags_detail") or []
    return {
        "has_json": True, "供给面": dict(q), "供给面命中": any(bool(v) for v in q.values()),
        "lhb_triggered": bool(lhb.get("triggered")), "lhb_reason": lhb.get("reason"),
        "财报评级": fr.get("评级"),
        "财报红旗_高": [f.get("code") for f in fd if f.get("命中") and f.get("严重度") == "高"],
        "财报红旗_全部": [f.get("code") for f in fd if f.get("命中")],
    }


def hard_veto(m: dict, name: str, slow: dict, th: dict) -> list[str]:
    r = []
    if "ST" in name.upper() or "退" in name:
        r.append("ST/退市")
    if m["涨停"]:
        r.append("涨停不可买")
    if m["pos60"] >= th["极高位pos60"] and m["dist20high"] >= th["极高位距高"] and m["dist60high"] >= th["极高位距高"]:
        r.append("极高位")
    if slow.get("has_json"):
        if slow.get("lhb_triggered"):
            r.append("龙虎榜否决")
        if slow.get("财报红旗_高") or slow.get("财报评级") == "差":
            r.append("财报高危红旗")
        if slow.get("供给面命中"):
            r.append("供给面")
    return r


def in_pool(m: dict, th: dict) -> bool:
    return bool(m["收阳"] and m["站上MA5"] and th["池涨幅下限"] <= m["pct"] < th["池涨幅上限"]
                and m["vol_ratio"] >= th["池量比"] and m["pos60"] < th["池pos60上限"])


def tier(m: dict, th: dict) -> str | None:
    vr = m["vol_ratio"]
    if m["收阳"] and m["站上MA5"] and m["站上前日高"] and vr >= th["T1量比"]:
        return "T1"
    if m["收阳"] and m["站上MA5"] and vr >= th["T2量比"]:
        return "T2"
    if (m["收阳"] and m["站上MA5"]) or vr >= th["T3量比"]:
        return "T3"
    return None


def score(m: dict, th: dict) -> tuple[float, dict]:
    vr = m["vol_ratio"]
    parts = {
        "放量": round(30 * float(np.clip((vr - 1.0) / 1.5, 0, 1)), 1),
        "收阳幅度": round(20 * float(np.clip(m["pct"] / 6, 0, 1)), 1),
        "站上强度": round(20 * float(np.clip((m["close"] / m["ma5"] - 1) / 0.05, 0, 1)), 1),
        "位置": 15.0 if 0.30 <= m["pos60"] <= 0.80 else (7.5 if m["pos60"] < 0.30 else 0.0),
        "距60高": 15.0 if m["dist60high"] <= -0.08 else (7.5 if m["dist60high"] <= -0.04 else 0.0),
        "连续放量减分": -10.0 if all(x > th["连续放量量比"] for x in m["vr_hist3"] if np.isfinite(x)) else 0.0,
        "爆量减分": -15.0 if vr > th["爆量量比"] else 0.0,
    }
    return round(sum(parts.values()), 1), parts


def fill_prices(m: dict) -> dict:
    close, low, ma5, ma20 = m["close"], m["low"], m["ma5"], m["ma20"]
    entry_lo, entry_hi, cap = close, close * 1.01, close * 1.02
    stop = min(low, ma5) * 0.99
    if stop >= entry_lo:
        stop = entry_lo * 0.98
    cap = max(cap, entry_hi)
    add = None
    if ma5 < close:
        add_stop = min(ma20, ma5 * 0.98)
        if add_stop >= ma5:
            add_stop = ma5 * 0.98
        add = {"加仓挂单_回踩MA5": round(ma5, 2), "加仓止损": round(add_stop, 2)}
    r2 = lambda x: round(float(x), 2)  # noqa
    return {"挂单价下限": r2(entry_lo), "挂单价上限": r2(entry_hi), "不追高上限": r2(cap), "止损价": r2(stop),
            "止损幅度%": round((stop / entry_lo - 1) * 100, 2), "加仓口径": add,
            "单调性": bool(stop < entry_lo <= entry_hi <= cap)}


# ───────────────────────── 阈值应用（可重复施加于已算好的行） ─────────────────────────
def apply_thresholds(rows: list[dict], th: dict) -> dict:
    """对已算好 metrics 的宇宙行施加阈值，返回 池/排雷/各层 统计与排序表。"""
    pool, vetoed, by_tier = [], [], {"T1": [], "T2": [], "T3": [], None: []}
    veto_counts: dict[str, int] = {}
    for r in rows:
        m = r["m"]
        if m["amt20"] < th["流动性_20日均额"] and not r["named"]:
            continue
        r["in_pool"] = in_pool(m, th)
        r["vetos"] = hard_veto(m, r["name"], r["slow"], th)
        r["tier"] = tier(m, th)
        r["score"], r["score_parts"] = score(m, th)
        if not r["in_pool"]:
            continue
        pool.append(r)
        if r["vetos"]:
            vetoed.append(r)
            for v in r["vetos"]:
                veto_counts[v] = veto_counts.get(v, 0) + 1
        else:
            by_tier[r["tier"]].append(r)
    for t in by_tier:
        by_tier[t].sort(key=lambda r: (-r["score"], -r["m"]["vol_ratio"]))
    scores = np.array([r["score"] for r in pool if not r["vetos"]]) if pool else np.array([])
    dist = {}
    if scores.size:
        dist = {"n": int(scores.size), "均值": round(float(scores.mean()), 1), "中位": round(float(np.median(scores)), 1),
                "分位": {q: round(float(np.percentile(scores, q)), 1) for q in (10, 25, 50, 75, 90)},
                "档位计数": {"≥70": int((scores >= 70).sum()), "50~69": int(((scores >= 50) & (scores < 70)).sum()),
                             "30~49": int(((scores >= 30) & (scores < 50)).sum()), "<30": int((scores < 30).sum())}}
    return {"池票数": len(pool), "排雷踢出": len(vetoed), "排雷原因计数": veto_counts, "过闸": len(pool) - len(vetoed),
            "各层": {t: len(by_tier[t]) for t in ("T1", "T2", "T3")}, "评分分布": dist, "_by_tier": by_tier, "_vetoed": vetoed}


def sensitivity(rows: list[dict], th: dict) -> list[dict]:
    """每个数值阈值 ±20%，报告 池/T1/T2/T3 数量。"""
    keys = ["流动性_20日均额", "池量比", "池涨幅下限", "池涨幅上限", "池pos60上限", "T1量比", "T2量比", "极高位pos60"]
    base = apply_thresholds(copy.deepcopy(rows), th)
    out = [{"阈值": "基线", "取值": None, "池": base["池票数"], "排雷": base["排雷踢出"], **base["各层"]}]
    for k in keys:
        for mult in (0.8, 1.2):
            th2 = dict(th)
            th2[k] = th[k] * mult
            if k == "池pos60上限" or k == "极高位pos60":
                th2[k] = min(th2[k], 1.0)
            res = apply_thresholds(copy.deepcopy(rows), th2)
            out.append({"阈值": k, "取值": round(th2[k], 4), "倍数": mult, "池": res["池票数"], "排雷": res["排雷踢出"], **res["各层"]})
    return out


def compact(r: dict, with_prices: bool = False) -> dict:
    m = r["m"]
    d = {"code": r["code"], "name": r["name"], "industry": r["industry"], "named": r["named"], "tier": r["tier"],
         "score": r["score"], "score_parts": r["score_parts"], "vetos": r["vetos"], "in_pool": r.get("in_pool"),
         "close": m["close"], "pct": round(m["pct"], 2), "vol_ratio": round(m["vol_ratio"], 2),
         "vr_hist3": [round(x, 2) if np.isfinite(x) else None for x in m["vr_hist3"]],
         "pos60": round(m["pos60"], 3), "dist60high": round(m["dist60high"], 4), "dist20high": round(m["dist20high"], 4),
         "ma5": round(m["ma5"], 3), "ma20": round(m["ma20"], 3), "prev_high": m["prev_high"], "low": m["low"],
         "amt20亿": round(m["amt20"] / 1e8, 2), "ret5": round(m["ret5"], 4), "收阳": m["收阳"], "站上MA5": m["站上MA5"],
         "站上前日高": m["站上前日高"], "slow": r["slow"]}
    if with_prices:
        d["prices"] = fill_prices(m)
    return d


# ───────────────────────── 主流程 ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="2026-09-17")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--named", default="", help="逗号分隔外部票单：同口径落档统计，不给特权、不评价")
    ap.add_argument("--set", action="append", default=[], help="覆盖阈值，如 --set 池量比=1.3")
    ap.add_argument("--sensitivity", action="store_true", help="做 ±20%% 阈值敏感性试算")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-csv", default=None)
    args = ap.parse_args()

    th = dict(THRESH)
    for kv in args.set:
        k, v = kv.split("=", 1)
        if k not in th:
            sys.exit(f"未知阈值 {k}，可用：{list(th)}")
        th[k] = type(THRESH[k])(float(v)) if isinstance(THRESH[k], int) else float(v)

    root = Path(args.data_root) if args.data_root else detect_data_root()
    asof = pd.Timestamp(args.date)
    kdir, adir = root / "data/master/kline", root / "data/analysis" / args.date
    names = json.load(open(root / "config/code_name.json"))
    industry = json.load(open(root / "config/code_industry.json"))
    named = [c.strip() for c in args.named.split(",") if c.strip()]
    print(f"[root] {root} kline={kdir.is_dir()} analysis={adir.is_dir()} thresholds={th}", file=sys.stderr)

    files = sorted(glob.glob(str(kdir / "*.parquet")))
    stats = {"文件总数": len(files), "剔_非00/30/60/68": 0, "剔_ST": 0, "剔_K线不足": 0, "剔_当日无bar": 0,
             "剔_流动性": 0, "有效宇宙": 0, "有per-stock档": 0}
    rows, named_rows = [], []
    mkt = {"pcts": [], "up": 0, "dn": 0, "lu": 0, "ld": 0}
    for f in files:
        code = os.path.basename(f)[:6]
        is_named = code in named
        if not code.startswith(("00", "30", "60", "68")):
            stats["剔_非00/30/60/68"] += 1
            continue
        name = names.get(code, code)
        try:
            df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        except Exception:
            continue
        m = metrics(df, code, asof, th)
        if "skip" in m:
            stats["剔_K线不足" if "不足" in m["skip"] else "剔_当日无bar"] += 1
            if is_named:
                named_rows.append({"code": code, "name": name, "skip": m["skip"]})
            continue
        mkt["pcts"].append(m["pct"]); mkt["up"] += m["pct"] > 0; mkt["dn"] += m["pct"] < 0
        mkt["lu"] += m["涨停"]; mkt["ld"] += m["跌停"]
        if ("ST" in name.upper() or "退" in name) and not is_named:
            stats["剔_ST"] += 1
            continue
        if m["amt20"] < th["流动性_20日均额"]:
            stats["剔_流动性"] += 1
            if not is_named:
                continue
        else:
            stats["有效宇宙"] += 1
        slow = load_slow_fields(adir, code)
        stats["有per-stock档"] += bool(slow.get("has_json"))
        rows.append({"code": code, "name": name, "industry": industry.get(code), "named": is_named, "m": m, "slow": slow})

    pcts = np.array(mkt["pcts"])
    market = {"全A等权涨跌%": round(float(pcts.mean()), 3), "中位数%": round(float(np.median(pcts)), 3),
              "上涨家数": int(mkt["up"]), "下跌家数": int(mkt["dn"]), "上涨占比": round(mkt["up"] / max(1, pcts.size), 3),
              "涨停数": int(mkt["lu"]), "跌停数": int(mkt["ld"]), "样本数": int(pcts.size),
              "口径": "主档 K 线当日全部 00/30/60/68 有 bar 的票（含 ST、不限流动性）；涨停按 涨停判定 阈值"}

    res = apply_thresholds(rows, th)
    by_tier, vetoed = res.pop("_by_tier"), res.pop("_vetoed")
    # 外部票单落档统计
    named_stat = {"票数": len(named), "落档": {}, "明细": []}
    for r in rows:
        if r["named"]:
            bucket = ("排雷:" + "/".join(r["vetos"])) if (r["in_pool"] and r["vetos"]) else \
                (f"入池·{r['tier']}" if r["in_pool"] else (f"未入池·{r['tier'] or '无层'}"))
            named_stat["落档"][bucket] = named_stat["落档"].get(bucket, 0) + 1
            named_stat["明细"].append({"code": r["code"], "name": r["name"], "in_pool": r["in_pool"], "tier": r["tier"],
                                       "score": r["score"], "vetos": r["vetos"], "收阳": r["m"]["收阳"],
                                       "站上MA5": r["m"]["站上MA5"], "量比": round(r["m"]["vol_ratio"], 2), "pos60": round(r["m"]["pos60"], 2)})
    for nr in named_rows:
        named_stat["落档"]["无bar/K线不足"] = named_stat["落档"].get("无bar/K线不足", 0) + 1
        named_stat["明细"].append(nr)

    out = {
        "标注": "D2 原型·工程试算·非选股产出·非投资建议",
        "date": args.date, "version": "pyramid_select_v1 (D2 程序层原型)",
        "数据口径": "K 线收盘为准；688 成交量已校正（amount/close 反推）；per-stock 仅取慢变字段；H1/H2 待修；防未来 as_of ≤ date",
        "thresholds": th, "rules": RULES, "universe_stats": stats, "market": market,
        "pool_stats": res,
        "ranked": {t: [compact(r, with_prices=True) for r in by_tier[t]] for t in ("T1", "T2", "T3")},
        "vetoed": [compact(r) for r in vetoed],
        "named_coverage": named_stat,
    }
    if args.sensitivity:
        out["sensitivity_±20%"] = sensitivity(rows, th)
    # 价位回填单调性自检
    bad = [r["code"] for t in ("T1", "T2", "T3") for r in out["ranked"][t] if not r["prices"]["单调性"]]
    out["价位回填单调性违规"] = bad
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(args.out_json, "w"), ensure_ascii=False, indent=1, default=lambda x: None)
    if args.out_csv:
        pd.DataFrame([{k: v for k, v in compact(r).items() if k not in ("slow", "score_parts", "vr_hist3")} for r in rows]).to_csv(args.out_csv, index=False)
    print(json.dumps({"universe": stats, "market": market, "pool": res, "named": named_stat["落档"],
                      "单调性违规": bad, "sensitivity": out.get("sensitivity_±20%")}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
