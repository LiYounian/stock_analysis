"""策略 S02「放量后缩量回踩」入场 Screener(规则型布尔组合)。

与 S01 同为**独立规则型 screener**(不注册合议专家、不复用四形态):当日盘后逐票,
五条硬规则 C1..C5 全满足即入选(SELECT = C1 AND C2 AND C3 AND C4 AND C5,单日触发)。

规格(均前复权 OHLCV,当日 = t;整数索引 t 指 kdf 第 t 根):
  C1 周线近期放量:REF(周量,1) > MA(周量,8)×倍数  OR  REF(周量,2) > MA(周量,8)×倍数
  C2 短均线多头:MA5 > MA10(短均线可配)
  C3 当日缩量回踩(价):C<O 且 C<REF(C,1)(收阴且收跌,无跌幅%门槛)
  C4 接近地量(量):V ≤ MA(V,10)×地量系数
  C5 贴 10 日线:ABS(C−MA10)/MA10 ≤ 贴线容差

日→周聚合(关键):周量 = 日成交量按 **ISO 自然周(周一起)聚合求和**;
  **当周在途不计**——第 t 根所在自然周整周剔除,只用已收盘的完整周;
  REF(周量,1) = 上一完整周、REF(周量,2) = 上上完整周;MA(周量,8) = 近 8 个完整周均量。

防未来函数红线:所有量只用 t 及之前;当周整周剔除(不引入 t 之后的交易日);
历史需 ≥ 周量均窗 个完整周(约 40+ 交易日)且 ≥ 最少历史根数 日线,不足不选。

参数全读 `THRESHOLDS["放量后缩量回踩"]`,不散写硬编码。
数据只读复用 `collectors.market.load_kline`(优先滚动主档、回退 raw)。
入口:`python -m tools.pipeline.screen_s02 [--codes ...|--universe N] [--date D] [--no-fetch]`。
"""
from __future__ import annotations

import logging
from collections import OrderedDict

import pandas as pd

from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_s02")

_CFG = THRESHOLDS["放量后缩量回踩"]


def _ma(arr, t: int, period: int) -> float:
    """arr 第 t 根的 period 日简单均线(用 t 及之前的 period 根)。不足 → NaN。"""
    if t - period + 1 < 0:
        return float("nan")
    seg = arr[t - period + 1: t + 1]
    return float(sum(seg) / len(seg))


def weekly_volumes(kdf: pd.DataFrame, t: int) -> list[float]:
    """截至第 t 根(含)、按 ISO 自然周聚合的**完整周**成交量(时间升序)。

    已剔除第 t 根所在的在途周(当周在途不计)。日期升序 → 分组保序即时间序。
    只用 [0, t] 的数据,绝不引入 t 之后的交易日(防未来函数)。
    """
    if t < 0:
        return []
    dates = pd.to_datetime(kdf["date"].iloc[: t + 1])
    vols = kdf["volume"].iloc[: t + 1].to_numpy(dtype=float)
    iso = dates.dt.isocalendar()                       # year / week / day
    years = iso["year"].tolist()
    weeks = iso["week"].tolist()
    keys = list(zip(years, weeks))
    sums: "OrderedDict[tuple, float]" = OrderedDict()
    for k, v in zip(keys, vols):
        sums[k] = sums.get(k, 0.0) + float(v)
    cur_key = keys[-1]                                 # t 所在自然周 = 在途周
    sums.pop(cur_key, None)                            # 整周剔除
    return list(sums.values())                          # 升序(完整周,旧→新)


def min_history() -> int:
    """入场判定所需最少日线根数(粗过滤;精确门槛看完整周数)。"""
    return int(_CFG["最少历史根数"])


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否入选,返回逐条布尔明细 + SELECT。

    历史不足(完整周 < 周量均窗 或 日线 < 最少历史根数 或 t<1)→ SELECT=False + 原因。
    只用 t 及之前的数据(防未来函数);当周整周剔除。
    """
    c = cfg or _CFG
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "原因": "索引越界"}
    need_bars = int(c["最少历史根数"])
    if t + 1 < need_bars or t < 1:
        return {"SELECT": False, "原因": f"历史不足({t + 1}<{need_bars})"}

    close = kdf["close"].to_numpy(dtype=float)
    open_ = kdf["open"].to_numpy(dtype=float)
    vol = kdf["volume"].to_numpy(dtype=float)

    # —— C1 周线近期放量(当周在途不计)——
    need_weeks = int(c["周量均窗"])
    wv = weekly_volumes(kdf, t)
    if len(wv) < max(need_weeks, 2):
        return {"SELECT": False, "原因": f"完整周不足({len(wv)}<{need_weeks})"}
    ma_w = sum(wv[-need_weeks:]) / need_weeks           # MA(周量,8)=近 8 完整周均量
    ref1, ref2 = wv[-1], wv[-2]                         # 上一/上上完整周
    mult = float(c["周量倍数"])
    c1 = (ref1 > ma_w * mult) or (ref2 > ma_w * mult)

    # —— C2 短均线多头 ——
    ps, pl = [int(x) for x in c["短均线"]]
    ma_s, ma_l = _ma(close, t, ps), _ma(close, t, pl)
    c2 = (ma_s == ma_s) and (ma_l == ma_l) and (ma_s > ma_l)

    # —— C3 当日缩量回踩(价):收阴且收跌 ——
    c3 = (close[t] < open_[t]) and (close[t] < close[t - 1])

    # —— C4 接近地量(量)——
    vw = int(c["地量均窗"])
    ma_v = _ma(vol, t, vw)
    c4 = (ma_v == ma_v) and (vol[t] <= ma_v * float(c["地量系数"]))

    # —— C5 贴 10 日线 ——
    lp = int(c["贴线均线"])
    ma_line = _ma(close, t, lp)
    c5 = (ma_line == ma_line) and (ma_line > 0) and \
        (abs(close[t] - ma_line) / ma_line <= float(c["贴线容差"]))

    select = bool(c1 and c2 and c3 and c4 and c5)
    return {
        "SELECT": select,
        "C1_周线放量": bool(c1), "C2_短均多头": bool(c2), "C3_缩量回踩": bool(c3),
        "C4_接近地量": bool(c4), "C5_贴10日线": bool(c5),
        "明细": {
            "周量REF1": round(ref1, 2), "周量REF2": round(ref2, 2),
            "周量MA8": round(ma_w, 2), "完整周数": len(wv),
            f"MA{ps}": (round(ma_s, 4) if ma_s == ma_s else None),
            f"MA{pl}": (round(ma_l, 4) if ma_l == ma_l else None),
            "close": round(float(close[t]), 4), "open": round(float(open_[t]), 4),
            "prev_close": round(float(close[t - 1]), 4),
            "V": round(float(vol[t]), 2), f"MA_V{vw}": (round(ma_v, 2) if ma_v == ma_v else None),
            f"MA{lp}": (round(ma_line, 4) if ma_line == ma_line else None),
            "贴线偏离": (round(abs(close[t] - ma_line) / ma_line, 4)
                        if (ma_line == ma_line and ma_line > 0) else None),
        },
    }


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, cfg)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_s02_screen(codes: list[str], as_of: str | None = None,
                   fetch: bool = True) -> dict:
    """扫描 codes,对每票判最后一根是否入选,落 view「放量后缩量回踩」。返回 summary。

    fetch=True:缺 K 线自动采集;False:只读本地缓存(离线复算,不触网)。
    历史不足(<最少历史根数)的票记入「跳过数」,不入选(不足不选)。
    """
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
            selected.append({"code": code, "明细": r["明细"]})

    view = {
        "as_of": as_of,
        "策略": "放量后缩量回踩(S02)",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": ("C1 REF(周量,1)或REF(周量,2)>MA(周量,8)×1.6(当周在途不计)AND "
                 "C2 MA5>MA10 AND C3 C<O 且 C<REF(C,1)(收阴收跌)AND "
                 "C4 V≤MA(V,10)×0.70 AND C5 |C−MA10|/MA10≤0.03"),
        "防未来函数": "只用 t 及之前;当周整周剔除;完整周<8 或 日线<41 不选",
    }
    p = store.put_view("放量后缩量回踩", view)
    logger.info("放量后缩量回踩:扫描 %d / 有效 %d / 跳过(历史不足)%d / 入选 %d → %s",
                len(codes), scanned, skipped, len(selected), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S02 放量后缩量回踩 入场扫描")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    else:
        codes = universe.universe_codes(limit=a.universe)
    logger.info("S02 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_s02_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
