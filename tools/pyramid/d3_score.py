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
                   root: Optional[str] = None) -> dict:
    """个股 D0→D+K 收益 + 窗口内最低点（相对 D0 收盘 %）。

    窗口未走完（D+K 超出 score_asof 数据）→ {insufficient:True}。数据缺 → {缺:True}。
    """
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
    seg = df.iloc[p0 + 1: pk + 1]
    low = float(seg["low"].min()) if "low" in seg.columns and len(seg) else ck
    return {
        "d0_close": c0, "dK_close": ck,
        "dK_date": str(dates[pk])[:10],
        "ret_pct": round((ck / c0 - 1) * 100, 2),
        "low_pct": round((low / c0 - 1) * 100, 2),
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
                 score_asof: Optional[str] = None, root: Optional[str] = None) -> dict:
    """对一方（三方之一或某召回模式）的 buy/avoid 全票逐窗记分 + 命中率/均收益汇总。"""
    d0 = picks["as_of"]
    sa = score_asof or d0
    rows: list = []
    for side, codes in (("买入", picks["buy"]), ("规避", picks["avoid"])):
        stop = picks.get("stops", {})
        for c in codes:
            r = 票记分(code=c, side=side)
            for k in windows:
                fr = forward_return(c, d0, k, sa, root=root)
                if fr.get("缺"):
                    r.缺 = True
                    continue
                if fr.get("insufficient"):
                    r.windows[k] = {"insufficient": True}
                    continue
                bench = bench_return(d0, k, sa, root=root)
                j = judge(side, fr["ret_pct"], bench)
                击穿 = (c in stop and fr["低点"] is not None and fr["低点"] <= stop[c])
                r.windows[k] = {"ret": fr["ret_pct"], "bench": bench, **j,
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
        summary[k] = {
            "n": n,
            "命中率A": round(hitA, 3),
            "命中率B": (round(hitB, 3) if hitB is not None else None),
            "均收益": round(sum(v["ret"] for v in vals) / n, 2),
            "基准覆盖": len(bvals),
        }
    return {"source": picks["source"], "as_of": d0, "score_asof": sa,
            "rows": rows, "summary": summary}


# ── 决策日全量记分卡（三方 + 可扩四模式）────────────────
def _find_pick_files(as_of: str, root: Optional[str]) -> list:
    d = os.path.join(data_root(root), "docs", "每日分析", "选股")
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if f.startswith(f"{as_of}_金字塔_") and f.endswith(".md"))


def build_scorecard(as_of: str, windows=_WINDOWS_DEFAULT,
                    score_asof: Optional[str] = None, root: Optional[str] = None) -> dict:
    """决策日 as_of 的三方选股 → 逐方记分。score_asof=None 时用"今天可得的最新"（=as_of 兜底）。"""
    files = _find_pick_files(as_of, root)
    sa = score_asof or as_of
    parts = [score_source(parse_picks(f), windows, sa, root) for f in files]
    return {"as_of": as_of, "score_asof": sa, "windows": list(windows), "sources": parts}


def render_scorecard(sc: dict) -> str:
    L = [f"# 金字塔 D3 记分卡 · 选股日={sc['as_of']} · 记分执行={sc['score_asof']}",
         f"窗口=D+{list(sc['windows'])}　口径A=绝对收益　口径B=跑赢沪深300",
         ""]
    for p in sc["sources"]:
        L.append(f"## {p['source']}")
        for k in sc["windows"]:
            s = p["summary"].get(k, {})
            if not s.get("n"):
                L.append(f"- D+{k}: 无可记分票（窗口未满/数据缺）")
                continue
            b = f"命中率B={s['命中率B']}" if s["命中率B"] is not None else f"命中率B=基准缺({s['基准覆盖']}/{s['n']})"
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
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    ws = tuple(int(x) for x in a.windows.split(",") if x.strip())
    sc = build_scorecard(a.as_of, ws, a.score_asof, a.data_root)
    print(render_scorecard(sc))


if __name__ == "__main__":
    _cli()
