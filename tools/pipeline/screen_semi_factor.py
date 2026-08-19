"""策略 5「半导体多因子」全 A 规则型 Screener(限申万二级 801081 半导体池)。

复用 `tools.strategy.semi_factor.combo_semi_factor_screen`(策略E)——
3 因子 winsor+zscore 加权(rd/rev × 0.6 + rd/mcap × 0.2 + 营收增速 × 0.2)。

数据依赖:
  · **市值**:`collectors.fundamental.load_fundamental(code)["总市值"]`(亿元,百度估值)
  · **研发费用率 + 营收增速**:`analysis.financial.analyzer.build_financial_block(code).derived`
    (M2 财报块产出,读三大表 + metrics.compute_derived)
  · **snapshot(pct_chg)**:走 technical.compute(kdf)["last"]["pct_chg"] 或跳过涨跌停过滤

设计权衡(为什么可以低成本跑全 A 半导体池):
  · 池只有 178 只,远小于全 A 5000+,财报三大表串行采可接受
  · fetch=True 时逐票 skip-if-cached 补 fundamental + financial;fetch=False 只读缓存
  · 缺 fundamental 或 financial.derived 的票 → 剔(策略E 内部逻辑),诚实降级

防未来函数:financial.derived 与 fundamental 均基于**已披露**报告期 + 当日估值。
"""
from __future__ import annotations

import json
import logging

import pandas as pd

from tools.analysis import technical as ta
from tools.analysis.financial import analyzer as fr_analyzer
from tools.collectors import fundamental as fd
from tools.collectors import market
from tools.config import settings
from tools.store import repo as store
from tools.strategy.semi_factor import combo_semi_factor_screen

logger = logging.getLogger("pipeline.screen_semi_factor")

DEFAULT_TOP_K = 8                                        # 原脚本 g.stocknum=8
_UNIVERSE_PATH = settings.PROJECT_ROOT / "config" / "semi_universe.json"


def _load_semi_universe() -> set[str]:
    """半导体池 178 只(申万二级 801081);缺文件 → 空 set(降级为全 A 通过)。"""
    try:
        codes = json.loads(_UNIVERSE_PATH.read_text("utf-8"))
        return set(codes) if isinstance(codes, list) else set()
    except FileNotFoundError:
        logger.warning("半导体池缺失(%s),不限池;运行 tools/collectors/semi_universe.py 刷新", _UNIVERSE_PATH)
        return set()


def _build_record(code: str, as_of: str, fetch: bool) -> dict | None:
    """按策略E 需要拼装最小 record:snapshot(pct_chg) + valuation(mktcap_yi) + financial.derived。

    fetch=True:缺 fundamental/financial 就补采;fetch=False 只读缓存,缺就跳过。
    任一因子缺失 → 返回 None(策略E 自会跳过,这里返回 None 让主流程记跳过数)。
    """
    # 1) fundamental(总市值)
    try:
        fund = fd.load_fundamental(code)
    except FileNotFoundError:
        fund = None
    if fund is None and fetch:
        try:
            fund = (fd.fetch_fundamental([code]) or {}).get(code)
        except Exception:                                # noqa: BLE001
            fund = None
    if not fund or not isinstance(fund.get("总市值"), (int, float)) or fund["总市值"] <= 0:
        return None
    mktcap_yi = float(fund["总市值"])

    # 2) financial 块(derived + 利润表摘要)
    try:
        fin_block = fr_analyzer.build_financial_block(code, as_of=as_of)
    except Exception:                                    # noqa: BLE001
        fin_block = None
    if not fin_block:
        return None
    derived = fin_block.get("derived") or {}
    if not isinstance(derived.get("研发费用率"), (int, float)) or derived["研发费用率"] <= 0:
        return None
    if not isinstance(derived.get("营收增速"), (int, float)):
        return None
    profit_summary = fin_block.get("利润表摘要") or {}
    if not isinstance(profit_summary.get("营业总收入"), (int, float)):
        return None

    # 3) snapshot(pct_chg)——用 kline 最后一根,缺则填 0(策略E 触板过滤会仍旧生效)
    pct_chg = 0.0
    try:
        kdf = market.load_kline_recent(code)
        if kdf is not None and len(kdf) and "pct_chg" in kdf.columns:
            pct_chg = float(kdf["pct_chg"].iloc[-1])
        elif kdf is not None and len(kdf):
            tech = ta.compute(kdf)
            pct_chg = float((tech.get("last") or {}).get("pct_chg") or 0.0)
    except FileNotFoundError:
        return None
    except Exception:                                    # noqa: BLE001
        pct_chg = 0.0

    return {
        "meta": {"code": code},
        "snapshot": {"pct_chg": pct_chg},
        "valuation": {"mktcap_yi": mktcap_yi},
        "financial": {"derived": derived, "利润表摘要": profit_summary},
    }


def run_semi_factor_screen(codes: list[str], as_of: str | None = None,
                           fetch: bool = True, top_k: int = DEFAULT_TOP_K) -> dict:
    """扫 codes ∩ 半导体池 → 补数据 → 调策略E → 落 view「半导体多因子」。

    codes:通常传全 A 全量(与 screen_momentum 等同款),内部与 semi_universe.json 求交。
    fetch=True:缺 fundamental/financial 补采;False 只读缓存(离线复算,不触网)。
    top_k 默认 8(原脚本 g.stocknum=8)。
    """
    if as_of:
        store.set_active_date(as_of)

    universe = _load_semi_universe()
    if universe:
        scoped = [c for c in codes if c in universe]
    else:
        scoped = list(codes)

    logger.info("半导体多因子:扫描 %d 只(全A ∩ 半导体池 %d)", len(scoped), len(universe))

    records: dict[str, dict] = {}
    skipped = 0
    for code in scoped:
        rec = _build_record(code, as_of=as_of or pd.Timestamp.today().strftime("%Y-%m-%d"),
                            fetch=fetch)
        if rec is None:
            skipped += 1
            continue
        records[code] = rec

    logger.info("半导体多因子:有效样本 %d / 跳过(缺数据)%d", len(records), skipped)

    result = combo_semi_factor_screen(records, top_k=top_k)

    selected: list[dict] = []
    for d in result.get("因子明细", [])[:top_k]:
        code = d["code"]
        selected.append({
            "code": code,
            "行业": "半导体(申万二级 801081)",
            "组合": ["半导体多因子"],
            "明细": {
                "综合分": d["综合分"],
                "rd_rev": d["rd_rev"], "rd_mcap": d["rd_mcap"], "rev_yoy": d["rev_yoy"],
                "rd_rev_z": d["rd_rev_z"], "rd_mcap_z": d["rd_mcap_z"], "rev_yoy_z": d["rev_yoy_z"],
            },
        })

    view = {
        "as_of": as_of,
        "策略": "半导体多因子(策略5·限申万二级 801081 池)",
        "口径": ("限申万二级 801081 半导体池 178 只;3 因子 winsor+zscore 加权 "
                 "(研发/营收 × 0.6 + 研发/市值 × 0.2 + 营收增速 × 0.2);"
                 "缺 financial.derived(研发费用率/营收增速)或 fundamental.总市值 → 剔。"),
        "扫描数": len(scoped),
        "universe_size": len(universe),
        "有效样本": len(records),
        "跳过数(缺数据)": skipped,
        "入选数": len(selected),
        "top_k": top_k,
        "权重": result.get("权重"),
        "入选清单": selected,
        "复用": "tools.strategy.semi_factor.combo_semi_factor_screen(records)",
        "防未来函数": "financial.derived / fundamental 均基于已披露报告期 + 当日估值",
    }
    p = store.put_view("半导体多因子", view)
    logger.info("半导体多因子:扫描 %d / 有效 %d / 跳过 %d / 入选 %d → %s",
                len(scoped), len(records), skipped, len(selected), p)
    return view


def _main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 5 半导体多因子 全A扫描(限半导体池)")
    ap.add_argument("--codes", help="逗号分隔的指定代码(优先于全A)")
    ap.add_argument("--date", help="运行日期 YYYY-MM-DD(默认今天)")
    ap.add_argument("--no-fetch", action="store_true", help="只读本地缓存,不触网")
    ap.add_argument("--top-k", type=int, default=DEFAULT_TOP_K,
                    help=f"取前 K 只(默认 {DEFAULT_TOP_K},原脚本 g.stocknum=8)")
    a = ap.parse_args(argv)

    as_of = a.date or pd.Timestamp.today().strftime("%Y-%m-%d")
    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    else:
        from tools.collectors import universe
        codes = universe.universe_codes()
    v = run_semi_factor_screen(codes, as_of=as_of, fetch=not a.no_fetch, top_k=a.top_k)
    logger.info("完成:入选 %d / 有效 %d / 跳过 %d",
                v["入选数"], v["有效样本"], v["跳过数(缺数据)"])
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
