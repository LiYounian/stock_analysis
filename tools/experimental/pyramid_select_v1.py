#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""金字塔选股 · ClaudeCode 版 · v1（程序层：召回池 / 硬排雷 / 分层闸门 / 量价自证排序 / 价位回填）

⚠️ 测试环境研究模拟，非投资建议。实验脚本，不进生产调度、不改生产代码。

设计依据：docs/计划/2026-09-17_金字塔选股_需求对齐与规划.md §10 D2 收敛机制。
  · 闸门作用于 D 当日已实现 bar（放量 / 收阳 / 站上 MA5 / 站上前日高 / 未近涨停）
  · 广度共享池（全 A）而非 3~5 只 shortlist；分层 T1/T2/T3 保底取满 2~3 只
  · 排序 = 量价自证（公开公式，不含任何 LLM 建议分）；LLM 只做否决 + 解释（脚本外）
  · 首入场 = D+1 开盘附近限价；回踩 MA5 = 加仓口径

数据铁律（A4 §8 H1/H2 未修）：
  · 唯一真值 = 主档 K 线 data/master/kline/<code>.parquet，as_of ≤ D，防未来
  · H2：688 科创板 volume 自 09-08 起（及 08-13~09-04 部分回退行）被 ×100 → 一律用 amount/close 反推股数；
        amount 缺失的行按"与邻近可信行量级比 >20×"判定并 ÷100
  · H1：per-stock json 为午盘注入版 → 只取慢变字段 financing / lhb_veto / financial 红旗 / holder；
        禁用 snapshot.close / pct / vol_ratio

用法：
  python tools/experimental/pyramid_select_v1.py --date 2026-09-17 \
      --data-root /path/to/主仓 --out-json ... --out-csv ...
所有阈值写死在 PARAMS，产物里原样列出。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ───────────────────────── 参数（写死并在产物中列出） ─────────────────────────
PARAMS = {
    "版本": "pyramid_select_v1 · 2026-09-17",
    "宇宙": "全 A：代码前缀 00/30/60/68；剔 92x 北交所、ST/*ST/退市、as_of 前 K 线 <120 根（上市<120日代理）、当日无 bar",
    "基础流动性": "近 20 日日均成交额 ≥ 1 亿元（避免不可成交微盘；不在 §10 明文，为本版补充）",
    "量比口径": "当日成交量 / 前 5 日成交量均值（不含当日）；688 一律 amount/close 反推股数后再算",
    "池基础条件": "收阳(close>open) 且 close>MA5 且 1%≤涨跌幅<9% 且 量比≥1.2 且 pos60<0.95（v1 首跑 量比≥1.0/涨幅>0/5000万 得 640 只过宽，收紧至此）",
    "涨跌幅上限_池": 9.0,
    "涨跌幅下限_池": 1.0,
    "量比下限_池": 1.2,
    "保底说明": "池条件已 ⊇ T2；T3 层仅在 T1+T2 不足 3 只的弱市日放宽 量比≥1.0/涨幅>0 重跑启用",
    "硬排雷": {
        "涨停不可买": "涨跌幅 ≥ 9.7%（主板）/ ≥ 19.5%（30x/68x 20% 板）",
        "极高位": "pos60 ≥ 0.95 且 距20日高 ≥ −2% 且 距60日高 ≥ −2%",
        "龙虎榜否决": "per-stock lhb_veto.triggered == true（慢变字段，可用）",
        "财报高危红旗": "per-stock financial.flags_detail 严重度=高 任一命中；或 评级=差",
        "供给面": "per-stock financing.固定一问 任一为 true（存续可转债 / 推进中定增 / 未来90日解禁）",
        "ST": "名称含 ST 或 退",
        "说明": "无 per-stock json 的池票，龙虎榜/财报/供给面三项标『无档·待核』，不视为命中",
    },
    "分层闸门": {
        "T1": "量比≥1.5 且 收阳 且 close>MA5 且 close>前日高",
        "T2": "量比≥1.2 且 收阳 且 close>MA5",
        "T3": "(收阳 且 close>MA5) 或 量比≥1.0",
    },
    "量比阈值": {"T1": 1.5, "T2": 1.2, "T3": 1.0},
    "量价自证评分(0~100)": {
        "放量分": "30 × clip((量比−1.0)/1.5, 0, 1)",
        "收阳幅度分": "20 × clip(涨跌幅% / 6, 0, 1)",
        "站上强度分": "20 × clip((close/MA5−1)/0.05, 0, 1)",
        "位置分": "15：0.30≤pos60≤0.80 得满分；pos60<0.30 得 7.5；pos60>0.80 得 0",
        "距60日高分": "15：距60日高 ≤ −8% 得满分；≤ −4% 得 7.5；否则 0",
        "连续放量减分": "−10：近 3 日（含当日）量比均 >1.2（追高风险）",
        "爆量减分": "−15：量比 > 3.0（一日爆量·派发嫌疑）",
        "说明": "全部由 K 线派生；不含任何 LLM 建议分；同分按量比降序",
    },
    "价位回填(首入场·D+1 开盘附近限价)": {
        "挂单价区间": "[D 收盘, D 收盘×1.01]",
        "不追高上限": "D 收盘×1.02",
        "止损价": "min(D 低点, MA5)×0.99",
        "单调性": "止损 < 挂单下限 ≤ 挂单上限 ≤ 不追高上限（程序夹逼）",
    },
    "价位回填(加仓·回踩 MA5 限价)": {
        "加仓挂单": "MA5（若 MA5≥收盘则不给加仓价）",
        "加仓止损": "min(MA20, 加仓挂单×0.98)",
    },
    "评分档位含义": {
        "≥70": "量价自证强：放量 + 明显收阳 + 站稳 + 中低位，A2 赢票形态",
        "50~69": "量价自证中：具备 2~3 项支撑，需板块/催化补强",
        "30~49": "量价自证弱：勉强过 T3，仅弱市保底用",
        "<30": "不建议作为买入候选",
    },
}
MIN_BARS = 120
LIQ_MIN_AMOUNT_20D = 1e8


# ───────────────────────── 路径 ─────────────────────────
def detect_data_root() -> Path:
    """worktree 无 data/master/kline 时回退到 git 主仓。"""
    here = Path(__file__).resolve()
    wt_root = here.parents[2]
    if (wt_root / "data/master/kline").is_dir():
        return wt_root
    try:
        common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], cwd=str(wt_root), text=True).strip()
        main_root = Path(common).resolve().parent if common.endswith(".git") else Path(common).resolve().parent
        if (main_root / "data/master/kline").is_dir():
            return main_root
    except Exception:
        pass
    return wt_root


# ───────────────────────── K 线读取与 688 校正 ─────────────────────────
def limit_pct(code: str) -> float:
    return 0.20 if code.startswith(("300", "301", "688", "689")) else 0.10


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
        # 全无 amount：按 A4 结论自 09-08 起 ×100（保守：仅对最近 30 行 ÷100）
        fixed.iloc[-30:] = vol.iloc[-30:] / 100.0
    return fixed


def metrics(df: pd.DataFrame, code: str, asof: pd.Timestamp) -> dict | None:
    df = df[pd.to_datetime(df["date"]) <= asof].reset_index(drop=True)
    if len(df) < MIN_BARS:
        return {"skip": f"K线不足{MIN_BARS}根(n={len(df)})"}
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
    ma5, ma10, ma20, ma60 = (float(np.mean(c[-n:])) for n in (5, 10, 20, 60))
    base5 = float(np.mean(v[-6:-1]))
    vr = float(v[-1] / base5) if base5 > 0 else float("nan")

    def vr_at(k: int) -> float:  # 第 k 日前的量比
        b = float(np.mean(v[-6 - k:-1 - k]))
        return float(v[-1 - k] / b) if b > 0 else float("nan")

    vr_hist = [vr, vr_at(1), vr_at(2)]
    hi60, lo60 = float(np.max(h[-60:])), float(np.min(lo[-60:]))
    hi20 = float(np.max(h[-20:]))
    pos60 = (close - lo60) / (hi60 - lo60) if hi60 > lo60 else 0.5
    amt20 = float(np.nanmean(amt[-20:])) if np.isfinite(amt[-20:]).any() else float(np.mean(v[-20:] * c[-20:]))
    return {
        "open": open_, "high": high, "low": low, "close": close, "prev_close": prev_close, "prev_high": prev_high,
        "pct": pct, "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "vol_ratio": vr, "vr_hist3": vr_hist, "pos60": pos60,
        "dist60high": close / hi60 - 1, "dist20high": close / hi20 - 1,
        "amt20": amt20, "收阳": close > open_, "站上MA5": close > ma5, "站上前日高": close > prev_high,
        "涨停": pct >= (9.7 if limit_pct(code) == 0.10 else 19.5),
        "跌停": pct <= -(9.7 if limit_pct(code) == 0.10 else 19.5),
        "ret5": c[-1] / c[-6] - 1, "ret20": c[-1] / c[-21] - 1,
        "vol_raw": float(df["volume"].iloc[-1]), "vol_fixed": float(v[-1]),
    }


# ───────────────────────── 排雷 / 闸门 / 评分 ─────────────────────────
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
    flags_hi = [f.get("code") for f in (fr.get("flags_detail") or []) if f.get("命中") and f.get("严重度") == "高"]
    flags_all = [f.get("code") for f in (fr.get("flags_detail") or []) if f.get("命中")]
    return {
        "has_json": True,
        "供给面": {k: v for k, v in q.items()},
        "供给面命中": any(bool(v) for v in q.values()),
        "lhb_triggered": bool(lhb.get("triggered")),
        "lhb_reason": lhb.get("reason"),
        "财报评级": fr.get("评级"),
        "财报红旗_高": flags_hi,
        "财报红旗_全部": flags_all,
        "财报报告期": fr.get("报告期"),
        "holder": (d.get("holder") or {}).get("户数环比"),
    }


def hard_veto(m: dict, name: str, slow: dict) -> list[str]:
    r = []
    if "ST" in name.upper() or "退" in name:
        r.append("ST/退市")
    if m["涨停"]:
        r.append(f"涨停不可买(涨跌{m['pct']:.2f}%)")
    if m["pos60"] >= 0.95 and m["dist20high"] >= -0.02 and m["dist60high"] >= -0.02:
        r.append(f"极高位(pos60={m['pos60']:.2f},距60高{m['dist60high']*100:.1f}%)")
    if slow.get("has_json"):
        if slow.get("lhb_triggered"):
            r.append(f"龙虎榜否决({slow.get('lhb_reason')})")
        if slow.get("财报红旗_高") or slow.get("财报评级") == "差":
            r.append(f"财报高危红旗({slow.get('财报红旗_高') or slow.get('财报评级')})")
        if slow.get("供给面命中"):
            hit = [k for k, v in (slow.get("供给面") or {}).items() if v]
            r.append(f"供给面({'/'.join(hit)})")
    return r


def tier(m: dict) -> str | None:
    vr = m["vol_ratio"]
    if m["收阳"] and m["站上MA5"] and m["站上前日高"] and vr >= PARAMS["量比阈值"]["T1"]:
        return "T1"
    if m["收阳"] and m["站上MA5"] and vr >= PARAMS["量比阈值"]["T2"]:
        return "T2"
    if (m["收阳"] and m["站上MA5"]) or vr >= PARAMS["量比阈值"]["T3"]:
        return "T3"
    return None


def score(m: dict) -> tuple[float, dict]:
    vr = m["vol_ratio"]
    s_vr = 30 * float(np.clip((vr - 1.0) / 1.5, 0, 1))
    s_pct = 20 * float(np.clip(m["pct"] / 6, 0, 1))
    s_ma = 20 * float(np.clip((m["close"] / m["ma5"] - 1) / 0.05, 0, 1))
    p = m["pos60"]
    s_pos = 15.0 if 0.30 <= p <= 0.80 else (7.5 if p < 0.30 else 0.0)
    d = m["dist60high"]
    s_d60 = 15.0 if d <= -0.08 else (7.5 if d <= -0.04 else 0.0)
    pen_cons = -10.0 if all(x > 1.2 for x in m["vr_hist3"] if np.isfinite(x)) and len(m["vr_hist3"]) == 3 else 0.0
    pen_blow = -15.0 if vr > 3.0 else 0.0
    parts = {"放量": round(s_vr, 1), "收阳幅度": round(s_pct, 1), "站上强度": round(s_ma, 1), "位置": s_pos,
             "距60高": s_d60, "连续放量减分": pen_cons, "爆量减分": pen_blow}
    return round(sum(parts.values()), 1), parts


def fill_prices(m: dict) -> dict:
    """首入场 = D+1 开盘附近限价；加仓 = 回踩 MA5。单调性夹逼。"""
    close, low, ma5, ma20 = m["close"], m["low"], m["ma5"], m["ma20"]
    entry_lo = close
    entry_hi = close * 1.01
    cap = close * 1.02
    stop = min(low, ma5) * 0.99
    if stop >= entry_lo:
        stop = entry_lo * 0.98
    cap = max(cap, entry_hi)
    add = None
    if ma5 < close:
        add_entry = ma5
        add_stop = min(ma20, add_entry * 0.98)
        if add_stop >= add_entry:
            add_stop = add_entry * 0.98
        add = {"加仓挂单_回踩MA5": round(add_entry, 2), "加仓止损": round(add_stop, 2)}
    r3 = lambda x: round(float(x), 2)  # noqa
    return {"挂单价下限": r3(entry_lo), "挂单价上限": r3(entry_hi), "不追高上限": r3(cap), "止损价": r3(stop),
            "止损幅度%": round((stop / entry_lo - 1) * 100, 2), "加仓口径": add,
            "单调性": bool(stop < entry_lo <= entry_hi <= cap)}


# ───────────────────────── 主流程 ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-09-17")
    ap.add_argument("--data-root", default=None)
    ap.add_argument("--named", default="", help="逗号分隔的命名候选代码(四模式票)，同口径复核、不给特权")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-csv", default=None)
    args = ap.parse_args()

    root = Path(args.data_root) if args.data_root else detect_data_root()
    asof = pd.Timestamp(args.date)
    kdir = root / "data/master/kline"
    adir = root / "data/analysis" / args.date
    names = json.load(open(root / "config/code_name.json"))
    industry = json.load(open(root / "config/code_industry.json"))
    print(f"[root] {root}  kline={kdir.is_dir()}  analysis={adir.is_dir()}", file=sys.stderr)

    named = [c.strip() for c in args.named.split(",") if c.strip()]
    roster = {}
    for f in glob.glob(str(root / "data/sector_roster/*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for role, L in (d.get("roles") or {}).items():
            for it in L:
                roster.setdefault(it["code"], []).append({"板块": d["板块"], "角色": role, "选级": it.get("选级"), "理由": it.get("理由")})
    sector_ctx = {}
    try:
        sf = json.load(open(adir / "sector_focus.json"))
        for b in sf.get("重点板块池") or []:
            sector_ctx[b["板块"]] = {"focus_score": b.get("focus_score"), "冷热": b.get("冷热"), "新闻净催化": b.get("新闻净催化"),
                                   "涨停数": b.get("涨停数"), "动量_截面档": b.get("动量_截面档"), "来源": "sector_focus.json 17:31"}
        for b in (sf.get("消息驱动") or {}).get("利好板块") or []:
            sector_ctx.setdefault(b["board"], {})["消息驱动"] = f"利好·{b.get('强弱')}"
        sr = json.load(open(adir / "sector_regime.json"))
        for b in sr.get("板块") or []:
            sector_ctx.setdefault(b["板块"], {}).update({"regime冷热": b.get("冷热标签"), "拥挤分位": b.get("拥挤分位"), "板块均涨幅": b.get("板块均涨幅")})
    except Exception as e:  # noqa
        print(f"[warn] sector ctx: {e}", file=sys.stderr)
    avoid_scan = []
    files = sorted(glob.glob(str(kdir / "*.parquet")))
    universe_stats = {"文件总数": len(files), "剔_非00/30/60/68": 0, "剔_ST": 0, "剔_K线不足": 0, "剔_当日无bar": 0,
                      "剔_流动性": 0, "有效宇宙": 0}
    rows = []
    mkt_pcts, up, dn, limit_up, limit_dn = [], 0, 0, 0, 0
    for f in files:
        code = os.path.basename(f)[:6]
        is_named = code in named
        if not code.startswith(("00", "30", "60", "68")):
            universe_stats["剔_非00/30/60/68"] += 1
            continue
        name = names.get(code, code)
        try:
            df = pd.read_parquet(f, columns=["date", "open", "high", "low", "close", "volume", "amount"])
        except Exception:
            continue
        m = metrics(df, code, asof)
        if m is None or "skip" in m:
            key = "剔_K线不足" if (m and "不足" in m["skip"]) else "剔_当日无bar"
            universe_stats[key] += 1
            if is_named:
                rows.append({"code": code, "name": name, "named": True, "skip": m["skip"] if m else "读取失败"})
            continue
        # 大盘 β 统计（全宇宙、剔 ST 前，含当日有 bar 的所有票）
        mkt_pcts.append(m["pct"])
        up += m["pct"] > 0
        dn += m["pct"] < 0
        limit_up += m["涨停"]
        limit_dn += m["跌停"]
        if ("ST" in name.upper() or "退" in name) and not is_named:
            universe_stats["剔_ST"] += 1
            continue
        if m["amt20"] < LIQ_MIN_AMOUNT_20D and not is_named:
            universe_stats["剔_流动性"] += 1
            continue
        universe_stats["有效宇宙"] += 1
        # 规避候选扫描（池外）：涨停 / 极高位且爆量或收阴 / 连续 3 日放量且 pos60≥0.9
        if m["涨停"] or (m["pos60"] >= 0.95 and (m["vol_ratio"] > 2.0 or not m["收阳"])) \
                or (m["pos60"] >= 0.90 and all(x > 1.2 for x in m["vr_hist3"] if np.isfinite(x))):
            avoid_scan.append({"code": code, "name": name, "industry": industry.get(code), "pct": round(m["pct"], 2),
                               "vol_ratio": round(m["vol_ratio"], 2), "pos60": round(m["pos60"], 2), "收阳": m["收阳"],
                               "dist60high": round(m["dist60high"], 4), "amt20亿": round(m["amt20"] / 1e8, 2), "ret5": round(m["ret5"] * 100, 1),
                               "vr_hist3": [round(x, 2) if np.isfinite(x) else None for x in m["vr_hist3"]], "named": is_named,
                               "roster": roster.get(code)})
        in_pool = (m["收阳"] and m["站上MA5"] and PARAMS["涨跌幅下限_池"] <= m["pct"] < PARAMS["涨跌幅上限_池"]
                   and m["vol_ratio"] >= PARAMS["量比下限_池"] and m["pos60"] < 0.95)
        if not (in_pool or is_named):
            continue
        slow = load_slow_fields(adir, code)
        vetos = hard_veto(m, name, slow)
        t = tier(m)
        sc, parts = score(m)
        rows.append({
            "code": code, "name": name, "industry": industry.get(code), "named": is_named, "in_pool": bool(in_pool),
            **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if k != "vr_hist3"},
            "vr_hist3": [round(x, 2) if np.isfinite(x) else None for x in m["vr_hist3"]],
            "tier": t, "score": sc, "score_parts": parts, "vetos": vetos, "veto": bool(vetos),
            "slow": slow, "prices": fill_prices(m), "roster": roster.get(code),
            "sector_ctx": sector_ctx.get(industry.get(code) or "", {}),
        })

    mkt = {
        "全A等权涨跌%": round(float(np.mean(mkt_pcts)), 3), "中位数%": round(float(np.median(mkt_pcts)), 3),
        "上涨家数": int(up), "下跌家数": int(dn), "上涨占比": round(up / max(1, len(mkt_pcts)), 3),
        "涨停数": int(limit_up), "跌停数": int(limit_dn), "样本数": len(mkt_pcts),
        "口径": "主档 K 线 as_of 当日全部 00/30/60/68 有 bar 的票；涨停=涨跌≥9.7%(主板)/19.5%(20%板)",
    }
    pool = [r for r in rows if r.get("in_pool")]
    passed = [r for r in pool if not r["veto"]]
    by_tier = {t: sorted([r for r in passed if r["tier"] == t], key=lambda r: (-r["score"], -r["vol_ratio"]))
               for t in ("T1", "T2", "T3")}
    out = {
        "date": args.date, "version": PARAMS["版本"], "免责": "测试环境研究模拟，非投资建议。",
        "数据口径": "K 线收盘为准；688 成交量已校正（amount/close 反推）；per-stock 仅取慢变字段；H1/H2 待修",
        "params": PARAMS, "universe_stats": universe_stats, "market": mkt,
        "pool_stats": {
            "池票数": len(pool), "排雷踢出": len(pool) - len(passed), "过闸": len(passed),
            "各层": {t: len(v) for t, v in by_tier.items()},
            "排雷原因分布": pd.Series([v.split("(")[0] for r in pool for v in r["vetos"]]).value_counts().to_dict(),
            "命名候选复核": [{"code": r["code"], "name": r["name"], "in_pool": r.get("in_pool"), "tier": r.get("tier"),
                              "score": r.get("score"), "vetos": r.get("vetos"), "skip": r.get("skip")}
                             for r in rows if r.get("named")],
        },
        "ranked": {t: [{k: r[k] for k in ("code", "name", "industry", "score", "score_parts", "vol_ratio", "pct", "pos60",
                                          "dist60high", "close", "ma5", "prev_high", "low", "ma20", "amt20", "ret5", "ret20", "vr_hist3", "named", "slow", "prices", "roster", "sector_ctx")}
                       for r in v] for t, v in by_tier.items()},
        "avoid_scan": sorted(avoid_scan, key=lambda r: -r["amt20亿"])[:60],
        "sector_ctx": sector_ctx,
        "vetoed": [{k: r[k] for k in ("code", "name", "industry", "score", "tier", "vol_ratio", "pct", "pos60",
                                      "dist60high", "vetos", "named")} for r in pool if r["veto"]],
    }
    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(args.out_json, "w"), ensure_ascii=False, indent=1, default=lambda x: None if x != x else str(x))
    if args.out_csv:
        flat = [{k: v for k, v in r.items() if k not in ("slow", "prices", "score_parts", "vr_hist3")} for r in rows]
        pd.DataFrame(flat).to_csv(args.out_csv, index=False)
    print(json.dumps({"universe": universe_stats, "market": mkt, "pool": out["pool_stats"]}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
