"""财报高危红旗接入选股(降权/否决)—— 跨历史日期效果验证(WI-6 升级)。

问:接入后是否降低"暴雷票(高危红旗)前排/入选",而不过度误伤正常票?
量:对每个分析日,比较**接入前 vs 接入后**:
  · 自选股 / 策略0:高危红旗票的平均排名(名次越大=越靠后越好);
  · 综合选股并集:高危红旗票在列表前 1/3 的占比(越低越好);
  · 误伤:干净票(无高危红旗)的相对顺序是否被打乱(应为 0)。

用法:python -m ops.validate_redflag_veto [--data-root /path/to/repo/with/data]
不改数据、纯读;非投资建议。
"""
from __future__ import annotations

import argparse
import statistics as _stat


def _avg_rank(codes_order: list[str], hits: set[str]) -> float | None:
    """hits 在 codes_order 中的平均 1-based 名次(越大越靠后);无命中→None。"""
    ranks = [i + 1 for i, c in enumerate(codes_order) if c in hits]
    return round(_stat.mean(ranks), 2) if ranks else None


def _clean_order(rows: list[dict]) -> list[str]:
    """干净票(无高危红旗)相对顺序,用于误伤检测。"""
    return [r["code"] for r in rows if not (r.get("risk"))]


def run(data_root: str | None = None) -> int:
    from tools.config import strategy as strategy_cfg
    from tools.store import repo as store
    from web import data_access as da

    if data_root:
        from pathlib import Path
        store._ANALYSIS_DIR = Path(data_root) / "data" / "analysis"   # 指向带历史数据的仓

    dates = store.list_dates("analysis")
    if not dates:
        print("无分析数据,跳过。")
        return 0

    cfg_on = dict((strategy_cfg.redflag_cfg() or {}))
    print(f"红旗接入配置:{cfg_on}\n")
    print(f"{'日期':<12}{'高危票':>6}{'自选前↑名次':>12}{'自选后↓名次':>12}"
          f"{'并集前1/3·前':>12}{'并集前1/3·后':>12}{'误伤(干净票错序)':>16}")

    for d in dates:
        recs = {r["meta"]["code"]: r for r in store.iter_records(date=d)}
        if not recs:
            continue
        hits = {c for c, r in recs.items() if da.financial_risk(r)}
        if not hits:
            continue

        # —— 接入前(禁用)——
        orig = dict(cfg_on); orig["启用"] = False
        strategy_cfg.redflag_cfg = lambda _o=orig: _o                     # type: ignore
        page0 = da.selection_page(d)
        pool0 = [r["code"] for r in page0["rows"]]
        comb0 = [r["code"] for r in page0["combined"]["rows"]]
        clean0 = _clean_order(page0["rows"])

        # —— 接入后(启用,当前 config)——
        strategy_cfg.redflag_cfg = lambda _o=cfg_on: _o                   # type: ignore
        page1 = da.selection_page(d)
        pool1 = [r["code"] for r in page1["rows"]]
        comb1 = [r["code"] for r in page1["combined"]["rows"]]
        clean1 = _clean_order(page1["rows"])

        pr0, pr1 = _avg_rank(pool0, hits), _avg_rank(pool1, hits)
        def _front_third(order):
            k = max(1, len(order) // 3)
            front = set(order[:k])
            return round(len(front & hits) / max(1, len(hits)), 3)
        f0, f1 = _front_third(comb0), _front_third(comb1)
        # 误伤:干净票相对顺序变化数(应为 0)
        mis = sum(1 for a, b in zip(clean0, clean1) if a != b) + abs(len(clean0) - len(clean1))

        print(f"{d:<12}{len(hits):>6}{str(pr0):>12}{str(pr1):>12}"
              f"{f0:>12}{f1:>12}{mis:>16}")

    return 0


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="红旗接入选股效果验证")
    ap.add_argument("--data-root", help="带 data/analysis 历史数据的仓根目录(默认当前仓)")
    a = ap.parse_args(argv)
    return run(a.data_root)


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
