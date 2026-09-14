"""对 24 组合结果套用**结果前预注册**的决策规则(设计文档 §4bis),产出判决与 2/3 计票。

不含任何在看到结果后新增的判据;规则字面照抄设计文档。⚠️ 研究模拟,非投资建议。
"""
from __future__ import annotations
import argparse, json


def sig_pos(mean, p):
    """day 级显著为正 ⟺ 均值>0 且 bootstrap 双侧 p<0.05(95%CI 不跨 0)。"""
    return mean is not None and p is not None and mean > 0 and p < 0.05


def evaluate(results: dict) -> dict:
    ec = results["event_counts"]
    H1, H2 = results["H1"], results["H2"]
    keys = list(H1.keys())
    out = {"n_combos": len(keys), "H1_beta": [], "H1_alpha": [], "H2": [], "small_sample": []}
    for k in keys:
        n_cap = ec[k]["capitulation_oos"]
        if n_cap < 20:
            out["small_sample"].append((k, n_cap))
        cap5 = (H1[k]["capitulation"].get("5") or {})
        ord5 = (H1[k]["ordinary_down"].get("5") or {})
        # H1-β:cap 绝对收益 > 普通普跌绝对收益
        cb, ob = cap5.get("abs_ret_mean"), ord5.get("abs_ret_mean")
        out["H1_beta"].append((k, n_cap, cb, ob, (cb is not None and ob is not None and cb > ob)))
        # H1-α:cap day 级 α5 显著为正
        out["H1_alpha"].append((k, n_cap, cap5.get("day_alpha_mean"), cap5.get("alpha_boot_p"),
                                sig_pos(cap5.get("day_alpha_mean"), cap5.get("alpha_boot_p"))))
        # H2:G_volup_close vs G_MA5 四条件
        g = H2[k]["gates"]; v, m = g["G_volup_close"], g["G_MA5"]
        a = (v["alpha_5_day_mean"] is not None and m["alpha_5_day_mean"] is not None
             and v["alpha_5_day_mean"] > m["alpha_5_day_mean"])
        b = sig_pos(v["alpha_5_day_mean"], v["alpha_5_day_p"])
        c = (v["precision_5"] is not None and m["precision_5"] is not None
             and v["precision_5"] >= m["precision_5"])
        d = (v["seg_capture_mean"] is not None and m["seg_capture_mean"] is not None
             and v["seg_capture_mean"] < m["seg_capture_mean"])
        out["H2"].append((k, n_cap, v["alpha_5_day_mean"], m["alpha_5_day_mean"],
                          v["precision_5"], m["precision_5"], v["seg_capture_mean"],
                          m["seg_capture_mean"], a, b, c, d, (a and b and c and d)))
    # 计票
    def tally(rows, idx):
        return sum(1 for r in rows if r[idx]), len(rows)
    out["vote"] = {
        "H1_beta": tally(out["H1_beta"], 4),
        "H1_alpha": tally(out["H1_alpha"], 4),
        "H2": tally(out["H2"], 12),
    }
    thr = (2 * len(keys) + 2) // 3   # ceil(2/3 * n)
    out["threshold_2of3"] = thr
    out["verdict"] = {
        "H1_beta": "通过" if out["vote"]["H1_beta"][0] >= thr else "否定/证不了",
        "H1_alpha": "通过" if out["vote"]["H1_alpha"][0] >= thr else "证不了",
        "H2": "通过" if out["vote"]["H2"][0] >= thr else "否定/证不了",
    }
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--results", required=True)
    a = ap.parse_args()
    res = json.load(open(a.results))
    ev = evaluate(res)
    thr = ev["threshold_2of3"]
    print(f"n_combos={ev['n_combos']} · 2/3阈值={thr} · 小样本(<20)组合={ev['small_sample']}\n")
    print("=== 判决(预注册规则) ===")
    for h in ["H1_beta", "H1_alpha", "H2"]:
        hit, n = ev["vote"][h]
        print(f"  {h}: {hit}/{n} 组合成立 → {ev['verdict'][h]}")
    print("\n=== H2 逐组合(G_volup_close vs G_MA5) ===")
    print(f"{'key':22}{'nc':>4}{'v_a5d':>7}{'m_a5d':>7}{'v_p5':>6}{'m_p5':>6}{'v_seg':>6}{'m_seg':>6} pass")
    for r in ev["H2"]:
        print(f"{r[0]:22}{r[1]:>4}{str(r[2]):>7}{str(r[3]):>7}{str(r[4]):>6}{str(r[5]):>6}"
              f"{str(r[6]):>6}{str(r[7]):>6}  {'Y' if r[12] else '.'}")
    print("\n=== H1-β / H1-α 逐组合 ===")
    print(f"{'key':22}{'nc':>4}{'cap_abs5':>9}{'ord_abs5':>9}{'βok':>4}{'cap_a5d':>8}{'a5_p':>7}{'αok':>4}")
    for rb, ra in zip(ev["H1_beta"], ev["H1_alpha"]):
        print(f"{rb[0]:22}{rb[1]:>4}{str(rb[2]):>9}{str(rb[3]):>9}{'Y' if rb[4] else '.':>4}"
              f"{str(ra[2]):>8}{str(ra[3]):>7}{'Y' if ra[4] else '.':>4}")


if __name__ == "__main__":
    main()
