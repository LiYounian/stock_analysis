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
_TREND_CFG = THRESHOLDS["S02趋势过滤"]


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


def trend_min_history(cfg: dict | None = None) -> int:
    """趋势模板判定所需最少日线根数(52 周高低点窗为最紧约束)。不足→不通过趋势门。"""
    tc = cfg or _TREND_CFG
    return int(tc["最少历史根数"])


def _trend_template(kdf: pd.DataFrame, t: int, rs_rank: float | None,
                    rs_up: bool | None = None, cfg: dict | None = None) -> dict:
    """Minervini 8 条趋势模板门:在第 t 根判该票是否处于强势上升趋势(全过=PASS)。

    只用 ≤ t 的数据(防未来函数)。`rs_rank` 为**当日全A 横截面**的 RS 百分位(0–100),
    须由调用方预计算后喂入(单票函数不做横截面);为 None → 条件 8 无法判 → PASS=False。
    `rs_up` 可选:RS 曲线是否向上(RS[t]>RS[t−RS曲线回看]);仅当配置「启用RS曲线向上」为真时参与。
    历史不足(< 最少历史根数)→ PASS=False + 原因(次新股不通过)。
    """
    tc = cfg or _TREND_CFG
    n = len(kdf)
    if t < 0 or t >= n:
        return {"PASS": False, "原因": "索引越界"}
    need = int(tc["最少历史根数"])
    if t + 1 < need:
        return {"PASS": False, "原因": f"历史不足({t + 1}<{need})", "跳过": True}

    close = kdf["close"].to_numpy(dtype=float)
    high = kdf["high"].to_numpy(dtype=float)
    low = kdf["low"].to_numpy(dtype=float)

    p50, p150, p200 = int(tc["MA50"]), int(tc["MA150"]), int(tc["MA200"])
    ma50 = _ma(close, t, p50)
    ma150 = _ma(close, t, p150)
    ma200 = _ma(close, t, p200)
    up_look = int(tc["MA200上升回看"])
    strong_look = int(tc["MA200强上升回看"])
    ma200_prev = _ma(close, t - up_look, p200) if t - up_look >= 0 else float("nan")
    ma200_strong_prev = _ma(close, t - strong_look, p200) if t - strong_look >= 0 else float("nan")

    win = int(tc["周窗口"])
    seg_lo = low[t - win + 1: t + 1]
    seg_hi = high[t - win + 1: t + 1]
    low52 = float(seg_lo.min()) if len(seg_lo) else float("nan")
    high52 = float(seg_hi.max()) if len(seg_hi) else float("nan")

    c = float(close[t])
    lo_mult = float(tc["低点距离倍数"])
    hi_mult = float(tc["高点距离倍数"])
    rs_thr = float(tc["RS排名门槛"])

    def ok(x) -> bool:
        return x == x  # 非 NaN

    cond1 = ok(ma150) and ok(ma200) and c > ma150 and c > ma200
    cond2 = ok(ma150) and ok(ma200) and ma150 > ma200
    cond3 = ok(ma200) and ok(ma200_prev) and ma200 > ma200_prev
    cond3_strong = ok(ma200) and ok(ma200_strong_prev) and ma200 > ma200_strong_prev
    cond4 = ok(ma50) and ok(ma150) and ok(ma200) and ma50 > ma150 and ma50 > ma200
    cond5 = ok(ma50) and c > ma50
    cond6 = ok(low52) and c >= low52 * lo_mult
    cond7 = ok(high52) and c >= high52 * hi_mult
    cond8_rank = (rs_rank is not None) and (float(rs_rank) >= rs_thr)
    if bool(tc.get("启用RS曲线向上", False)):
        cond8 = cond8_rank and bool(rs_up)
    else:
        cond8 = cond8_rank

    passed = bool(cond1 and cond2 and cond3 and cond4 and cond5
                  and cond6 and cond7 and cond8)
    return {
        "PASS": passed,
        "C1_价上150_200": bool(cond1), "C2_150上200": bool(cond2),
        "C3_200上升": bool(cond3), "C3_200强上升": bool(cond3_strong),
        "C4_50上150_200": bool(cond4), "C5_价上50": bool(cond5),
        "C6_距52低≥30%": bool(cond6), "C7_距52高≤25%": bool(cond7),
        "C8_RS达标": bool(cond8),
        "明细": {
            "close": round(c, 4),
            f"MA{p50}": (round(ma50, 4) if ok(ma50) else None),
            f"MA{p150}": (round(ma150, 4) if ok(ma150) else None),
            f"MA{p200}": (round(ma200, 4) if ok(ma200) else None),
            f"MA200[t-{up_look}]": (round(ma200_prev, 4) if ok(ma200_prev) else None),
            "低52": (round(low52, 4) if ok(low52) else None),
            "高52": (round(high52, 4) if ok(high52) else None),
            "RS百分位": (round(float(rs_rank), 2) if rs_rank is not None else None),
            "RS曲线向上": (bool(rs_up) if rs_up is not None else None),
        },
    }


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None,
              trend_filter: bool = False, rs_rank: float | None = None,
              rs_up: bool | None = None, trend_cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否入选,返回逐条布尔明细 + SELECT。

    历史不足(完整周 < 周量均窗 或 日线 < 最少历史根数 或 t<1)→ SELECT=False + 原因。
    只用 t 及之前的数据(防未来函数);当周整周剔除。

    可选 `trend_filter=True`(默认关,**向后兼容**:关时结果逐字节等价旧版,不加任何键):
      在 base SELECT 之上叠加 Minervini 趋势门(见 `_trend_template`),base 通过且趋势门
      PASS 才 SELECT=True,并在返回加 `趋势门` 明细键。`rs_rank` 为当日全A 横截面 RS 百分位,
      须由调用方喂入;为 None → 趋势门条件 8 不通过 → SELECT=False(诚实标注)。
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

    base_select = bool(c1 and c2 and c3 and c4 and c5)
    result = {
        "SELECT": base_select,
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
    if not trend_filter:
        return result                                       # 向后兼容:关时逐字节等价旧版
    # —— 叠加趋势门(只在 base 通过时才有意义;base 未过则趋势门无关,SELECT 已为 False)——
    gate = _trend_template(kdf, t, rs_rank, rs_up=rs_up, cfg=trend_cfg)
    result["趋势门"] = gate
    result["SELECT"] = bool(base_select and gate.get("PASS"))
    return result


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, cfg)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline(code)
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
