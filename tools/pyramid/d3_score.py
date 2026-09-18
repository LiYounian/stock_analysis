"""D3-1 · 金字塔闭环记分引擎（程序·可审计·为回测调权提供证据）。

机制见 docs/计划/2026-09-18_收盘runbook与D3记分设计.md：
  选股 md（含 PICKS_BUY/AVOID 标记 + 止损价）→ 读 D0..D+K 主档 K线 →
  两套命中口径并列产出：
    口径 A（绝对收益）：买入票 D+K 收益>0 命中；规避票 收益<0 命中。恒可算（只需个股 K线）。
    口径 B（跑赢基准·沪深300）：买入票 (收益−基准)>0 命中；规避票 (收益−基准)<0 命中。
      基准覆盖不到 D+K → 标「基准缺」不编（对齐"缺数据不编造"铁律）。
  止损击穿：持有窗口内最低价 ≤ md 止损价 = 纪律触发（有止损价才判）。

防未来：只读 ≤ score_asof（记分执行日）的 bar；D+K 未走完 → 标「窗口未满」不算。
纯函数 + CLI + 语义锁测试；口径与档位写死，改动须过测试。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from tools.pyramid._common import data_root, load_kline
from tools.pyramid.entry_rule import (
    DEFAULT_ENTRY_RULE as _DEFAULT_ENTRY_RULE,
    ENTRY_RULES as _ENTRY_RULES,
    match_entry as _match_entry,
    normalize_rule as _normalize_rule,
)

# 全A等权基准（沪深300 缺时回退·A13）；与 model_a α 基准同源，口径一致
_EW_REL = os.path.join("data", "analysis", "backtest", "finval", "market_ew.parquet")

# ── 选股 md 机器可读标记（三方文件首部）──
_BUY_RE = re.compile(r"<!--\s*PICKS_BUY:\s*([0-9,\s]*?)\s*-->")
_AVOID_RE = re.compile(r"<!--\s*PICKS_AVOID:\s*([0-9,\s]*?)\s*-->")
_CODE_RE = re.compile(r"\b(\d{6})\b")
_止损_RE = re.compile(r"止损\s*\**\s*(\d+(?:\.\d+)?)")

_WINDOWS_DEFAULT = (1, 3, 5)
_BENCH = "000300"  # 沪深300


# ── 解析选股 md ─────────────────────────────────────────
def _split_codes(s: Optional[str]) -> list:
    if not s:
        return []
    return [c for c in re.split(r"[,\s]+", s.strip()) if re.fullmatch(r"\d{6}", c)]


def parse_stops(text: str) -> dict:
    """按 ### 小节切分，取每小节标题里的代码 → 该节首个止损价。best-effort，缺则不收。"""
    stops = {}
    blocks = re.split(r"\n#{2,4}\s", text)
    for b in blocks:
        head = b.split("\n", 1)[0]
        m = _CODE_RE.search(head)
        if not m:
            continue
        code = m.group(1)
        sm = _止损_RE.search(b)
        if sm:
            stops[code] = float(sm.group(1))
    return stops


def parse_picks(md_path: str) -> dict:
    """选股 md → {buy, avoid, stops, source, as_of}。标记缺失返回空列表不报错。"""
    with open(md_path, "r", encoding="utf-8") as f:
        text = f.read()
    buy = _split_codes(_BUY_RE.search(text).group(1) if _BUY_RE.search(text) else "")
    avoid = _split_codes(_AVOID_RE.search(text).group(1) if _AVOID_RE.search(text) else "")
    fn = os.path.basename(md_path)
    as_of = (re.search(r"(\d{4}-\d{2}-\d{2})", fn) or [None, None])[1] if re.search(r"\d{4}-\d{2}-\d{2}", fn) else None
    src = re.sub(r"^\d{4}-\d{2}-\d{2}_金字塔_|\.md$", "", fn) or fn
    return {"buy": buy, "avoid": avoid, "stops": parse_stops(text),
            "source": src, "as_of": as_of, "md": fn}


# ── 收益计算（读 K线）───────────────────────────────────
def _pos_le(dates, d0: str) -> Optional[int]:
    """最后一个 ≤ d0 的行下标（d0 停牌/非交易日则取其前一交易日）。"""
    d0ts = str(d0)
    idx = None
    for i, d in enumerate(dates):
        if str(d)[:10] <= d0ts:
            idx = i
        else:
            break
    return idx


def forward_return(code: str, d0: str, k: int, score_asof: str,
                   root: Optional[str] = None,
                   entry_rule: str = _DEFAULT_ENTRY_RULE,
                   df=None) -> dict:
    """个股 D0→D+K 收益 + 窗口内最低点，按 `entry_rule`（A8）统一撮合口径。

    · entry_rule="close"（默认·横截面记分）：D0 收盘即入、恒成交，收益 = D+K收盘/D0收盘；
      窗口内最低点相对 D0 收盘 %。
    · entry_rule="limit"（回踩限价·= model_a 口径）：限价=D0 收盘，D+1 回踩才成交，
      成交价=min(限价,D+1开)；收益 = D+K收盘/成交价；未成交 → {filled:False, untriggered:True}。

    窗口未走完（D+K 超出 score_asof 数据）→ {insufficient:True}。数据缺 → {缺:True}。
    统一附带 filled / entry_price / entry_rule 供三处（entry_price/d3_score/model_a）对齐。
    df：可传入已按 ≤score_asof 截断的 K线（回测批量复用·省重复 IO）；None 则内部 load_kline。
    """
    entry_rule = _normalize_rule(entry_rule)
    if df is None:
        df = load_kline(code, score_asof, root=root)
    if df is None or "close" not in df.columns:
        return {"缺": True}
    dates = df["date"].tolist() if "date" in df.columns else list(df.index)
    p0 = _pos_le(dates, d0)
    if p0 is None:
        return {"缺": True}
    pk = p0 + k
    if pk >= len(df):
        return {"insufficient": True, "d0_close": float(df["close"].iloc[p0])}
    c0 = float(df["close"].iloc[p0])
    ck = float(df["close"].iloc[pk])

    # ── 撮合：确定成交价（限价口径撮合 D+1；收盘口径 D0 收盘即入）──
    limit = c0
    p1 = p0 + 1
    next_open = float(df["open"].iloc[p1]) if "open" in df.columns and p1 < len(df) else None
    next_low = float(df["low"].iloc[p1]) if "low" in df.columns and p1 < len(df) else None
    m = _match_entry(entry_rule, limit, next_open, next_low)
    if m["filled"] is False:
        # 高开未回踩、踏空（final）：不计收益，剔出分母
        return {"filled": False, "untriggered": True, "entry_rule": entry_rule,
                "d0_close": c0, "dK_date": str(dates[pk])[:10], "note": m["note"]}
    entry_price = m["entry_price"] if m["entry_price"] is not None else c0

    seg = df.iloc[p0 + 1: pk + 1]
    low = float(seg["low"].min()) if "low" in seg.columns and len(seg) else ck
    return {
        "filled": True, "entry_rule": entry_rule, "entry_price": round(entry_price, 3),
        "d0_close": c0, "dK_close": ck,
        "dK_date": str(dates[pk])[:10],
        "ret_pct": round((ck / entry_price - 1) * 100, 2),
        "low_pct": round((low / entry_price - 1) * 100, 2),
        "低点": low,
    }


def _load_bench_df(root: Optional[str]):
    """沪深300 指数 K线（按 root 读，不依赖 settings.PROJECT_ROOT）。

    指数存为 data/raw/<date>/index_kline/000300.parquet（每份都是全历史快照）；
    取最新日期分区的那份（含最长序列）。找不到 → None。
    """
    import glob
    import pandas as pd
    base = os.path.join(data_root(root), "data", "raw")
    hits = sorted(glob.glob(os.path.join(base, "*", "index_kline", f"{_BENCH}.parquet")))
    if not hits:
        return None
    try:
        return pd.read_parquet(hits[-1])  # 路径含日期，字典序=时间序，末个=最新快照
    except Exception:
        return None


def bench_return(d0: str, k: int, score_asof: str,
                 root: Optional[str] = None) -> Optional[float]:
    """沪深300 D0→D+K 收益 %。基准覆盖不到 → None（口径 B 标「基准缺」）。"""
    bdf = _load_bench_df(root)
    if bdf is None or "close" not in bdf.columns:
        return None
    dates = bdf["date"].tolist() if "date" in bdf.columns else list(bdf.index)
    # 防未来：基准也截 ≤ score_asof
    keep = [i for i, d in enumerate(dates) if str(d)[:10] <= str(score_asof)[:10]]
    if not keep:
        return None
    last = keep[-1]
    p0 = _pos_le(dates[: last + 1], d0)
    if p0 is None:
        return None
    pk = p0 + k
    if pk > last:
        return None
    c0 = float(bdf["close"].iloc[p0])
    ck = float(bdf["close"].iloc[pk])
    return round((ck / c0 - 1) * 100, 2)


def _load_ew_series(root: Optional[str]) -> Optional[dict]:
    """全A等权净值 {date: ew_index}（data/analysis/backtest/finval/market_ew.parquet）。

    = model_a / sector_news_forward 的 α 基准同源，保证 D3 与 model_a 基准口径一致。
    缺文件/坏档 → None（真「基准缺」，不编）。
    """
    import pandas as pd
    p = os.path.join(data_root(root), _EW_REL)
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "ew_index"])
        return {str(x)[:10]: float(v) for x, v in zip(df["date"], df["ew_index"])}
    except Exception:
        return None


def market_ew_return(d0: str, k: int, score_asof: str,
                     root: Optional[str] = None) -> Optional[float]:
    """全A等权 D0→D+K 收益 %（沪深300 缺时的回退基准·A13）。

    防未来：仅用 ≤ score_asof 的净值点。基准点缺（D0 或 D+K 不在序列）→ None。
    净值序列已是"每日再平衡"的等权指数，D0→D+K 直接取比值，与个股收益口径同。
    """
    ew = _load_ew_series(root)
    if not ew:
        return None
    sa = str(score_asof)[:10]
    dates = sorted(d for d in ew if d <= sa)
    if not dates:
        return None
    p0 = _pos_le(dates, d0)
    if p0 is None:
        return None
    pk = p0 + k
    if pk >= len(dates):
        return None
    base = ew.get(dates[p0])
    end = ew.get(dates[pk])
    if not base or not end:
        return None
    return round((end / base - 1) * 100, 2)


def resolve_bench(d0: str, k: int, score_asof: str,
                  root: Optional[str] = None) -> dict:
    """基准解析（A13）：沪深300 优先，覆盖不到则回退全A等权 market_ew。

    返回 {value, source}；source ∈ {"沪深300","market_ew",None}。两者都取不到 → value=None
    （真「基准缺」，对齐"缺数据不编造"铁律）。**不折进 bench_return**：后者保持 000300 纯口径，
    其"末日超窗返回 None"语义仍被 test_bench_覆盖与缺 锁死。
    """
    b = bench_return(d0, k, score_asof, root=root)
    if b is not None:
        return {"value": b, "source": "沪深300"}
    ew = market_ew_return(d0, k, score_asof, root=root)
    if ew is not None:
        return {"value": ew, "source": "market_ew"}
    return {"value": None, "source": None}


# ── 单票裁决 + 单方汇总 ─────────────────────────────────
def judge(side: str, ret_pct: float, bench: Optional[float]) -> dict:
    """一票在一个窗口的命中判定。side ∈ {买入, 规避}。

    口径 A：买入 ret>0 命中；规避 ret<0 命中。
    口径 B：超额=ret−bench，买入 超额>0 命中；规避 超额<0 命中；bench=None → 口径B=None。
    """
    hit_a = (ret_pct > 0) if side == "买入" else (ret_pct < 0)
    if bench is None:
        return {"命中A": hit_a, "超额": None, "命中B": None}
    excess = round(ret_pct - bench, 2)
    hit_b = (excess > 0) if side == "买入" else (excess < 0)
    return {"命中A": hit_a, "超额": excess, "命中B": hit_b}


@dataclass
class 票记分:
    code: str
    side: str
    windows: dict = field(default_factory=dict)  # k -> {ret,bench,超额,命中A,命中B,击穿}
    缺: bool = False


def score_source(picks: dict, windows=_WINDOWS_DEFAULT,
                 score_asof: Optional[str] = None, root: Optional[str] = None,
                 entry_rule: str = _DEFAULT_ENTRY_RULE) -> dict:
    """对一方（三方之一或某召回模式）的 buy/avoid 全票逐窗记分 + 命中率/均收益汇总。

    entry_rule（A8）统一撮合口径；"limit" 口径下高开未回踩的票记「踏空」剔出分母。
    基准（A13）经 resolve_bench：沪深300 优先、缺则回退全A等权 market_ew。
    """
    entry_rule = _normalize_rule(entry_rule)
    d0 = picks["as_of"]
    sa = score_asof or d0
    rows: list = []
    for side, codes in (("买入", picks["buy"]), ("规避", picks["avoid"])):
        stop = picks.get("stops", {})
        for c in codes:
            r = 票记分(code=c, side=side)
            for k in windows:
                fr = forward_return(c, d0, k, sa, root=root, entry_rule=entry_rule)
                if fr.get("缺"):
                    r.缺 = True
                    continue
                if fr.get("insufficient"):
                    r.windows[k] = {"insufficient": True}
                    continue
                if fr.get("filled") is False:      # 踏空（limit 口径高开未回踩）→ 剔出分母
                    r.windows[k] = {"untriggered": True, "dK": fr.get("dK_date")}
                    continue
                br = resolve_bench(d0, k, sa, root=root)
                bench = br["value"]
                j = judge(side, fr["ret_pct"], bench)
                击穿 = (c in stop and fr["低点"] is not None and fr["低点"] <= stop[c])
                r.windows[k] = {"ret": fr["ret_pct"], "bench": bench,
                                "基准源": br["source"], **j,
                                "击穿止损": 击穿, "dK": fr["dK_date"]}
            rows.append(r)

    # 汇总：每窗、每口径的命中率 + 均收益（买入看收益，规避看"避对率"）
    summary = {}
    for k in windows:
        vals = [r.windows[k] for r in rows if k in r.windows and "ret" in r.windows[k]]
        if not vals:
            summary[k] = {"n": 0}
            continue
        n = len(vals)
        hitA = sum(1 for v in vals if v["命中A"]) / n
        bvals = [v for v in vals if v["命中B"] is not None]
        hitB = (sum(1 for v in bvals if v["命中B"]) / len(bvals)) if bvals else None
        # 基准源统计（沪深300 / market_ew 各占几票）
        src_cnt: dict = {}
        for v in bvals:
            s = v.get("基准源")
            if s:
                src_cnt[s] = src_cnt.get(s, 0) + 1
        summary[k] = {
            "n": n,
            "命中率A": round(hitA, 3),
            "命中率B": (round(hitB, 3) if hitB is not None else None),
            "均收益": round(sum(v["ret"] for v in vals) / n, 2),
            "基准覆盖": len(bvals),
            "基准源": src_cnt,
        }
    return {"source": picks["source"], "as_of": d0, "score_asof": sa,
            "entry_rule": entry_rule, "rows": rows, "summary": summary}


# ── 决策日全量记分卡（三方 + 可扩四模式）────────────────
def _find_pick_files(as_of: str, root: Optional[str]) -> list:
    d = os.path.join(data_root(root), "docs", "每日分析", "选股")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(f"{as_of}_金字塔_") and f.endswith(".md"))


def build_scorecard(as_of: str, windows=_WINDOWS_DEFAULT,
                    score_asof: Optional[str] = None, root: Optional[str] = None,
                    entry_rule: str = _DEFAULT_ENTRY_RULE) -> dict:
    """决策日 as_of 的三方选股 → 逐方记分。score_asof=None 时用"今天可得的最新"（=as_of 兜底）。"""
    files = _find_pick_files(as_of, root)
    sa = score_asof or as_of
    er = _normalize_rule(entry_rule)
    parts = [score_source(parse_picks(f), windows, sa, root, entry_rule=er) for f in files]
    return {"as_of": as_of, "score_asof": sa, "windows": list(windows),
            "entry_rule": er, "sources": parts}


def _fmt_bench_src(src_cnt: dict) -> str:
    """基准源统计 → 简报串（如 "沪深300×4·market_ew×2"）。空 → ""。"""
    if not src_cnt:
        return ""
    return "·".join(f"{k}×{v}" for k, v in src_cnt.items())


def render_scorecard(sc: dict) -> str:
    er = sc.get("entry_rule", _DEFAULT_ENTRY_RULE)
    er_desc = "收盘即入·恒成交" if er == "close" else "回踩限价·含踏空"
    L = [f"# 金字塔 D3 记分卡 · 选股日={sc['as_of']} · 记分执行={sc['score_asof']}",
         f"窗口=D+{list(sc['windows'])}　口径A=绝对收益　口径B=跑赢基准(沪深300→缺退market_ew)",
         f"入场口径 entry_rule={er}（{er_desc}）",
         ""]
    for p in sc["sources"]:
        L.append(f"## {p['source']}")
        for k in sc["windows"]:
            s = p["summary"].get(k, {})
            if not s.get("n"):
                L.append(f"- D+{k}: 无可记分票（窗口未满/数据缺/踏空）")
                continue
            if s["命中率B"] is not None:
                src = _fmt_bench_src(s.get("基准源", {}))
                b = f"命中率B={s['命中率B']}" + (f"[{src}]" if src else "")
            else:
                b = f"命中率B=基准缺({s['基准覆盖']}/{s['n']})"
            L.append(f"- D+{k}: n={s['n']} 命中率A={s['命中率A']} {b} 均收益={s['均收益']}%")
        for r in p["rows"]:
            if r.缺:
                L.append(f"  · {r.code}({r.side}): 数据缺")
                continue
            segs = []
            for k in sc["windows"]:
                w = r.windows.get(k, {})
                if w.get("insufficient"):
                    segs.append(f"D+{k}:未满")
                elif w.get("untriggered"):
                    segs.append(f"D+{k}:踏空")
                elif "ret" in w:
                    ex = f"/超额{w['超额']}" if w["超额"] is not None else "/基准缺"
                    ko = "·击穿" if w.get("击穿止损") else ""
                    segs.append(f"D+{k}:{w['ret']}%{ex}{ko}")
            L.append(f"  · {r.code}({r.side}): " + "　".join(segs))
        L.append("")
    return "\n".join(L)


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="金字塔 D3 闭环记分")
    ap.add_argument("--as-of", required=True, help="选股日 D0")
    ap.add_argument("--score-asof", default=None, help="记分执行日（数据现有到哪天）；缺省=as-of")
    ap.add_argument("--windows", default="1,3,5", help="forward 窗口，逗号分隔")
    ap.add_argument("--entry-rule", default=_DEFAULT_ENTRY_RULE,
                    choices=list(_ENTRY_RULES), help="入场撮合口径（A8）")
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    ws = tuple(int(x) for x in a.windows.split(",") if x.strip())
    sc = build_scorecard(a.as_of, ws, a.score_asof, a.data_root, entry_rule=a.entry_rule)
    print(render_scorecard(sc))


if __name__ == "__main__":
    _cli()
