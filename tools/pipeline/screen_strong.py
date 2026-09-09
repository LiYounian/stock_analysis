"""策略 S05「最强选股」入场 Screener(筹码取数源可切:本地 chip 推演 / Tushare cyq_perf)。

看多型:六均线多头 + 近期连续大涨 + 高位区间 + 筹码高度获利。④号「筹码高度获利」的取数由
开关 `strategy.STRONG_CHIP_SOURCE`(env `STRONG_CHIP_SOURCE`)选源:
  · local(默认)= 本地 `chip.py` 推演(获利比例×100→winner_rate、成本区间上沿→cost_95pct),
                  15:40 主流程当场可算、**零 Tushare 依赖**;取不到换手率/数据不足 → ④False、不选。
  · tushare     = 旧行为回退,走 `tushare_daily.fetch_chip`(cyq_perf);未配 token / 取不到筹码
                  → 返回 present=False + "需 Tushare" 提示,**不产出选股**(不用免费源硬凑)。

规格(参数全读 THRESHOLDS["最强选股"];当日 = 第 t 根,均前复权 OHLC + 当日筹码):
  ① 六均线多头:MA5>MA10>MA20>MA30>MA60>MA200
  ② 近期连续大涨:近 涨幅窗口 日内单日涨≥涨幅阈值 的天数 ≥ 涨幅次数
  ③ 高位区间:贴近高下界·H52 < C < 贴近高上界·H52(H52=近 H52窗口 日最高价,含当日)
  ④ 筹码高度获利:winner_rate > 获利比阈值(%)  或  HIGH ≥ cost_95pct
  SELECT = ①∧②∧③∧④   (chip=None → ④False → 不选)

⚠️ 量纲:signal_at 的 winner_rate 一律是**百分数(0~100)**,与 `获利比阈值=95.0` 同量纲。
  本地源在适配层把 `获利比例`(0~1 小数)×100 转成百分数(写反会让 ④ 恒 False)。
防未来函数:只用 t 及之前;本地筹码 point-in-time(summarize_asof 只用 ≤as_of 的 bar)。⚠️ 非投资建议。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis.trend_template import indicators as ind
from tools.collectors import chip, market, tushare_daily
from tools.config import strategy
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_strong")

_CFG = THRESHOLDS["最强选股"]


def _chip_source() -> str:
    """当前 ④筹码取数源:'local'(默认)或 'tushare'。每次动态读,便于 env / 测试切换。"""
    return getattr(strategy, "STRONG_CHIP_SOURCE", "local") or "local"


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


def _local_chip_of(kdf: pd.DataFrame, as_of: str | None) -> dict | None:
    """本地 chip 适配层:从已加载的 K线推演当日筹码,映射到 cyq_perf 口径的 chip dict。

    映射(方案A,阈值 95.0 不改):`获利比例`(0~1 小数)×100 → winner_rate(百分数,与阈值同量纲)、
    `成本区间上沿`(0.95 分位成本价,元)→ cost_95pct。换手率不可用/数据不足(获利比例 None)→ None
    (交由 signal_at 令 ④False、不选;不伪造筹码)。point-in-time:as_of 指定则 summarize_asof
    只用 ≤as_of 的 bar,无前视偏差。
    """
    try:
        rec = chip.summarize_asof(kdf, as_of) if as_of else chip.summarize(kdf)
    except Exception as e:                       # 推演异常只降级该票,不中断整批
        logger.warning("本地筹码推演失败(降级为无筹码):%s", e)
        return None
    wr = rec.get("获利比例")
    if wr is None:                               # 换手缺失/数据不足 → 无筹码
        return None
    cost95 = rec.get("成本区间上沿")
    return {"winner_rate": float(wr) * 100.0,    # ⚠️ ×100:小数→百分数(与 获利比阈值=95.0 同量纲)
            "cost_95pct": (float(cost95) if cost95 is not None else None)}


def run_strong_screen(codes: list[str], as_of: str | None = None,
                      fetch: bool = True) -> dict | None:
    """扫描 codes,落 view「最强选股」。④筹码取数源由 `strategy.STRONG_CHIP_SOURCE` 决定。

    · local(默认):本地 chip 推演,零 Tushare 依赖,15:40 当场出真值。
    · tushare(回退):**仅 Tushare 可用且筹码取得到时出**;否则写"需 Tushare"占位 view 并返回。
    """
    if as_of:
        store.set_active_date(as_of)
    if _chip_source() == "local":
        return _run_local(codes, as_of, fetch)
    return _run_tushare(codes, as_of, fetch)


def _run_local(codes: list[str], as_of: str | None, fetch: bool) -> dict:
    """本地筹码路径:每票用本地 chip 推演 ④,不触网、不依赖 Tushare。"""
    need = min_history()
    selected: list[dict] = []
    scanned = skipped = degraded = 0
    for code in codes:
        kdf = _load_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        chip_rec = _local_chip_of(kdf, as_of)
        if chip_rec is None:
            degraded += 1
        r = screen_latest(kdf, chip=chip_rec)
        if r.get("SELECT"):
            selected.append({"code": code, "明细": r["明细"]})

    view = {
        "as_of": as_of, "策略": "最强选股(S05)", "方向": "看多", "present": True,
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "筹码不可用数": degraded,
        "入选数": len(selected), "入选清单": selected,
        "规则": ("六均线多头(MA5>10>20>30>60>200)AND 11日内≥2日涨≥5% AND "
                 "0.9·H52<C<1.2·H52 AND (获利比例×100>95% 或 HIGH≥成本区间上沿)"),
        "数据源": "本地筹码推演 chip.py(获利比例×100→winner_rate、成本区间上沿→cost_95pct;零 Tushare)",
        "防未来函数": "只用 t 及之前;本地筹码 point-in-time(summarize_asof 只用 ≤as_of 的 bar);日线<250 不选",
    }
    store.put_view("最强选股", view)
    logger.info("最强选股(local):扫描 %d / 有效 %d / 跳过 %d / 筹码不可用 %d / 入选 %d",
                len(codes), scanned, skipped, degraded, len(selected))
    return view


def _run_tushare(codes: list[str], as_of: str | None, fetch: bool) -> dict:
    """Tushare 回退路径:行为与切换前逐字节等价(未配 token / 取不到筹码 → 占位 view)。"""
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
