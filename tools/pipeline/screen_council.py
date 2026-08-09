"""策略 0「全A · 多专家合议选股」驱动(计算层)。

区别于策略1(规则型 screener)与形态选股:本策略**不新增任何算法**,只把现成的
技术指标(technical.compute)与合议层(council.build_council_block)串起来跑全A——
逐票算 signals → 组装最小中心记录 → 合议默认专家组 → 按综合分降序取 Top N 落 view。

弃权由合议层天然处理(不改算法):
  - 技术趋势 / 超买超卖 / 拐点:读最小记录 signals → 发声。
  - 板块轮动:meta.industry 有 RRG 数据时发声,否则弃权。
  - 资金流 / 情绪三层 / 多因子 / 事件驱动:最小记录无对应数据(fundflow/sentiment/factor
    code_view/as_of 均缺)→ 自然弃权,不入分母、不稀释在场专家(council 置信度加权)。

防未来函数:只用 load_kline 的历史 K 线(technical.compute 取最后一根及之前),不引未来数据。
幂等 + _safe 降级:单票异常跳过不崩;view 恒可产出(空池也落 view)。

数据只读复用 `collectors.market.load_kline`(优先滚动主档、回退当日 raw 分区)。
入口:`python -m tools.pipeline.screen_council [--universe N] [--codes ...] [--date D] [--no-fetch]`。
--no-fetch 只读本地缓存(离线复算,不触网),票池从本地已缓存 K 线自动枚举。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import council, technical
from tools.collectors import board, market
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_council")

# 参与合议所需最少 K 线根数(不足则趋势/均线类信号无意义 → 跳过,不入选)。
MIN_BARS = 60
TOP_N = 20


def _offline_universe_codes(limit: int | None = None) -> list[str]:
    """离线枚举全A票池:从本地已缓存 K 线(滚动主档优先,回退 raw kline 分区)取代码。

    不触网(--no-fetch 场景用)。主档存在 → 用主档代码;否则扫 data/raw 各日期分区 +
    扁平 kline 目录的 *.parquet 文件名(6 位数字)去重。升序返回;limit 截前 N 只。
    """
    codes = set(store.list_master_codes())
    if not codes:
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


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline(code)
    except FileNotFoundError:
        if not fetch:
            return None
        try:
            return market.fetch_kline([code]).get(code)
        except Exception:                              # noqa: BLE001
            return None


def build_min_record(code: str, kdf: pd.DataFrame) -> dict | None:
    """组装单票最小中心记录:{meta:{code,行业}, signals, 其余字段 None}。

    signals 由 technical.compute 现算(不改算法);行业取本地板块归属(缺则 None)。
    K 线不足 / 无技术信号 → 返回 None(该票跳过,不入选)。
    """
    tech = technical.compute(kdf)
    if not isinstance(tech, dict) or "signal" not in tech:
        return None
    industry = None
    try:
        industry = board.board_of(code)               # 本地缓存映射,缺失 → None(不触网)
    except Exception:                                  # noqa: BLE001
        industry = None
    return {
        "meta": {"code": code, "industry": industry},   # 无 as_of → 事件驱动专家天然弃权
        "snapshot": None,
        "valuation": None,
        "fundamental": None,
        "signals": {"trend": tech["signal"], "reversal": tech["reversal"], "ob_os": tech["ob_os"]},
        "prediction": None,
        "sentiment": None,
        "fundflow": None,
        "events": None,
    }


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:                                  # noqa: BLE001
        return default


def run_council_screen(codes: list[str], as_of: str | None = None,
                       fetch: bool = True, top_n: int = TOP_N) -> dict:
    """扫描 codes,逐票合议默认专家组,按综合分降序落 view「策略0合议」。返回 view。

    fetch=True:缺 K 线自动采集;False:只读本地缓存(离线复算,不触网)。
    历史不足(<MIN_BARS)/ 无技术信号的票记入「跳过数」,不入选。空池仍产出 view(top=[])。
    """
    if as_of:
        store.set_active_date(as_of)

    scored: list[dict] = []
    scanned = skipped = 0
    for code in codes:
        kdf = _load_or_fetch_kline(code, fetch)
        if kdf is None or len(kdf) < MIN_BARS:
            skipped += 1
            continue
        rec = _safe(lambda: build_min_record(code, kdf))
        if rec is None:
            skipped += 1
            continue
        scanned += 1
        cblk = _safe(lambda: council.build_council_block(rec, kdf))
        if not cblk or not isinstance(cblk.get("default"), dict):
            continue
        d = cblk["default"]
        scored.append({
            "code": code,
            "行业": (rec.get("meta") or {}).get("industry"),
            "综合方向": d.get("综合方向"),
            "综合分": d.get("综合分", 0.0),
            "council": cblk,                            # {default, experts, config} 供前端勾选重排
        })

    # 综合分降序(None 沉底);并列按 code 稳定
    scored.sort(key=lambda x: (x["综合分"] is not None, x["综合分"] if x["综合分"] is not None else -1e9),
                reverse=True)
    top = scored[:top_n]

    view = {
        "as_of": as_of,
        "策略": "策略0 · 多专家合议(全A)",
        "扫描数": len(codes),
        "有效": scanned,
        "跳过数(历史不足/无信号)": skipped,
        "top_n": len(top),
        "top": top,
        "口径": ("全A逐票 technical.compute → 合议默认专家组(资金流/多因子/情绪/事件因无全A数据自然弃权)"
                 " → 按综合分降序 Top N;纯数据·非投资建议"),
        "防未来函数": "只用 load_kline 历史 K 线(compute 取最后一根及之前),不引未来数据",
    }
    p = store.put_view("策略0合议", view)
    logger.info("策略0合议:扫描 %d / 有效 %d / 跳过 %d / Top %d → %s",
                len(codes), scanned, skipped, len(top), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略0 全A 多专家合议选股")
    ap.add_argument("--universe", type=int, metavar="N", help="票池前 N 只(不传=全量)")
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
    logger.info("策略0 扫描:%d 只(日期 %s,fetch=%s)", len(codes), as_of, not a.no_fetch)
    v = run_council_screen(codes, as_of=as_of, fetch=not a.no_fetch)
    logger.info("完成:有效 %d / Top %d", v["有效"], v["top_n"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
