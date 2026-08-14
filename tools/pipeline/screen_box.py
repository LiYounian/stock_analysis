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

from tools.analysis.pattern_screener.pattern import detect_box
from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_box")

_CFG = THRESHOLDS["形态选股"]["箱体"]


def min_history() -> int:
    """箱体识别所需最少日线根数(窗口 + 1 根突破,不足不选)。"""
    return int(_CFG["窗口"]) + 1


def signal_at(kdf: pd.DataFrame, t: int, cfg: dict | None = None) -> dict:
    """判 kdf 第 t 根是否命中箱体形态,返回 {SELECT, 特征}。

    只用 t 及之前的数据(防未来函数):把 kdf 截到 [0, t] 再调 detect_box。
    历史不足 → SELECT=False + 原因。cfg 缺省用 THRESHOLDS["形态选股"](含「箱体」子键)。
    """
    n = len(kdf)
    if t < 0 or t >= n:
        return {"SELECT": False, "特征": {"原因": "索引越界"}}
    sub = kdf.iloc[: t + 1]
    # detect_box 期望的 cfg 形如 THRESHOLDS["形态选股"](内部取 ["箱体"]);缺省传 None 走全局。
    r = detect_box(sub, cfg)
    return {"SELECT": bool(r.get("达标")), "特征": r.get("特征", {})}


def screen_latest(kdf: pd.DataFrame, cfg: dict | None = None) -> dict:
    """判**最后一根**(当日盘后逐票用)。历史不足 → SELECT=False。"""
    n = len(kdf)
    if n == 0:
        return {"SELECT": False, "特征": {"原因": "空 K 线"}}
    return signal_at(kdf, n - 1, cfg)


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        return market.fetch_kline([code]).get(code)


def run_box_screen(codes: list[str], as_of: str | None = None,
                   fetch: bool = True) -> dict:
    """扫描 codes,对每票判最后一根是否命中箱体,落 view「箱体形态」。返回 summary。

    fetch=True:缺 K 线自动采集;False:只读本地缓存(离线复算,不触网)。
    历史不足(<窗口+1)的票记入「跳过数」,不入选(不足不选)。
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
            selected.append({"code": code, "特征": r["特征"]})

    win = int(_CFG["窗口"])
    view = {
        "as_of": as_of,
        "策略": "箱体形态(策略3)",
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "入选清单": selected,
        "规则": (f"窄幅(不含末根 {win} 根箱高% ≤ {_CFG['高度上限%']})AND "
                 f"突破(C > 箱顶×(1+{_CFG['突破幅度%']}%))AND "
                 f"放量(末根量 > 前 {win} 根均量×{_CFG['突破放量倍数']})"),
        "复用": "tools.analysis.pattern_screener.pattern.detect_box",
        "防未来函数": f"detect_box 箱体不含末根;按 t 截 [0,t] 识别;历史<{need} 不选",
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
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.no_fetch:
        codes = _offline_universe_codes(limit=a.universe)   # 离线:从本地已缓存 K 线枚举
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("箱体形态 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_box_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
