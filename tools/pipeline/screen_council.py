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

from tools.analysis import council, risk_veto, technical
from tools.analysis.financial import flags as fin_flags
from tools.collectors import board, market
from tools.config.strategy import risk_veto_adjust
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
    # 全A票池 = 主档代码 ∪ 所有 raw 日期分区的 kline(总是并集;此前用 if-not-codes
    # 只在主档为空时才扫 raw → 主档有少量自选票时把全A缩到那几十只,是坑,已改为恒并)。
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


def _load_or_fetch_kline(code: str, fetch: bool):
    try:
        return market.load_kline_recent(code)
    except FileNotFoundError:
        if not fetch:
            return None
        try:
            return market.fetch_kline([code]).get(code)
        except Exception:                              # noqa: BLE001
            return None


def build_min_record(code: str, kdf: pd.DataFrame, as_of: str | None = None) -> dict | None:
    """组装单票最小中心记录:{meta:{code,行业}, signals, financial?, 其余字段 None}。

    signals 由 technical.compute 现算(不改算法);行业取本地板块归属(缺则 None)。
    K 线不足 / 无技术信号 → 返回 None(该票跳过,不入选)。

    财报块(全A 覆盖):若该票已缓存 as_of 可见的财报三大表 raw,则挂 `financial` 轻量块
    (analysis.financial.build_financial_block,披露日锚定、防未来函数),使 **财报质地专家**
    不再弃权、红旗判定在全A排序生效;无缓存 → None(优雅降级,专家自然弃权,行为同旧)。
    注:**不往 meta 塞 as_of**——事件驱动专家以 meta.as_of 为发声闸,塞了会让它对有事件缓存的
    票改判(超出本次"财报数据供给"范围);财报专家只读 record['financial'],无需 meta.as_of。
    """
    tech = technical.compute(kdf)
    if not isinstance(tech, dict) or "signal" not in tech:
        return None
    industry = None
    try:
        industry = board.board_of(code)               # 本地缓存映射,缺失 → None(不触网)
    except Exception:                                  # noqa: BLE001
        industry = None
    financial = None
    try:
        from tools.analysis.financial import analyzer as fr_analyzer
        financial = fr_analyzer.build_financial_block(code, as_of=as_of, industry=industry)
    except Exception:                                  # noqa: BLE001
        financial = None                               # 缺财报 raw / 分析失败 → 弃权(不炸)
    return {
        "meta": {"code": code, "industry": industry},   # 无 as_of → 事件驱动专家天然弃权
        "snapshot": None,
        "valuation": None,
        "fundamental": None,
        "financial": financial,                          # 财报质地块(缺 → None,财报专家弃权)
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
                       fetch: bool = True, top_n: int = TOP_N,
                       persist: bool = True) -> dict:
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
        rec = _safe(lambda: build_min_record(code, kdf, as_of=as_of))
        if rec is None:
            skipped += 1
            continue
        scanned += 1
        cblk = _safe(lambda: council.build_council_block(rec, kdf))
        if not cblk or not isinstance(cblk.get("default"), dict):
            continue
        d = cblk["default"]
        # 统一风控 veto 汇聚(WI-6 Phase 3):财报高危红旗 + 龙虎榜否决 正交 OR 合成 → 同一排序出口。
        #   · 财报轴 dose = 高危红旗数(缺财报块/无高危 → 0);
        #   · 龙虎榜轴 = as-of 入选否决裁决(缺快照/未触发 → None,该轴不发声)。
        # 无龙虎榜数据时退化为红旗现状(向后兼容)。防未来函数:红旗基于已披露财报(analyzer 控 as_of),
        # 龙虎榜走 lhb_asof(list_date < as_of 严格,盘后披露当天不可用)。
        dose = fin_flags.high_flag_count((rec.get("financial") or None))
        lhbv = risk_veto.lhb_verdict_asof(code, as_of)
        # 弃权置信度软收缩:发声专家过少的票综合分向中性收缩后再进排序(收缩启用=False → ==综合分,
        # 排序回原口径)。收缩只降低"少数纯技术专家发声"票的排序竞争力,不剔除、保留展示(下方带标注)。
        base_score = d.get("综合分_收缩", d.get("综合分", 0.0))
        adj = risk_veto_adjust(base_score, dose, lhbv)
        _lhb_hit = bool((adj.get("各轴") or {}).get("龙虎榜", {}).get("应用"))
        scored.append({
            "code": code,
            "行业": (rec.get("meta") or {}).get("industry"),
            "综合方向": d.get("综合方向"),
            "综合分": d.get("综合分", 0.0),
            # 弃权置信度标注(合议级;标注关 → 缺键,下游 .get 兜底):透出参与度与合议置信度供分析师辨识
            # "纯技术极值"vs"多口径一致",并让软收缩后的综合分驱动排序(收缩关时 综合分_收缩==综合分)。
            "合议置信度": d.get("合议置信度"),
            "参与专家数": d.get("参与专家数"),
            "口径多样性": d.get("口径多样性"),
            "覆盖口径": d.get("覆盖口径"),
            "低合议置信度": d.get("低合议置信度"),
            "综合分_收缩": d.get("综合分_收缩", d.get("综合分", 0.0)),
            "有财报块": rec.get("financial") is not None,  # 是否挂到 as_of 财报块(覆盖统计)
            "排序分": adj["排序分"],                     # 收缩+降权后分(收缩后综合分−Σ罚分;否决靠标记沉底)
            # 风控风险归因(两轴合成;无触发 → None,保持旧展示语义)。键名沿用「财报风险」向后兼容,
            # 增补「归因/各轴」透出龙虎榜轴命中。
            "财报风险": {"高危数": adj["高危数"], "罚分": adj["罚分"],
                        "否决": adj["否决"], "剔除": adj["剔除"],
                        "归因": adj.get("归因"), "各轴": adj.get("各轴"),
                        "flags": (rec.get("financial") or {}).get("flags") if dose else []}
                        if (dose or _lhb_hit) else None,
            "council": cblk,                            # {default, experts, config} 供前端勾选重排
        })

    # 财报红旗接入排序(与 web _rerank_scored 同键):(未剔除, 非否决沉底, 有分, 排序分) 降序;
    # 未启用红旗接入 / 无高危时,排序分==综合分、否决/剔除全 False → 与旧「综合分降序」完全一致(不回归)。
    scored = [x for x in scored if not (x.get("财报风险") or {}).get("剔除")]
    scored.sort(key=lambda x: (
        not (x.get("财报风险") or {}).get("否决"),      # 非否决 > 否决沉底
        x["排序分"] is not None,                         # 有分 > 无分
        x["排序分"] if x["排序分"] is not None else -1e9,
    ), reverse=True)
    top = scored[:top_n]
    命中高危 = sum(1 for x in scored if (x.get("财报风险") or {}).get("高危数"))   # 财报高危红旗票数
    命中龙虎榜 = sum(1 for x in scored                                          # 龙虎榜否决触发票数
                   if ((x.get("财报风险") or {}).get("各轴") or {}).get("龙虎榜", {}).get("应用"))
    带块数 = sum(1 for x in scored if x.get("有财报块"))                # 挂到 as_of 财报块的票数(覆盖代理)
    低置信数 = sum(1 for x in top if x.get("低合议置信度"))            # Top 内低合议置信度(少数/单口径发声)票数

    view = {
        "as_of": as_of,
        "策略": "策略0 · 多专家合议(全A)",
        "扫描数": len(codes),
        "有效": scanned,
        "跳过数(历史不足/无信号)": skipped,
        "财报覆盖": 带块数,                    # 有 as_of 可见财报块参与合议的票数(全A采财报后↑)
        "命中高危红旗": 命中高危,              # 财报高危红旗降权/否决沉底的票数
        "命中龙虎榜否决": 命中龙虎榜,          # 龙虎榜净买上榜否决/降权沉底的票数(风控微结构轴)
        "Top内低合议置信度": 低置信数,         # 少数/单口径专家发声(合议置信度<阈)的 Top 票数;越少越健康
        "top_n": len(top),
        "top": top,
        "口径": ("全A逐票 technical.compute → 合议默认专家组(有财报 raw 的票挂 as_of 财报块,财报质地"
                 "专家发声;资金流/多因子/情绪/事件因无全A数据自然弃权)→ 统一风控 veto 汇聚接入排序"
                 "(财报红旗 + 龙虎榜否决 正交 OR 合成,降权/否决沉底,与 web 同一纯函数)→ Top N;纯数据·非投资建议"),
        "防未来函数": ("只用 load_kline 历史 K 线 + 披露日≤as_of 的已披露财报(analyzer 控 as_of)"
                     "+ 龙虎榜 list_date<as_of 严格(盘后披露当天不可用),不引未来数据"),
    }
    # persist=False:只算不落盘(如前向记分卡复算全票排名时),避免覆盖闭环已落的 top-N view。
    p = store.put_view("策略0合议", view) if persist else "(未落盘·persist=False)"
    logger.info("策略0合议:扫描 %d / 有效 %d / 跳过 %d / 财报覆盖 %d / 命中高危红旗 %d / 命中龙虎榜 %d / Top %d → %s",
                len(codes), scanned, skipped, 带块数, 命中高危, 命中龙虎榜, len(top), p)
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
