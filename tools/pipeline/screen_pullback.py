"""回踩低吸 · 统一入场框架 Screener(两段式:突破→观察池→回踩缩量进场)。

需求源自 docs/计划/回踩低吸统一入场框架_设计与预注册.md。提供者定调:
**突破只加观察不操作;等回踩支撑 + 同时缩量 再进场、设止损。**

规格(前复权 OHLCV,当日 = t;整数索引 t 指 kdf 第 t 根):
  Stage1 突破→观察池:`pattern.detect_box_strict`(严格横盘矩形放量突破,不含末根)命中 →
    记 {突破日 b, 支撑=箱顶(突破位), 观察窗 M}。突破本身**不进场**(只加观察)。
  Stage2 回踩缩量→进场:观察窗 [b+1, b+M] 内,首个当日 t 同时满足:
    ① 回踩到支撑:收盘贴近 {突破位/箱顶, MA10, MA20} 任一(≤支撑容差%)且未有效跌破(≥支撑×(1−有效跌破%));
    ② 缩量:V ≤ MA(V,缩量均窗)×缩量系数(复用 S02 地量口径);
    ③(可选)趋势未破:收盘未有效跌破 MA(趋势门均线)。
  → 该 t 为**进场信号日**,进场 t+1(次日开盘);止损=支撑×(1−止损buffer%)。

复用:Stage1 = `pattern.detect_box_strict`(收紧的严格箱体);
      Stage2 = 泛化 S02 缩量回踩算子(`pullback_shrink_at` 对任意观察池票判回踩+缩量,
               不绑 S02 自身 C1–C5)。参数全读 `THRESHOLDS["回踩低吸"]`,不散写硬编码。

防未来函数红线:Stage1 突破按 b 截 [0,b] 识别(箱体不含末根);Stage2 回踩/缩量按 t 只用
[0,t];进场 t+1。历史不足不选。数据只读复用 `collectors.market.load_kline`。
入口:`python -m tools.pipeline.screen_pullback [--codes ...|--universe N] [--date D] [--no-fetch]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.pattern_screener.pattern import detect_box_strict
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_pullback")

_CFG = THRESHOLDS["回踩低吸"]


def _ma(arr, t: int, period: int) -> float:
    """arr 第 t 根的 period 日简单均线(用 t 及之前的 period 根)。不足 → NaN。"""
    if t - period + 1 < 0:
        return float("nan")
    seg = arr[t - period + 1: t + 1]
    return float(sum(seg) / len(seg))


def min_history(cfg: dict | None = None) -> int:
    """框架所需最少日线根数(不足不选)= 严格箱体窗上界 + 1(突破末根)。"""
    c = cfg or _CFG
    return int(c["严格箱体"]["窗口区间"][1]) + 1


# ———————————————————— Stage 2 · 回踩缩量算子(泛化 S02)————————————————————
def pullback_shrink_at(kdf: pd.DataFrame, t: int, support: float,
                       cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否为「回踩支撑 + 缩量(+趋势未破)」进场信号。

    对**任意观察池票**判定(不绑 S02 的 C1–C5):
      ① 回踩到位:收盘贴近 {突破位/箱顶 support, MA10, MA20} 任一(≤支撑容差%)且未有效跌破;
      ② 缩量:V ≤ MA(V,缩量均窗)×缩量系数;
      ③(可选)趋势未破:收盘 ≥ MA(趋势门均线)×(1−有效跌破%)。
    只用 t 及之前(防未来函数)。返回 {命中, 支撑, 明细}。
    """
    c = (cfg or _CFG)["回踩"]
    n = len(kdf)
    if t < 1 or t >= n:
        return {"命中": False, "原因": "索引越界"}
    close = kdf["close"].to_numpy(float)
    vol = kdf["volume"].to_numpy(float)
    px = float(close[t])
    tol = float(c["支撑容差%"]) / 100.0
    brk = float(c["有效跌破%"]) / 100.0

    # ① 回踩到位 + 未有效跌破(对任一支撑:突破位/箱顶 + 动态均线)
    supports: dict[str, float] = {"突破位/箱顶": float(support)}
    for w in c["支撑均线"]:
        ma = _ma(close, t, int(w))
        if ma == ma:
            supports[f"MA{int(w)}"] = ma
    hit = None
    for name, s in supports.items():
        if s <= 0:
            continue
        near = abs(px - s) / s <= tol
        not_broken = px >= s * (1 - brk)
        if near and not_broken:
            hit = (name, round(s, 4))
            break
    c1 = hit is not None

    # ② 缩量(复用 S02 地量口径)
    vw = int(c["缩量均窗"])
    ma_v = _ma(vol, t, vw)
    c2 = (ma_v == ma_v) and (vol[t] <= ma_v * float(c["缩量系数"]))

    # ③ 趋势未破(可选)
    tg = c.get("趋势门", {})
    c3 = True
    ma_tr = None
    if tg.get("启用"):
        w = int(tg.get("MA不破均线", 20))
        ma_tr = _ma(close, t, w)
        c3 = (ma_tr == ma_tr) and (px >= ma_tr * (1 - brk))

    ok = bool(c1 and c2 and c3)
    return {
        "命中": ok, "支撑": hit,
        "回踩到位": bool(c1), "缩量": bool(c2), "趋势未破": bool(c3),
        "明细": {
            "close": round(px, 4),
            "缩量均量": (round(ma_v, 2) if ma_v == ma_v else None),
            "V": round(float(vol[t]), 2),
            "趋势MA": (round(ma_tr, 4) if (ma_tr is not None and ma_tr == ma_tr) else None),
            "候选支撑": {k: round(v, 4) for k, v in supports.items()},
        },
    }


# ———————————————————— Stage 1 · 突破观察池 ————————————————————
def find_breakouts(kdf: pd.DataFrame, cfg: dict | None = None) -> list[tuple[int, float]]:
    """扫全历史,返回所有严格箱体放量突破 (突破日索引 b, 支撑=箱顶) 升序列表。"""
    c = cfg or _CFG
    sc = c["严格箱体"]
    n = len(kdf)
    start = max(min_history(c) - 1, 1)
    out: list[tuple[int, float]] = []
    for b in range(start, n):
        r = detect_box_strict(kdf.iloc[: b + 1], sc)
        if r.get("达标"):
            out.append((b, float(r["特征"]["箱顶"])))
    return out


def find_signals_pullback(kdf: pd.DataFrame, cfg: dict | None = None) -> list[dict]:
    """两段式全流程:每个突破在观察窗 [b+1, b+M] 内取**首个**回踩缩量日 t 作进场信号。

    返回 [{"t": 进场信号索引, "突破日": b, "支撑": 箱顶}, ...](升序,一个突破最多一笔)。
    进场信号日 t 之后进场 t+1(由回测器 _entry 落地)。防未来函数:突破按 b、回踩按 t 只用 ≤ 各自当日。
    """
    c = cfg or _CFG
    M = int(c["观察窗M"])
    n = len(kdf)
    signals: list[dict] = []
    for b, support in find_breakouts(kdf, c):
        for t in range(b + 1, min(b + 1 + M, n)):
            if pullback_shrink_at(kdf, t, support, c).get("命中"):
                signals.append({"t": t, "突破日": b, "支撑": support})
                break                                   # 一个突破只取首个回踩
    return signals


# ———————————————————— 当日盘后逐票(观察池 + 当日是否回踩)————————————————————
def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判**最后一根**是否为进场信号:近 M 日内有严格箱体突破(观察池存活)且当日回踩缩量。

    返回 {SELECT, 突破日, 支撑, 止损, 明细}。历史不足 → SELECT=False。
    """
    c = cfg or _CFG
    n = len(kdf)
    need = min_history(c)
    if n < need:
        return {"SELECT": False, "原因": f"历史不足({n}<{need})"}
    t = n - 1
    M = int(c["观察窗M"])
    # 观察池:在 [t-M, t-1] 里找最近一次突破(其支撑用于当日回踩判定)
    for b in range(t - 1, max(t - 1 - M, 0) - 1, -1):
        r = detect_box_strict(kdf.iloc[: b + 1], c["严格箱体"])
        if not r.get("达标"):
            continue
        support = float(r["特征"]["箱顶"])
        pr = pullback_shrink_at(kdf, t, support, c)
        if pr.get("命中"):
            buf = float(c["止损buffer%"]) / 100.0
            hit_s = pr["支撑"][1] if pr.get("支撑") else support
            return {"SELECT": True, "突破日": str(kdf["date"].iloc[b])[:10],
                    "支撑": support, "命中支撑": pr.get("支撑"),
                    "止损价": round(hit_s * (1 - buf), 4), "明细": pr["明细"]}
        break                                            # 只看最近一次突破对应的回踩
    return {"SELECT": False, "原因": "观察窗内无突破或当日未回踩缩量"}


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_pullback_screen(codes: list[str], as_of: str | None = None,
                        fetch: bool = True) -> dict:
    """扫描 codes,对每票判最后一根是否为回踩缩量进场信号,落 view「回踩低吸」。返回 summary。"""
    if as_of:
        store.set_active_date(as_of)
    need = min_history()
    selected: list[dict] = []
    scanned = skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = screen_latest(kdf)
        if r.get("SELECT"):
            selected.append({"code": code, "突破日": r["突破日"], "支撑": r["支撑"],
                             "止损价": r["止损价"], "明细": r["明细"]})

    sc = _CFG["严格箱体"]
    pb = _CFG["回踩"]
    view = {
        "as_of": as_of,
        "策略": "回踩低吸(统一入场框架)",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected), "入选清单": selected,
        "规则": (f"Stage1 严格箱体放量突破(振幅∈[{sc['振幅下界%']},{sc['振幅上界%']}]%,触碰≥{sc['触碰次数']}"
                 f"/横盘/缩量三硬门,放量×{sc['突破放量倍数']})→ 观察池(窗 {_CFG['观察窗M']} 日);"
                 f"Stage2 回踩支撑(≤{pb['支撑容差%']}% 且未有效跌破 {pb['有效跌破%']}%)+ 缩量"
                 f"(V≤MA(V,{pb['缩量均窗']})×{pb['缩量系数']})→ 进场 t+1"),
        "防未来函数": f"突破按 b、回踩按 t 只用 ≤ 各自当日;进场 t+1;历史<{need} 不选",
    }
    p = store.put_view("回踩低吸", view)
    logger.info("回踩低吸:扫描 %d / 有效 %d / 跳过 %d / 入选 %d → %s",
                len(codes), scanned, skipped, len(selected), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="回踩低吸 统一入场框架 全A扫描")
    ap.add_argument("--codes", help="逗号分隔代码")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只")
    ap.add_argument("--date", help="as_of 日期(YYYY-MM-DD)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不采集")
    a = ap.parse_args(argv)
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.universe:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    else:
        codes = store.list_master_codes()
    run_pullback_screen(codes, as_of=a.date, fetch=not a.no_fetch)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
