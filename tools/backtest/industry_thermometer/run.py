"""行业冷热钟摆表·数值4维回测 —— 口径A(native 申万指数,~1.5yr)端到端。

用法(worktree 内, 数据在主仓, 只读复用):
    cd /Users/yqg/Documents/projects/worktrees/stock_analysis/regime-thermometer
    PYTHONPATH=. /Users/yqg/.conda/envs/stock_analysis/bin/python \
        -m tools.backtest.industry_thermometer.run \
        --out data/backtest_local/industry_thermometer/native.json --start 2024-07-01

产物:结果 JSON(--out) + 同名 .md 报告。逐维度对前瞻行业指数收益的预测力 verdict。
防未来:因果分位/T+1入场/子样本符号;功效纪律:短样本不显著=「欠功效」非「证伪」。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from tools.analysis import industry_map
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PIPE
from tools.backtest.industry_thermometer import dims as DIMS
from tools.backtest.industry_thermometer import evaluate as EV

logger = logging.getLogger("industry_thermometer.run")


def _bind(data_root: str | None) -> None:
    if data_root:
        from tools.store import repo
        root = Path(data_root)
        repo._RAW_DIR = root / "raw"
        repo._MASTER_DIR = root / "master"
        repo._ANALYSIS_DIR = root / "analysis"
    else:
        D.bind_main_repo()
    D.clear_caches()


def _membership() -> dict:
    from tools.collectors import code_industry
    snap = code_industry.load()
    return {c: industry_map.to_sw(r) for c, r in snap.items()
            if r and industry_map.to_sw(r)}


def run(*, start: str, end: str | None, horizons: tuple[int, ...],
        data_root: str | None) -> dict:
    _bind(data_root)
    membership = _membership()
    dates = PIPE.trading_calendar(start=start, end=end)
    logger.info("口径A native:%d 交易日 %s..%s,成分票 %d",
                len(dates), dates[0] if dates else "-", dates[-1] if dates else "-",
                len(membership))

    dim_panel = DIMS.build_dim_panel(membership, dates)
    industries = sorted(dim_panel["industry"].unique()) if not dim_panel.empty else []
    ret = PIPE.industry_forward_returns(industries, horizons)
    ev = EV.evaluate_all(dim_panel, ret, horizons)

    # 精简 meta(不落大 DataFrame)
    valued = dim_panel.dropna(subset=["value"])
    meta = {
        "口径": "A·native申万指数(~1.5yr,快照成分弱前视)",
        "warmup_start": start, "end": end,
        "horizons": list(horizons),
        "n_industries": len(industries),
        "n_dates": len(dates),
        "date_range": [dates[0], dates[-1]] if dates else [None, None],
        "n_valued_rows": int(len(valued)),
        "样本告知": "native 申万指数史仅~1.5yr → 日频检验天然欠功效;verdict 按功效纪律,"
                    "短样本不显著记『欠功效待复查』,不写『证伪』。真 null 需口径B(2018+)或补长史。",
    }
    return {"meta": meta, **ev}


def _report_md(res: dict) -> str:
    m = res["meta"]
    lines = [
        "# 行业冷热钟摆表·数值4维 回测报告(口径A native)",
        "",
        f"- 口径:{m['口径']}",
        f"- 区间:{m['date_range'][0]} .. {m['date_range'][1]}(交易日 {m['n_dates']})",
        f"- 行业数:{m['n_industries']} ｜ horizons:{m['horizons']} ｜ 有值行:{m['n_valued_rows']}",
        f"- ⚠️ {m['样本告知']}",
        "",
        "## 逐维度 verdict",
        "",
        "| 维度 | best IC | naive_t(⚠️重叠虚高) | p_boot(重叠校正·判定用) | 方向 | verdict |",
        "|---|---|---|---|---|---|",
    ]
    for dim, r in res["results"].items():
        ics = r["ic"]
        best_h = max(ics, key=lambda h: abs(ics[h].get("mean_ic") or 0.0))
        b = ics[best_h]
        best_ic = b.get("mean_ic")
        nt = b.get("naive_t")
        pb = b.get("p_boot")
        v = r["verdict"]
        ic_str = "—" if best_ic is None else f"{best_ic:+.4f}(h{best_h})"
        nt_str = "—" if nt is None else f"{nt:+.2f}"
        pb_str = "—" if pb is None else f"{pb:.3f}"
        lines.append(f"| {dim} | {ic_str} | {nt_str} | {pb_str} | {v.get('方向') or '—'} | **{v['判定']}** |")
    lines += [
        "",
        f"- 多重检验:{res['multiple_testing']['提示']}(族 {res['multiple_testing']['族大小']},"
        f"Bonferroni α={res['multiple_testing']['Bonferroni_alpha']})",
        "- **判定用 p_boot(连续IC块自助,块长=h,重叠校正),不用 naive_t**——naive_t 对重叠区间虚高,"
        "正是旧 IET 被 t=1.78 误杀的坑。",
        "- 方向读法:IC<0=过冷跑赢/过热跑输(Marks 逆向一致);IC>0=动量延续。",
        "- verdict 读法:有预测力/欠功效待复查(短样本,非证伪)/不可用·真null(样本充足且点估≈0)。",
    ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="行业冷热钟摆表·数值4维回测(口径A native)")
    ap.add_argument("--out", required=True, help="结果 JSON 路径(同名 .md 报告)")
    ap.add_argument("--start", default=PIPE.WARMUP_START, help="分位预热起点(默认 2024-07-01)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--horizons", default="5,10,20")
    ap.add_argument("--data-root", default=None, help="覆盖数据根(默认绑定主仓只读)")
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(","))
    res = run(start=args.start, end=args.end, horizons=horizons, data_root=args.data_root)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=str)
    md = out.with_suffix(".md")
    md.write_text(_report_md(res), encoding="utf-8")
    logger.info("写出 %s + %s", out, md)
    print(_report_md(res))


if __name__ == "__main__":
    main()
