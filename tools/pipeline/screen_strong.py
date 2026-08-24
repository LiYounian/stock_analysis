"""策略 S05「最强选股」入场 Screener(**硬依赖 Tushare 筹码获利比例,仅 Tushare 可用时出**)。

看多型:六均线多头 + 近期连续大涨 + 高位区间 + 筹码高度获利。筹码获利比例(`cyq_perf`
的 winner_rate / cost_95pct)**免费源拿不到**,故未配 Tushare / 取不到筹码 → **不产出选股结果**
(返回 present=False + "需 Tushare" 提示),**不用免费源硬凑**(见方案 J 表)。

规格(参数全读 THRESHOLDS["最强选股"];当日 = 第 t 根,均前复权 OHLC + 当日筹码):
  ① 六均线多头:MA5>MA10>MA20>MA30>MA60>MA200
  ② 近期连续大涨:近 涨幅窗口 日内单日涨≥涨幅阈值 的天数 ≥ 涨幅次数
  ③ 高位区间:贴近高下界·H52 < C < 贴近高上界·H52(H52=近 H52窗口 日最高价,含当日)
  ④ 筹码高度获利:winner_rate > 获利比阈值(%)  或  HIGH ≥ cost_95pct
  SELECT = ①∧②∧③∧④   (chip=None → ④False → 不选)

防未来函数:只用 t 及之前;筹码用当日快照。⚠️ 非投资建议。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.trend_template import indicators as ind
from tools.collectors import market, tushare_daily
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_strong")

_CFG = THRESHOLDS["最强选股"]


def min_history() -> int:
    return int(_CFG["最少历史根数"])


def signal_at(kdf: pd.DataFrame, t: int, chip: dict | None = None,
              cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否入选。chip={winner_rate, cost_95pct}(Tushare cyq_perf);None→④False。"""
    c = cfg or _CFG
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "原因": "索引越界"}
    need = int(c["最少历史根数"])
    if t + 1 < need or t < 1:
        return {"SELECT": False, "原因": f"历史不足({t + 1}<{need})"}

    close = kdf["close"].to_numpy(dtype=float)
    high = kdf["high"].to_numpy(dtype=float)

    # ① 六均线多头
    periods = [int(p) for p in c["均线多头周期"]]
    mas = [ind.ma(close, t, p) for p in periods]
    c1 = all(m is not None for m in mas) and all(mas[i] > mas[i + 1] for i in range(len(mas) - 1))

    # ② 近期连续大涨
    win = int(c["涨幅窗口"])
    thr = 1.0 + float(c["涨幅阈值"])
    start = max(1, t - win + 1)
    big = sum(1 for i in range(start, t + 1) if close[i - 1] > 0 and close[i] >= close[i - 1] * thr)
    c2 = big >= int(c["涨幅次数"])

    # ③ 高位区间
    h52 = ind.highest_high(high, t, int(c["H52窗口"]))
    c3 = (h52 is not None and h52 > 0
          and close[t] > h52 * float(c["贴近高下界"]) and close[t] < h52 * float(c["贴近高上界"]))

    # ④ 筹码高度获利(Tushare 独有)
    wr = cost95 = None
    c4 = False
    if chip:
        wr = chip.get("winner_rate")
        cost95 = chip.get("cost_95pct")
        c4 = ((wr is not None and float(wr) > float(c["获利比阈值"]))
              or (cost95 is not None and high[t] >= float(cost95)))

    select = bool(c1 and c2 and c3 and c4)
    return {
        "SELECT": select,
        "C1_六均线多头": bool(c1), "C2_近期连涨": bool(c2), "C3_高位区间": bool(c3),
        "C4_筹码获利": bool(c4),
        "明细": {
            "winner_rate": (round(float(wr), 2) if wr is not None else None),
            "cost_95pct": (round(float(cost95), 4) if cost95 is not None else None),
            "close": round(float(close[t]), 4), "high": round(float(high[t]), 4),
            "H52": (round(float(h52), 4) if h52 is not None else None),
            "近期大涨次数": int(big),
        },
    }


def screen_latest(kdf: pd.DataFrame, chip: dict | None = None, cfg: dict | None = None) -> dict:
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "原因": "空 K 线"}
    return signal_at(kdf, n - 1, chip=chip, cfg=cfg)


def _load_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def _chip_map(as_of: str) -> dict | None:
    """取当日全市场筹码 → {code: {winner_rate, cost_95pct}}。取不到返回 None。"""
    try:
        df = tushare_daily.fetch_chip(as_of)
    except Exception as e:
        logger.warning("Tushare 筹码 cyq_perf(%s) 取失败:%s", as_of, e)
        return None
    return {r["code"]: {"winner_rate": r["winner_rate"], "cost_95pct": r["cost_95pct"]}
            for _, r in df.iterrows()}


def run_strong_screen(codes: list[str], as_of: str | None = None,
                      fetch: bool = True) -> dict | None:
    """扫描 codes,落 view「最强选股」。**仅 Tushare 可用且筹码取得到时出**;否则写"需 Tushare"占位 view 并返回。

    未配 token / 筹码取不到 → 不产出选股(不用免费源硬凑),view 标 present=False + 提示。
    """
    if as_of:
        store.set_active_date(as_of)
    if not tushare_daily.is_configured():
        view = {"as_of": as_of, "策略": "最强选股(S05)", "方向": "看多",
                "present": False, "需要Tushare": True,
                "提示": "「最强选股」依赖 Tushare 筹码获利比例(cyq_perf),需配置 TUSHARE_TOKEN 才出;当前未配置。",
                "入选清单": [], "入选数": 0}
        store.put_view("最强选股", view)
        logger.info("最强选股:未配 Tushare,跳过(写占位提示 view)")
        return view
    chip = _chip_map(as_of or pd.Timestamp.today().strftime("%Y-%m-%d"))
    if not chip:
        view = {"as_of": as_of, "策略": "最强选股(S05)", "方向": "看多",
                "present": False, "需要Tushare": True,
                "提示": "Tushare 筹码 cyq_perf 当日取不到(非交易日/未收盘/接口限权),本日「最强选股」不出。",
                "入选清单": [], "入选数": 0}
        store.put_view("最强选股", view)
        # 三分法告警:能走到这里说明 is_configured()=True(未配 token 已在上一分支 return),
        # 却没取到筹码 → 极可能 token 失效/额度用尽/权限不足。这是唯一需要 WARNING 的情形:
        #   ①未配 token → 上一分支正常占位,不告警;
        #   ②配了 token 但入选0 → 合法结果(往下走,不告警);
        #   ③配了 token 但筹码取不到/出不了 → 就是这里,静默回落占位最难察觉 → 显式告警。
        logger.warning("最强选股:已配 TUSHARE_TOKEN 但筹码 cyq_perf(%s)未取到"
                       "(可能 token 失效/额度用尽/权限不足),策略9 将回落「需 Tushare」占位、当日不出",
                       as_of or "today")
        logger.info("最强选股:筹码取不到,跳过(写占位提示 view)")
        return view

    need = min_history()
    selected: list[dict] = []
    scanned = skipped = 0
    for code in codes:
        kdf = _load_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = screen_latest(kdf, chip=chip.get(code))
        if r.get("SELECT"):
            selected.append({"code": code, "明细": r["明细"]})

    view = {
        "as_of": as_of, "策略": "最强选股(S05)", "方向": "看多", "present": True,
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected), "入选清单": selected,
        "规则": ("六均线多头(MA5>10>20>30>60>200)AND 11日内≥2日涨≥5% AND "
                 "0.9·H52<C<1.2·H52 AND (winner_rate>95% 或 HIGH≥cost_95pct)"),
        "数据源": "Tushare cyq_perf 筹码获利比例(免费源拿不到)",
        "防未来函数": "只用 t 及之前;筹码用当日快照;日线<250 不选",
    }
    store.put_view("最强选股", view)
    logger.info("最强选股:扫描 %d / 有效 %d / 跳过 %d / 入选 %d(筹码 %d 只)",
                len(codes), scanned, skipped, len(selected), len(chip))
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from tools.collectors import universe

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S05 最强选股 入场扫描(仅 Tushare 可用时出)")
    ap.add_argument("--universe", type=int, metavar="N")
    ap.add_argument("--codes")
    ap.add_argument("--date")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [x.strip() for x in a.codes.split(",") if x.strip()]
    else:
        codes = universe.universe_codes(limit=a.universe)
    v = run_strong_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:%s", {k: v.get(k) for k in ("present", "入选数", "提示")})
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
