"""B · 基本面 veto 误杀 forward-shadow（non-gating advisory，纯记录）。

诊断与设计见 docs/计划/2026-09-16_选股规避能力整改_诊断与回测设计.md（§2B/§3/§6）。

统筹裁决 scope（**消费已有基建·不重造**）：
  · 价量强底座 = 当日 `量价放量.json` / `最强选股.json` 的入选清单（应用层已产出，本层只读）。
  · veto/超买底座 = 当日 `多策略命中闸门.json` 的 `票[]`：每票带 `veto`（应用/罚分/归因，
    含财报高危红旗）+ `过热闸`（触发/沉底/剔除 + 轴{超买共振/基本面空心/涨幅透支/…}）。
    winner_rate 超买经 过热闸「超买共振」轴体现；业绩预告负经 veto 归因/基本面空心轴体现。
  · 前向绝对收益 = 主档 K 线 close（`market.load_kline`，不触网），入场日 D→D+1/D+2 复利收益%。

逻辑（预注册·落盘后只读不改·append-only）：
  记录当日「**价量强（命中量价放量/最强选股）但被基本面 veto / 超买规避**」的票 → 落 advisory
  + 记其入场→D+1/D+2 **绝对收益**（forward 累积，未到期留 null 待回填）→ 判 veto 是净避损还是净漏利。

铁律：
  · **non-gating · forward-only · 纯记录**——绝不动 live veto、绝不进任何生产选股决策。
  · 数据仅 6 天历史标签（`多策略命中闸门.json` 起 2026-08-11）→ **只能 forward 攒**，≥120 样本才谈 validate。
  · 防未来/防偷看：只用 ≤当日已披露视图/闸门 + ≤到期 K 线；veto 命中口径预注册·只读不改；样本 <120 只报 N。
⚠️ 测试环境研究模拟，非投资建议。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger("research.veto_track")

# 价量强来源视图（预注册）
STRONG_VIEWS = ("量价放量", "最强选股")
DEFAULT_HORIZONS = (1, 2)          # 入场 D → D+1 / D+2 绝对收益


# ---------- 数据根 / 文件 I/O ----------
def _resolve_root(data_root: str | None) -> str:
    if data_root:
        return data_root
    try:
        from tools.analysis.market_forecast.dataroot import resolve_data_root
        return str(resolve_data_root(None))
    except Exception:  # noqa: BLE001
        return "data"


def _analysis_dir(date: str, data_root: str | None) -> str:
    return os.path.join(_resolve_root(data_root), "analysis", date)


def _load_json(p: str):
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ---------- 价量强 codes（哪些视图命中）----------
def strong_hits(date: str, data_root: str | None = None) -> dict[str, list[str]]:
    """{code: [命中视图...]}，来源 量价放量/最强选股 入选清单。缺文件 → 该视图跳过。"""
    d = _analysis_dir(date, data_root)
    out: dict[str, list[str]] = {}
    for view in STRONG_VIEWS:
        obj = _load_json(os.path.join(d, f"{view}.json"))
        if not isinstance(obj, dict):
            continue
        for item in obj.get("入选清单", []):
            code = item.get("code")
            if code:
                out.setdefault(code, [])
                if view not in out[code]:
                    out[code].append(view)
    return out


# ---------- veto / 超买命中（多策略命中闸门）----------
def veto_reasons(ticket: dict) -> list[str]:
    """从一张闸门票抽取 veto/超买规避原因（预注册口径）；无命中 → []。"""
    reasons: list[str] = []
    veto = ticket.get("veto") or {}
    if veto.get("应用") or veto.get("剔除") or veto.get("否决") or (veto.get("罚分") or 0) > 0:
        attrs = veto.get("归因") or []
        tag = "基本面veto"
        if veto.get("剔除") or veto.get("否决"):
            tag = "基本面veto(硬剔除)"
        reasons.append(f"{tag}[{'/'.join(map(str, attrs)) or '罚分'}·罚{veto.get('罚分')}]")
    heat = ticket.get("过热闸") or {}
    if heat.get("触发") or heat.get("沉底") or heat.get("剔除"):
        axes = [k for k, v in (heat.get("轴") or {}).items() if v]
        why = heat.get("原因") or []
        reasons.append(f"超买/过热规避[轴:{'/'.join(axes) or '?'}·{';'.join(map(str, why))[:60]}]")
    return reasons


def veto_tickets(date: str, data_root: str | None = None) -> dict[str, list[str]]:
    """{code: [veto原因...]}，来源 多策略命中闸门.json 的票。缺文件 → {}。"""
    obj = _load_json(os.path.join(_analysis_dir(date, data_root), "多策略命中闸门.json"))
    out: dict[str, list[str]] = {}
    if not isinstance(obj, dict):
        return out
    for t in obj.get("票", []):
        code = t.get("code")
        if not code:
            continue
        rs = veto_reasons(t)
        if rs:
            out[code] = rs
    return out


# ---------- 前向绝对收益（主档 K 线，canonical，无未来函数）----------
def _kline_index(code: str, cache: dict):
    """{code: (close: list[float]|None, date2idx)}，主档只读不触网。"""
    if code in cache:
        return cache[code]
    close, d2i = None, {}
    try:
        from tools.collectors import market
        df = market.load_kline(code).reset_index(drop=True)
        if "close" in df.columns and "date" in df.columns:
            close = df["close"].astype(float).tolist()
            import pandas as pd
            d2i = {str(x)[:10]: i for i, x in enumerate(
                pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist())}
    except Exception:  # noqa: BLE001 - 缺票/坏档:标签保守留 null
        close, d2i = None, {}
    cache[code] = (close, d2i)
    return cache[code]


def _labels_for(code: str, date: str, horizons, cache: dict) -> dict:
    """{close_0, r_1, r_2}；入场 D=date，r_N% =(close[idx+N]/close[idx]-1)*100；未到期 None。"""
    close, d2i = _kline_index(code, cache)
    idx = d2i.get(date)
    close_0 = (close[idx] if (close is not None and idx is not None
                              and 0 <= idx < len(close)) else None)
    lab = {"close_0": close_0}
    for N in horizons:
        r = None
        if close is not None and idx is not None and 0 <= idx and idx + N < len(close) \
                and close[idx] > 0:
            r = float(close[idx + N] / close[idx] - 1.0) * 100.0
        lab[f"r_{N}"] = r
    return lab


def _label_status(veto票: list[dict], horizons) -> str:
    cells = [v["labels"].get(f"r_{N}") for v in veto票 for N in horizons]
    if not cells:
        return "empty"
    filled = sum(1 for c in cells if c is not None)
    if filled == 0:
        return "pending"
    return "settled" if filled == len(cells) else "partial"


# ---------- 每日 shadow 记录器（non-gating advisory）----------
def run_daily_shadow(date: str, out_dir: str, data_root: str | None = None,
                     horizons=DEFAULT_HORIZONS) -> dict:
    """价量强 ∩ 被 veto/超买规避 的票 → advisory + 前向绝对收益标签（待回填）。"""
    os.makedirs(out_dir, exist_ok=True)
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(data_root)   # market.load_kline 需先设主档数据根
    except Exception:  # noqa: BLE001
        pass
    strong = strong_hits(date, data_root)
    vetos = veto_tickets(date, data_root)
    cross = sorted(set(strong) & set(vetos))
    cache: dict = {}
    veto票 = []
    for code in cross:
        veto票.append({
            "code": code,
            "veto原因": vetos[code],
            "价量命中": strong[code],
            "labels": _labels_for(code, date, horizons, cache),
        })
    advisory = {
        "date": date,
        "非validated": True,
        "non_gating": True,
        "horizons": list(horizons),
        "n_价量强": len(strong),
        "n_veto票": len(vetos),
        "n_价量强被veto": len(veto票),
        "veto票": veto票,
        "label_status": _label_status(veto票, horizons),
        "note": ("forward-shadow advisory；数据仅 6 天历史标签→**只能 forward 攒**；"
                 "未 validated 前不动 live veto、不进任何生产选股决策；样本 <120 只报 N；"
                 "veto 命中口径预注册·只读不改；收益=入场D→D+N 绝对收益%（未到期 null 待 --backfill 回填）"),
    }
    path = os.path.join(out_dir, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(advisory, f, ensure_ascii=False, indent=2)
    logger.info("veto_track %s：价量强 %d，veto票 %d，交集 %d",
                date, len(strong), len(vetos), len(veto票))
    return advisory


# ---------- 前向标签回填（遍历未结算 advisory，填已到期 r_N；幂等·append-only）----------
def backfill_labels(out_dir: str, horizons=DEFAULT_HORIZONS) -> dict:
    """对所有 label_status != settled 的 advisory 用主档 K 线回填**已到期** r_N（只填 None 的 cell）。"""
    if not os.path.isdir(out_dir):
        return {"n_filled": 0, "days_touched": 0}
    try:
        from tools.analysis.market_forecast.dataroot import ensure_data_root
        ensure_data_root(None)
    except Exception:  # noqa: BLE001
        pass
    cache: dict = {}
    n_filled = days_touched = 0
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".json"):
            continue
        p = os.path.join(out_dir, fn)
        adv = _load_json(p)
        if not adv or adv.get("label_status") == "settled":
            continue
        date = adv.get("date")
        hs = adv.get("horizons", list(horizons))
        changed = False
        for v in adv.get("veto票", []):
            lab = v.get("labels", {})
            close, d2i = _kline_index(v["code"], cache)
            idx = d2i.get(date)
            if lab.get("close_0") is None and close is not None and idx is not None \
                    and 0 <= idx < len(close):
                lab["close_0"] = close[idx]
                changed = True
            for N in hs:
                if lab.get(f"r_{N}") is not None:
                    continue
                if close is not None and idx is not None and idx + N < len(close) \
                        and close[idx] > 0:
                    lab[f"r_{N}"] = float(close[idx + N] / close[idx] - 1.0) * 100.0
                    n_filled += 1
                    changed = True
        if changed:
            adv["label_status"] = _label_status(adv.get("veto票", []), hs)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(adv, f, ensure_ascii=False, indent=2)
            days_touched += 1
    return {"n_filled": n_filled, "days_touched": days_touched}


# ---------- forward 汇总（veto 是净避损还是净漏利；样本 <120 只报 N）----------
def summarize(out_dir: str, horizons=DEFAULT_HORIZONS) -> dict:
    """跨已积累 advisory 算 veto 票的前向绝对收益均值/胜率（settled cell）。样本不足如实报 N。"""
    obs = {N: [] for N in horizons}
    n_days = n_ticket = 0
    if os.path.isdir(out_dir):
        for fn in sorted(os.listdir(out_dir)):
            if not fn.endswith(".json"):
                continue
            adv = _load_json(os.path.join(out_dir, fn))
            if not adv:
                continue
            n_days += 1
            for v in adv.get("veto票", []):
                n_ticket += 1
                for N in horizons:
                    r = v.get("labels", {}).get(f"r_{N}")
                    if r is not None:
                        obs[N].append(r)
    per_h = {}
    for N in horizons:
        xs = obs[N]
        mean = sum(xs) / len(xs) if xs else None
        per_h[f"r_{N}"] = {
            "n_settled": len(xs),
            "均值绝对收益%": round(mean, 3) if mean is not None else None,
            "为正占比": round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None,
            "读法": "均值>0 → veto 误杀(净漏利)；均值<0 → veto 净避损。样本<120 仅方向参考",
        }
    return {"n_days": n_days, "n_veto票累计": n_ticket, "horizons": list(horizons),
            "样本充足": n_ticket >= 120, "汇总": per_h}


# ---------- CLI（每日 shadow runner 入口，供 launchd 调度）----------
DEFAULT_OUT_DIR = "data/shadow_forward/veto_track"


def main(argv: list[str] | None = None) -> int:
    """每日 shadow runner：价量强∩veto票 advisory（non-gating）。
    退出码：0=成功/非交易日跳过/幂等跳过/backfill/summary；非0=当日无闸门或价量视图（数据缺）。"""
    import argparse
    from datetime import datetime

    from tools.collectors import calendar as cal

    ap = argparse.ArgumentParser(
        description="B 基本面 veto 误杀 forward-shadow runner（纯记录·non-gating）")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，缺省今天")
    ap.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="advisory 落盘目录")
    ap.add_argument("--data-root", default=None, help="缺省自动探测含 analysis/ 的主仓数据根")
    ap.add_argument("--force", action="store_true", help="覆盖当日已有 advisory")
    ap.add_argument("--backfill", action="store_true", help="回填历史 advisory 已到期前向收益后退出")
    ap.add_argument("--summary", action="store_true", help="打印 forward 汇总后退出")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.backfill:
        r = backfill_labels(args.out_dir)
        logger.info("backfill：填 %d cell，触及 %d 日", r["n_filled"], r["days_touched"])
        return 0
    if args.summary:
        print(json.dumps(summarize(args.out_dir), ensure_ascii=False, indent=2))
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    date = args.date or today

    if not cal.is_trading_day(date):
        logger.info("%s 非交易日，跳过 shadow", date)
        return 0

    out_path = os.path.join(args.out_dir, f"{date}.json")
    if os.path.exists(out_path) and not args.force:
        logger.info("%s advisory 已存在（幂等跳过，--force 覆盖）：%s", date, out_path)
        return 0

    strong = strong_hits(date, args.data_root)
    vetos = veto_tickets(date, args.data_root)
    if not strong and not vetos:
        logger.error("%s 无价量视图/闸门（数据缺），无法出 shadow", date)
        return 2

    adv = run_daily_shadow(date, args.out_dir, args.data_root)
    logger.info("shadow 落盘 %s：价量强被veto %d → %s", date, adv["n_价量强被veto"], out_path)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
