"""IET 探针复现 CLI:一键重算行业层 IC / 分解 / 敏感性 / 近端 / PIT 子样本。

用法:  python -m tools.backtest.iet_probe.probe_cli [--out results.json]
读主仓 data(bind_main_repo,只读),面板不落盘(内存)。产物 JSON 供报告核对。
⚠️ 成分源=当前快照(弱前视,见报告);PIT 子样本另路防前视交叉验证。
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np

from tools.analysis import industry_map
from tools.analysis.industry_temp import temperature as TEMP
from tools.backtest.iet_probe import data as D
from tools.backtest.iet_probe import pipeline as PL
from tools.backtest.iet_probe import run_probe as R

H = (1, 5, 20)
NEAR_CUTOFF = "2026-05-01"


def _summ(tbl):
    return {h: {"mean_ic": tbl[h].get("mean_ic"), "icir": tbl[h].get("icir"),
                "t": tbl[h].get("t_stat"), "n_days": tbl[h].get("n_days"),
                "pos_ratio": tbl[h].get("pos_ratio")} for h in H}


def _ic(temp, col, ret, date_min=None):
    f = temp[["date", "industry", col]].rename(columns={col: "k"}).dropna(subset=["k"])
    frame = R.build_eval_frame(f, ret, H)
    if date_min:
        frame = frame[frame["date"] >= date_min]
    return R.rank_ic_table(frame, H), frame


def _add_derived(temp):
    temp = temp.copy()
    temp["k_nonmom"] = np.where(
        temp[["val_ab", "turn_ab"]].isna().any(axis=1), np.nan,
        (temp["val_ab"] == "A").astype(float) + (temp["turn_ab"] == "A").astype(float))
    temp["mom_raw"] = temp.apply(lambda r: D.rs_momentum_of(r["industry"], r["date"]), axis=1)
    return temp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/iet_probe_results.json")
    ap.add_argument("--start", default=PL.WARMUP_START)
    args = ap.parse_args()

    t0 = time.time()
    D.bind_main_repo()
    snap = __import__("tools.collectors.code_industry", fromlist=["load"]).load()
    membership = {c: industry_map.to_sw(r) for c, r in snap.items()
                  if r and industry_map.to_sw(r)}
    cal = PL.trading_calendar(start=args.start)
    res = {"meta": {"snapshot_codes": len(membership), "calendar_days": len(cal),
                    "gate": {"IC_BAR": R.IC_BAR, "T_BAR": R.T_BAR}, "horizons": H}}

    # 快照面板 + 温度
    panel = PL.build_panel_snapshot(membership, cal)
    temp = _add_derived(TEMP.build_temperature_series(panel, D.rs_momentum_of))
    industries = sorted(temp["industry"].unique())
    ret = PL.industry_forward_returns(industries, H)

    # 全样本分解
    res["full_sample"] = {}
    for name, col in [("k_full", "k"), ("k_nonmom", "k_nonmom"), ("mom_raw", "mom_raw")]:
        tbl, _ = _ic(temp, col, ret)
        res["full_sample"][name] = _summ(tbl)
    tbl_k, frame_k = _ic(temp, "k", ret)
    res["layers_kfull"] = {h: R.layer_stats(frame_k, h) for h in H}
    res["subsample_year"] = {h: R.subsample_sign_stability(frame_k, h, by="year") for h in H}
    res["verdict_kfull"] = R.gate_verdict(tbl_k, res["subsample_year"][20])

    # 近端短窗
    res["near_term"] = {"cutoff": NEAR_CUTOFF, "ic": {}}
    for name, col in [("k_full", "k"), ("k_nonmom", "k_nonmom"), ("mom_raw", "mom_raw")]:
        tbl, _ = _ic(temp, col, ret, date_min=NEAR_CUTOFF)
        res["near_term"]["ic"][name] = _summ(tbl)

    # 敏感性
    res["sensitivity"] = {}
    for win in (120, 250):
        for cut in (0.5, 0.6, 0.7):
            tp = TEMP.build_temperature_series(panel, D.rs_momentum_of,
                                               win=win, val_cut=cut, turn_cut=cut)
            tbl, _ = _ic(tp, "k", ret)
            res["sensitivity"][f"win{win}_cut{cut}"] = {
                h: {"ic": tbl[h].get("mean_ic"), "t": tbl[h].get("t_stat")} for h in H}

    # PIT 子样本(防前视交叉验证)
    panel_pit = PL.build_pit_subsample_panel(D.universe_from_master(), cal)
    if len(panel_pit):
        tpit = _add_derived(TEMP.build_temperature_series(panel_pit, D.rs_momentum_of))
        ret_pit = PL.industry_forward_returns(sorted(tpit["industry"].unique()), H)
        res["pit_subsample"] = {}
        for name, col in [("k_full", "k"), ("k_nonmom", "k_nonmom")]:
            tbl, _ = _ic(tpit, col, ret_pit)
            res["pit_subsample"][name] = _summ(tbl)

    json.dump(res, open(args.out, "w"), ensure_ascii=False, indent=2, default=str)
    print(f"[IET探针] 完成 {time.time()-t0:.0f}s → {args.out}")
    v = res["verdict_kfull"]
    print("闸门(k_full):", v["判定"], "达标=", v["达标"])
    print("k_nonmom h20:", res["full_sample"]["k_nonmom"][20])


if __name__ == "__main__":
    main()
