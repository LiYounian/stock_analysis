"""S01 诊断专用一次性脚本:隔离「入场方向 edge」与「买法/持有期」两个自由度。

复用 screen_s01.signal_at 的纯 as-of 信号(与 v3 回放同一信号集),在同一回放抽样
宇宙上,对每个信号 (code, T) 计算多种**买法** × 多个 **horizon** 的前瞻超额收益,
以判定 S01 的负 edge 到底来自「入场方向本身」还是「可修的买法/离场错配」。

买法(entry basis,均只用 ≤T+1 的价,防未来函数):
  · close_T      : 信号日收盘买(旧口径)
  · open_T1      : 次日开盘买(v3 口径)
  · close_T1     : 次日收盘买
  · conf_T1nobrk : 次日不破 T 最低价才在次日收盘买(入场确认;破低则放弃该信号)

horizon h ∈ {1,2,3,5,10,20}:退出点 = close[T+h]。
超额 = 该笔 [entry_ref_date, T+h] 收益 − 同期宇宙等权收益(全市场代理,与 v3 一致)。
显著性:按**信号日聚类**(独立单元=交易日)做 cluster-bootstrap 双尾 p(自包含,无 scipy)。

只读、不落库、不改任何策略/参数。纯诊断产物,打印到 stdout。
"""
from __future__ import annotations

import json
import random
import statistics
from collections import defaultdict

import numpy as np

from tools.backtest.eval_v3 import replay_source as rs
from tools.collectors import market
from tools.pipeline import screen_s01

HORIZONS = (1, 2, 3, 5, 10, 20)
UNIVERSE_N = 800
SEED = 20260828


def load_universe():
    codes = rs.sample_universe(UNIVERSE_N, SEED)
    book = {}
    for c in codes:
        try:
            df = market.load_kline(c).reset_index(drop=True)
        except Exception:
            continue
        if "date" not in df.columns or len(df) < 60:
            continue
        book[c] = {
            "date": [str(x)[:10] for x in df["date"].tolist()],
            "open": df["open"].to_numpy(float) if "open" in df.columns else df["close"].to_numpy(float),
            "high": df["high"].to_numpy(float),
            "low": df["low"].to_numpy(float),
            "close": df["close"].to_numpy(float),
        }
    return book


def build_universe_returns(book):
    """每 (交易日 d, horizon h) 的宇宙等权收益 = mean over codes of close[i+h]/close[i]-1。
    入场基准锚在 d 的收盘(close_T 口径对齐);作为超额基准的市场代理。"""
    # 只算实际需要的天跨度:close_T 用 h;T+1 买法基准同期 = h-1。
    spans = sorted({h for h in HORIZONS} | {h - 1 for h in HORIZONS if h - 1 >= 1})
    ret = {h: defaultdict(list) for h in spans}
    for c, b in book.items():
        close = b["close"]
        n = len(close)
        for i in range(n):
            for h in spans:
                if i + h < n and close[i] > 0:
                    ret[h][b["date"][i]].append(close[i + h] / close[i] - 1.0)
    uni = {h: {d: (sum(v) / len(v)) for d, v in dd.items() if v} for h, dd in ret.items()}
    return uni


def collect_signals(book):
    """全史逐票扫 S01 as-of 信号,返回 [(code, T_idx, T_date)]。"""
    sigs = []
    need = screen_s01.min_history()
    for c, b in book.items():
        # 需重建 df 供 signal_at(它要 DataFrame)
        import pandas as pd
        df = pd.DataFrame({"open": b["open"], "high": b["high"],
                           "low": b["low"], "close": b["close"], "date": b["date"]})
        n = len(df)
        for t in range(need - 1, n):
            if screen_s01.signal_at(df, t).get("SELECT"):
                sigs.append((c, t, b["date"][t]))
    return sigs


def entry_ref(book, code, t, basis):
    """返回 (entry_price, entry_ref_idx_for_benchmark, ok)。entry_ref_idx=基准同期起点索引。
    close_T 用 T;其余用 T+1(基准同期也从 T+1 起,保证收益/基准同期口径)。"""
    b = book[code]
    close, open_, low = b["close"], b["open"], b["low"]
    n = len(close)
    if basis == "close_T":
        return float(close[t]), t, True
    if t + 1 >= n:
        return None, None, False
    if basis == "open_T1":
        px = float(open_[t + 1])
        return (px, t + 1, True) if px > 0 else (float(close[t + 1]), t + 1, True)
    if basis == "close_T1":
        return float(close[t + 1]), t + 1, True
    if basis == "conf_T1nobrk":
        if low[t + 1] < low[t]:
            return None, None, False       # 破低 → 放弃
        return float(close[t + 1]), t + 1, True
    raise ValueError(basis)


def run():
    book = load_universe()
    uni = build_universe_returns(book)
    sigs = collect_signals(book)
    print(f"宇宙有效票={len(book)} 信号数(全史,含各买法未过滤前)={len(sigs)}")

    bases = ["close_T", "open_T1", "close_T1", "conf_T1nobrk"]
    # 结果: basis -> h -> list of (excess, sign_day)  以及 raw
    out = {b: {h: {"raw": [], "exc": [], "by_day": defaultdict(list)} for h in HORIZONS}
           for b in bases}

    for code, t, tdate in sigs:
        b = book[code]
        close = b["close"]
        n = len(close)
        for basis in bases:
            ep, ref_idx, ok = entry_ref(book, code, t, basis)
            if not ok or ep is None or ep <= 0:
                continue
            ref_date = b["date"][ref_idx]
            for h in HORIZONS:
                exit_idx = ref_idx + (h if basis == "close_T" else h - 1) if False else t + h
                # 退出锚 T+h(与 v3 一致:退出点 close[T+h]);基准同期 = 从 ref_date 到 T+h
                if t + h >= n:
                    continue
                raw = close[t + h] / ep - 1.0
                # benchmark: 宇宙在 [ref_date, T+h] 的等权收益。用 horizon 天数 = (T+h)-ref_idx
                bh = (t + h) - ref_idx
                ub = uni.get(bh, {}).get(ref_date)
                if ub is None:
                    continue
                exc = raw - ub
                out[basis][h]["raw"].append(raw)
                out[basis][h]["exc"].append(exc)
                out[basis][h]["by_day"][tdate].append(exc)

    # cluster bootstrap p (双尾, H0 mean excess=0), 按信号日聚类
    def cluster_p(by_day, iters=2000):
        days = list(by_day.keys())
        if len(days) < 3:
            return None, None
        day_means = {d: statistics.mean(v) for d, v in by_day.items()}
        obs = statistics.mean([m for m in day_means.values()])
        # 中心化后重抽 day-level
        vals = list(day_means.values())
        center = statistics.mean(vals)
        centered = [v - center for v in vals]
        rng = random.Random(42)
        cnt = 0
        for _ in range(iters):
            samp = [centered[rng.randrange(len(centered))] for _ in centered]
            if abs(statistics.mean(samp)) >= abs(obs):
                cnt += 1
        return obs, (cnt + 1) / (iters + 1)

    print("\n买法 × horizon:平均超额%(vs宇宙等权)| 中位raw% | 胜率% | N | 聚类p")
    print("=" * 92)
    for basis in bases:
        print(f"\n--- 买法 = {basis} ---")
        for h in HORIZONS:
            d = out[basis][h]
            exc = d["exc"]
            raw = d["raw"]
            if not exc:
                continue
            obs, p = cluster_p(d["by_day"])
            mean_exc = statistics.mean(exc) * 100
            med_raw = statistics.median(raw) * 100
            win = sum(1 for r in raw if r > 0) / len(raw) * 100
            pstr = f"{p:.4f}" if p is not None else "NA"
            print(f"  h={h:2d} | 超额 {mean_exc:+6.2f} | 中位raw {med_raw:+6.2f} | "
                  f"胜率 {win:4.1f} | N={len(exc):5d} | 聚类p={pstr}")


if __name__ == "__main__":
    run()
