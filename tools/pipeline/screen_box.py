"""策略 3「箱体形态」全A规则型 Screener。

当日盘后逐票:末根放量突破一段窄幅箱体上沿即入选(SELECT = 箱体达标,单日触发)。
不重写箱体几何——**直接复用** `tools.analysis.pattern_screener.pattern.detect_box`
(cfg 缺省读 `THRESHOLDS["形态选股"]["箱体"]`:窗口/高度上限%/突破幅度%/突破放量倍数)。

规格(前复权 OHLCV,当日 = t;整数索引 t 指 kdf 第 t 根):
  窄幅:不含末根的 win 根箱体高度(箱顶/箱底)% ≤ 高度上限%
  突破:C > 箱顶 × (1 + 突破幅度%)
  放量:末根量 > 前 win 根均量 × 突破放量倍数
  SELECT = 窄幅 AND 突破 AND 放量

防未来函数红线:detect_box 只看「不含末根的箱体 + 末根突破」;按 t 判定时把 kdf
截到 [0, t] 再识别,绝不引入 t 之后的交易日。历史需 ≥ 窗口+1 根,不足不选。

数据只读复用 `collectors.market.load_kline`(优先滚动主档、回退 raw)。
入口:`python -m tools.pipeline.screen_box [--codes ...|--universe N] [--date D] [--no-fetch]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import technical as ta
from tools.analysis.pattern_screener.pattern import detect_box, detect_box_v2
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_box")

_CFG_FS = THRESHOLDS["形态选股"]
_CFG = _CFG_FS["箱体"]          # v1(旧,A/B 对比)
_CFG2 = _CFG_FS["箱体v2"]       # v2(提供者规格重构)


def min_history(use_v2: bool = True) -> int:
    """箱体识别所需最少日线根数(不足不选)。

    v1:窗口 + 1 根突破。v2:max(箱体窗上界+1, 趋势门 MA200 所需 200 根)。
    """
    if not use_v2:
        return int(_CFG["窗口"]) + 1
    box_need = int(_CFG2["窗口区间"][1]) + 1
    ma_need = 200 if _CFG2.get("趋势门", {}).get("启用") and _CFG2["趋势门"].get("MA200") else box_need
    return max(box_need, ma_need)


def _trend_gate(kdf: pd.DataFrame, t: int, c2: dict) -> tuple[bool, dict]:
    """趋势门(硬门,只用 [0,t]):站上 MA200(年线)+ 短均线多头(MA5>MA10>MA20)。

    剔除下跌趋势个股——治"即死61%"的关键门。MA200 需满 200 根历史,不足→不过(保守剔除)。
    返回 (是否通过, 明细)。
    """
    g = c2.get("趋势门", {})
    if not g.get("启用", True):
        return True, {"启用": False}
    close = kdf["close"].iloc[: t + 1]
    detail: dict = {}
    ok = True
    if g.get("MA200", True):
        if len(close) < 200:
            return False, {"历史不足MA200": int(len(close))}
        ma200 = float(ta.ma(close, 200).iloc[-1])
        ma200_ok = float(close.iloc[-1]) > ma200
        detail.update({"MA200": round(ma200, 3), "站上MA200": bool(ma200_ok)})
        ok = ok and ma200_ok
    if g.get("短均线多头", True):
        ma5, ma10, ma20 = (float(ta.ma(close, w).iloc[-1]) for w in (5, 10, 20))
        bull = ma5 > ma10 > ma20
        detail.update({"MA5>10>20": bool(bull)})
        ok = ok and bull
    return bool(ok), detail


def _box_stop(bot: float, price: float, kdf: pd.DataFrame) -> dict:
    """止损点位:箱体下轨 与 ATR(2×)止损 取更近者(离现价更近=止损更紧)。复用 predict.atr。"""
    from tools.analysis.predict import atr
    sl_atr = None
    try:
        atr_v = float(atr(kdf).iloc[-1])
        if atr_v == atr_v:                       # 非 NaN
            sl_atr = price - 2 * atr_v
    except Exception:
        sl_atr = None
    sl = bot if sl_atr is None else max(bot, sl_atr)
    return {"止损价": round(sl, 3), "依据": "箱体下轨" if (sl_atr is None or sl == bot) else "ATR(2×)",
            "箱体下轨": round(bot, 3), "ATR止损": round(sl_atr, 3) if sl_atr is not None else None}


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None,
              use_v2: bool = True) -> dict:
    """判 kdf 第 t 根是否命中箱体形态,返回 {SELECT, 特征}。

    只用 t 及之前的数据(防未来函数):把 kdf 截到 [0, t] 再识别。
    use_v2=True:v2 几何(振幅带+站稳突破+放量硬门,触碰/缩量/横盘软信号)AND 趋势门(MA200+短多头),
    入选补 箱顶/箱底/止损 结构化输出;use_v2=False:走旧 detect_box(A/B 对比)。
    历史不足 → SELECT=False + 原因。cfg 缺省用 THRESHOLDS["形态选股"]。
    """
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "特征": {"原因": "索引越界"}}
    sub = kdf.iloc[: t + 1]
    if not use_v2:
        r = detect_box(sub, cfg)
        return {"SELECT": bool(r.get("达标")), "特征": r.get("特征", {})}
    c2 = (cfg or _CFG_FS).get("箱体v2", _CFG2)
    r = detect_box_v2(sub, cfg)
    feat = dict(r.get("特征", {}))
    if not r.get("达标"):
        return {"SELECT": False, "特征": feat}
    tg_ok, tg = _trend_gate(kdf, t, c2)
    feat["趋势门"] = tg
    if not tg_ok:
        feat["趋势门未过"] = True
        return {"SELECT": False, "特征": feat}
    price = float(sub["close"].iloc[-1])
    feat["止损"] = _box_stop(feat["箱底"], price, sub)
    return {"SELECT": True, "特征": feat}


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None,
                  use_v2: bool = True) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "特征": {"原因": "空 K 线"}}
    return signal_at(kdf, n - 1, cfg, use_v2=use_v2)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_box_screen(codes: list[str], as_of: str | None = None,
                   fetch: bool = True, use_v2: bool = True) -> dict:
    """扫描 codes,对每票判最后一根是否命中箱体,落 view「箱体形态」。返回 summary。

    fetch=True:缺 K 线自动采集;False:只读本地缓存(离线复算,不触网)。
    use_v2=True(默认):v2 规格(振幅带+站稳突破+放量+趋势门,触碰/缩量/横盘软信号)+ 结构化输出
    (箱顶/箱底/止损);use_v2=False:旧 detect_box。历史不足的票记入「跳过数」,不入选。
    """
    if as_of:
        store.set_active_date(as_of)
    need = min_history(use_v2)
    selected: list[dict] = []
    scanned = skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        r = screen_latest(kdf, use_v2=use_v2)
        if r.get("SELECT"):
            f = r["特征"]
            item = {"code": code, "特征": f}
            if use_v2:
                item.update({"箱顶": f.get("箱顶"), "箱底": f.get("箱底"),
                             "振幅%": f.get("振幅%"), "结构评分": f.get("结构评分"),
                             "止损": f.get("止损")})
            selected.append(item)

    if use_v2:
        c2 = _CFG2
        rule = (f"v2:自适应箱体窗{c2['窗口区间']} + 振幅∈[{c2['振幅下界%']},{c2['振幅上界%']}]% + "
                f"站稳突破(C>箱顶×(1+{c2['站稳容差%']}%)) + 放量×{c2['突破放量倍数']} [硬门];"
                f"触碰≥{c2['触碰次数']}/缩量/横盘 软信号(结构分≥{c2['结构分下限']});"
                f"趋势门 站上MA200+MA5>10>20 [硬门,剔下跌]")
        reuse = "pattern.detect_box_v2 + technical.ma(趋势门) + predict.atr(止损)"
    else:
        win = int(_CFG["窗口"])
        rule = (f"v1:窄幅(≤{_CFG['高度上限%']}%)AND 突破(×(1+{_CFG['突破幅度%']}%))AND "
                f"放量(×{_CFG['突破放量倍数']})")
        reuse = "pattern.detect_box"
    view = {
        "as_of": as_of,
        "策略": "箱体形态(策略3)" + ("· v2 提供者规格" if use_v2 else "· v1"),
        "版本": "v2" if use_v2 else "v1",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": rule,
        "复用": reuse,
        "防未来函数": f"按 t 截 [0,t] 识别(箱体不含末根)+ 趋势门只用 [0,t];历史<{need} 不选",
    }
    p = store.put_view("箱体形态", view)
    logger.info("箱体形态:扫描 %d / 有效 %d / 跳过(历史不足)%d / 入选 %d → %s",
                len(codes), scanned, skipped, len(selected), p)
    return view


def _offline_universe_codes(limit: int | None = None) -> list[str]:
    """离线枚举全A票池:主档代码 ∪ 本地 raw kline 分区文件名(6 位数字)。不触网。"""
    codes = set(store.list_master_codes())
    from tools.config import settings
    raw_root = settings.DATA_RAW
    if raw_root.exists():
        for p in raw_root.glob("**/kline/*.parquet"):
            stem = p.stem
            if len(stem) == 6 and stem.isdigit():
                codes.add(stem)
    out = sorted(codes)
    if limit:
        out = out[:limit]
    return out


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 3 箱体形态 全A扫描")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--v1", action="store_true", help="用旧 detect_box(默认 v2 提供者规格)")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.no_fetch:
        codes = _offline_universe_codes(limit=a.universe)   # 离线:从本地已缓存 K 线枚举
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("箱体形态 扫描:%d 只(日期 %s,fetch=%s,版本=%s)",
                len(codes), as_of, not a.no_fetch, "v1" if a.v1 else "v2")
    v = run_box_screen(codes, as_of=as_of, fetch=not a.no_fetch, use_v2=not a.v1)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
