"""策略 4「动量组合」全A规则型 Screener(仅 A 腿·纯价格动量口径)。

复用 `tools.strategy.momentum` 的组合选股算子,当日盘后逐票打分排序取 TopK:
  策略A（本 screener 全A口径） combo_momentum_screen
    加权对数动量打分 → R² 闸门 + 拉普拉斯低通「买」信号闸门 → 按动量分排序取 TopK。
    **只需收盘序列**(closes),全A 可低成本跑(closes_loader 走 market.load_kline)。

设计权衡（B 腿为何不进全A）:
  momentum 里另有 combo_dividend_momentum_screen(策略B:质地过滤 ROE/营收/净利增速
  → BBI 闸门 → 24 日动量排序)。策略B **需 record 里的基本面字段**,全A 逐票拉基本面
  成本高(触发大规模采集)。折中:**全A 只跑 A 腿**(纯价格动量,便宜);B 腿因需基本面
  在全A screener 里**跳过**,仍由 web 层在自选池实时跑(那里 fundamental 已缓存)。
  故本 screener 落的 view 明确标注「仅含 A 腿/全A 口径」,避免与 web 的 A+B 结果混淆。

closes 全A 加载:构造**最小 records**(每票 `{"meta":{"code": c}}`),真正的收盘序列
由注入的 closes_loader(走 market.load_kline)提供——**不为动量 A 触发全A serialize**。

防未来函数:加权对数动量/拉普拉斯信号均只用 t 及之前(见 momentum 各算子实现);
本 screener 用「截至当日」的完整收盘序列,尾部即当日。历史需 ≥ 回看+1 根,不足跳过。

入口:`python -m tools.pipeline.screen_momentum [--codes ...|--universe N] [--date D]
       [--no-fetch] [--top-k K]`。
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store
from tools.strategy.momentum import combo_momentum_screen, weighted_log_momentum

logger = logging.getLogger("pipeline.screen_momentum")

# —— A 腿默认参数(与 momentum.combo_momentum_screen 缺省对齐,可 CLI 覆盖 top_k)——
# Top-K 30→10:回测坐实动量 edge 只在 1 日尺度、且越窄越准(h=1 Top5 超额+1.27%/Top10 +0.84% p=0.06;
# 5 日起衰减转负)。收窄到 Top10(比 Top5 样本更厚更稳);清单仅供 1 日短线参考,不宜持有>1 日。
# 详见 docs/策略成绩报告.md(动量4 处置:保留但限用途·1日/Top-N 尺度)。
DEFAULT_TOP_K = 10
LOOKBACK_DAYS = 25
R2_MIN = 0.4
LAPLACE_S = 0.07
MIN_SLOPE = 0.002


def min_history() -> int:
    """A 腿打分所需最少日线根数(加权对数动量需 lookback+1 根,不足跳过)。"""
    return LOOKBACK_DAYS + 1


def _store_closes_loader(code: str) -> np.ndarray | None:
    """code → 收盘序列(np.ndarray)|None。只读 market.load_kline,不采集、不触网。"""
    try:
        kdf = market.load_kline_recent(code)
    except FileNotFoundError:
        return None
    except Exception:                                  # noqa: BLE001
        return None
    if kdf is None or len(kdf) == 0 or "close" not in kdf.columns:
        return None
    return kdf["close"].astype(float).to_numpy()


def run_momentum_screen(codes: list[str], as_of: str | None = None,
                        fetch: bool = True, top_k: int = DEFAULT_TOP_K) -> dict:
    """扫描 codes,跑 A 腿动量组合,落 view「动量组合」。返回 summary。

    仅 A 腿(纯价格动量):加权对数动量 → R²≥{R2_MIN} + 拉普拉斯「买」闸门 → TopK。
    fetch=True:缺 K 线自动采集补齐收盘;False:只读本地缓存(离线复算,不触网)。
    历史不足(<回看+1)的票不参与打分(记入「跳过数」)。
    """
    if as_of:
        store.set_active_date(as_of)

    # closes_loader:fetch 时缺档补采;否则纯本地。构造最小 records(不 serialize 全A)。
    def loader(code: str) -> np.ndarray | None:
        arr = _store_closes_loader(code)
        if arr is None and fetch:
            try:
                kdf = market.fetch_kline([code]).get(code)
            except Exception:                          # noqa: BLE001
                kdf = None
            if kdf is not None and len(kdf) and "close" in kdf.columns:
                arr = kdf["close"].astype(float).to_numpy()
        return arr

    need = min_history()
    records: dict[str, dict] = {}
    scanned = skipped = 0
    closes_cache: dict[str, np.ndarray | None] = {}
    for code in codes:
        arr = loader(code)
        closes_cache[code] = arr
        if arr is None or len(arr) < need:
            skipped += 1
            continue
        scanned += 1
        records[code] = {"meta": {"code": code}}       # 最小 record,收盘由 loader 提供

    picks = combo_momentum_screen(
        records,
        lookback_days=LOOKBACK_DAYS,
        r2_min=R2_MIN,
        top_k=top_k,
        s=LAPLACE_S,
        min_slope=MIN_SLOPE,
        closes_loader=lambda c: closes_cache.get(c),
    )

    # 补充每只入选票的动量明细(重算一次评分,便于人读 view)。
    selected: list[dict] = []
    for code in picks:
        arr = closes_cache.get(code)
        mom = weighted_log_momentum(arr, lookback_days=LOOKBACK_DAYS) if arr is not None else {}
        selected.append({
            "code": code,
            "特征": {
                "动量分": round(float(mom.get("score", 0.0)), 6),
                "年化": round(float(mom.get("annualized", 0.0)), 4),
                "R²": round(float(mom.get("r_squared", 0.0)), 4),
            },
        })

    view = {
        "as_of": as_of,
        "策略": "动量组合(策略4·仅A腿/全A口径)",
        "口径": ("仅含 A 腿(纯价格动量:加权对数动量+拉普拉斯闸门+R²);"
                 "B 腿(红利动量·需基本面质地)因全A采集成本高在本 screener 跳过,"
                 "由 web 在自选池实时跑。"),
        "扫描数": len(codes), "有效样本": scanned, "跳过数(历史不足)": skipped,
        "入选数": len(selected),
        "top_k": top_k,
        "适用尺度": "⚠仅 1 日短线尺度有选择性(回测:h=1 Top-N 有超额,持有≥5 日衰减转负);不宜持有>1 日。",
        "入选清单": selected,
        "规则": (f"加权对数动量打分(lookback={LOOKBACK_DAYS})→ R²≥{R2_MIN} + "
                 f"拉普拉斯低通末根='买'(s={LAPLACE_S},min_slope={MIN_SLOPE})闸门 → "
                 f"按动量分降序取 Top{top_k}(限 1 日尺度)"),
        "复用": "tools.strategy.momentum.combo_momentum_screen(closes_loader 注入)",
        "防未来函数": f"动量/信号只用 t 及之前;尾部即当日;历史<{need} 跳过",
    }
    p = store.put_view("动量组合", view)
    logger.info("动量组合:扫描 %d / 有效 %d / 跳过(历史不足)%d / 入选 %d → %s",
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
    ap = argparse.ArgumentParser(description="策略 4 动量组合(仅A腿) 全A扫描")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(不传=全量)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于 --universe)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K, help=f"取前 K 只(默认 {DEFAULT_TOP_K})")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.no_fetch:
        codes = _offline_universe_codes(limit=a.universe)   # 离线:从本地已缓存 K 线枚举
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("动量组合 扫描:%d 只(日期 %s,fetch=%s,top_k=%d)",
                len(codes), as_of, not a.no_fetch, a.top_k)
    v = run_momentum_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
