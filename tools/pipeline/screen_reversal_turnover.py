"""反转低换手组合(候选策略)全 A 规则型 Screener——纯量价、低成本跑全 A。

复用 `tools.strategy.reversal_turnover`:
  · 纯因子函数 reversal_factor / low_turnover_factor / avg_amount_wan 逐票算原始因子
  · combo_reversal_turnover_screen 做横截面 winsorize+zscore → 等权复合 → 过滤 → TopK

数据依赖:只读 kline(close / turnover / amount),不触基本面、不触发全 A serialize
(仿 screen_momentum:构造最小 record `{"meta","反转低换手","snapshot"}`)。

近端数据卫生:新采集端点不给换手率/成交额 → 末几根 turnover/amount 为 NaN。低换手/
流动性因子对**窗口内有效值**取均值(不足半窗 → 剔并计跳过),历史根不受影响。

防未来函数:因子只读序列尾部窗口(反转仅最后 N+1 根、换手仅最后 N 根);本 screener
用"截至当日"完整序列,尾部即当日。历史 < max(反转+1, 次新最少天数) 的票跳过。

命名:已授面板编号「策略10」,状态=前向观测中(非「已验证可用」;net 绝对水平存幸存者水分)。

入口:`python -m tools.pipeline.screen_reversal_turnover
       [--codes ...|--universe N] [--date D] [--no-fetch] [--top-k K]`。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store
from tools.strategy.reversal_turnover import (
    avg_amount_wan,
    combo_reversal_turnover_screen,
    low_turnover_factor,
    reversal_factor,
)

logger = logging.getLogger("pipeline.screen_reversal_turnover")

_CFG = THRESHOLDS.get("反转低换手", {})
DEFAULT_TOP_K = int(_CFG.get("top_k", 20))
REV_N = int(_CFG.get("反转窗口", 5))
TURN_N = int(_CFG.get("换手窗口", 20))
MIN_LISTING_DAYS = int(_CFG.get("次新_上市最少天数", 60))     # 次新过滤


def min_history() -> int:
    """打分所需最少日线根数:取 次新门槛 与 反转窗口+1 的较大者。"""
    return max(MIN_LISTING_DAYS, REV_N + 1)


def _load_series(code: str):
    """code → (closes, turnovers, amounts, last_pct_chg) | None。只读本地缓存,不触网。"""
    try:
        kdf = market.load_kline_recent(code)
    except FileNotFoundError:
        return None
    except Exception:                                  # noqa: BLE001
        return None
    if kdf is None or len(kdf) == 0 or "close" not in kdf.columns:
        return None
    closes = kdf["close"].astype(float).tolist()
    turnovers = kdf["turnover"].tolist() if "turnover" in kdf.columns else []
    amounts = kdf["amount"].tolist() if "amount" in kdf.columns else []
    last_pct = None
    if "pct_chg" in kdf.columns and len(kdf):
        v = kdf["pct_chg"].iloc[-1]
        last_pct = float(v) if pd.notna(v) else None
    return closes, turnovers, amounts, last_pct


def run_reversal_turnover_screen(codes: list[str], as_of: str | None = None,
                                 fetch: bool = True,
                                 top_k: int = DEFAULT_TOP_K) -> dict:
    """扫描 codes,算反转/低换手因子,跑复合选股,落 view「反转低换手组合」。返回 summary。

    fetch=True:缺 K 线自动采集补齐;False:只读本地缓存(离线复算,不触网)。
    历史不足 / 无 kline 的票不参与(记「跳过数」),诚实降级。
    """
    if as_of:
        store.set_active_date(as_of)

    def loader(code: str):
        s = _load_series(code)
        if s is None and fetch:
            try:
                kdf = market.fetch_kline([code]).get(code)
            except Exception:                          # noqa: BLE001
                kdf = None
            if kdf is not None and len(kdf) and "close" in kdf.columns:
                closes = kdf["close"].astype(float).tolist()
                turnovers = kdf["turnover"].tolist() if "turnover" in kdf.columns else []
                amounts = kdf["amount"].tolist() if "amount" in kdf.columns else []
                last_pct = None
                if "pct_chg" in kdf.columns and len(kdf):
                    v = kdf["pct_chg"].iloc[-1]
                    last_pct = float(v) if pd.notna(v) else None
                s = (closes, turnovers, amounts, last_pct)
        return s

    need = min_history()
    records: dict[str, dict] = {}
    scanned = 0
    skip_pre: dict[str, int] = {}

    def _skip(reason: str):
        skip_pre[reason] = skip_pre.get(reason, 0) + 1

    for code in codes:
        s = loader(code)
        if s is None:
            _skip("无K线")
            continue
        closes, turnovers, amounts, last_pct = s
        if len(closes) < need:                         # 次新 / 历史不足
            _skip("历史不足(含次新)")
            continue
        rev = reversal_factor(closes, n=REV_N)
        turn = low_turnover_factor(turnovers, n=TURN_N)
        amt = avg_amount_wan(amounts, n=TURN_N)
        if rev is None:
            _skip("反转因子缺失")
            continue
        if turn is None:
            _skip("换手因子缺失(近端NaN过多)")
            continue
        scanned += 1
        records[code] = {
            "meta": {"code": code, "n_bars": len(closes)},
            "反转低换手": {"rev": rev, "turn": turn, "amount_wan": amt},
            "snapshot": {"pct_chg": last_pct, "close": closes[-1]},
        }

    out = combo_reversal_turnover_screen(records, top_k=top_k)

    # 合并管线侧跳过 + 策略侧跳过,供人读
    skip_all = dict(skip_pre)
    for k, v in (out.get("跳过") or {}).items():
        skip_all[k] = skip_all.get(k, 0) + v

    selected = [d for d in (out.get("因子明细") or []) if d["code"] in set(out.get("codes") or [])]
    view = {
        "as_of": as_of,
        "策略": "反转低换手组合(策略10·前向观测中)",
        "口径": ("纯量价复合:反转 rev%d + 低换手 turn%d,各自 winsorize+zscore 后等权"
                 "(%.2f/%.2f)加权取 Top%d;近端 turnover 缺失按窗口有效值均值兜底。"
                 % (REV_N, TURN_N, out.get("权重", {}).get("反转", 0.5),
                    out.get("权重", {}).get("低换手", 0.5), top_k)),
        "扫描数": len(codes), "有效样本": out.get("有效样本", scanned),
        "跳过": skip_all,
        "入选数": len(out.get("codes") or []),
        "top_k": top_k,
        "入选清单": selected,
        "权重": out.get("权重"),
        "参数": out.get("参数"),
        "复用": "tools.strategy.reversal_turnover.combo_reversal_turnover_screen",
        "防未来函数": (f"因子只读序列尾部(反转最后{REV_N + 1}根/换手最后{TURN_N}根);"
                     f"尾部即当日;历史<{need}跳过"),
        "命名": "策略10(前向观测中,非已验证可用);诚实边界:可交易池+5-10日+TopK≤20,net绝对水平存幸存者水分,以前向观测为准",
    }
    if out.get("note"):
        view["note"] = out["note"]
    p = store.put_view("反转低换手组合", view)
    logger.info("反转低换手组合:扫描 %d / 有效 %d / 入选 %d → %s",
                len(codes), view["有效样本"], view["入选数"], p)
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
    ap = argparse.ArgumentParser(description="反转低换手组合(候选) 全A扫描")
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
        codes = _offline_universe_codes(limit=a.universe)
    else:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    logger.info("反转低换手组合 扫描:%d 只(日期 %s,fetch=%s,top_k=%d)",
                len(codes), as_of, not a.no_fetch, a.top_k)
    v = run_reversal_turnover_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:入选 %d / 有效 %d", v["入选数"], v["有效样本"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
