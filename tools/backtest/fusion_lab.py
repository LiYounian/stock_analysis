"""技术合议「融合基线」横截面回测实验室(分析·非投资建议)。

Phase A —— 构建一个横截面 zscore 复合的"技术融合分",并与现状 council 等权/删趋势基线
做同口径对比,给出**预注册达标判据**下的结论(达标才进 Phase B 接入生产)。

融合分设计(见 docs/策略/技术合议融合基线_设计.md):
  每个技术信号先做**逐日横截面 zscore**(去量纲、防单信号尺度主导),再按符号/权重线性复合:
    fuse = Σ_k  sign_k · w_k · z_day(signal_k)
  正权重信号(看多倾斜):
    - 超买超卖(合议好专家,rank-IC 稳定为正 ~+0.040)——signed strength(超卖→+、超买→−)
    - 拐点(温和良性,Top10 超额显著)——strength(仅看多 ≥0)
  反转候选(负权重,需回测达标才引入):
    - 技术趋势(单独 rank-IC 显著为负 −0.046)——**反用**:−z(trend)
  结构化技术状态输入(策略11 的"状态条件指标",用其**连续指标**不用离散排序):
    - bias20(乖离率):越低越超跌 → 均值回归看多 → −z(bias20)
    - boll percent_b(布林位置):越低越贴/破下轨 → −z(percent_b)
    这两个是**稠密**的均值回归结构信号,补齐超买超卖专家"仅约半数日发声"的稀疏缺口。
  动量**不并入**(edge 仅 1 日,并入 5 日会周期错配;动量保持独立)。

权重:先 **等权 zscore 复合(基线)**,再 **按各信号历史 rank-IC 加权**(|IC| 比例,符号取 IC 号);
两版都报。选股 **主榜 Top10 + 精选 Top5** 两档并列。

评测(复用 eval_v3 口径,深历史 + 防未来函数):
  rank-IC / ICIR + Top5/Top10 超额 + **gross/net(扣往返成本 0.1%/0.2%)** + 按日聚类 p,
  并与 **council 等权(V0)/ 删趋势(V2)** 在**同一面板**上对比(必须显示相对增量)。

防未来函数:信号 as-of(sub = df.iloc[:idx+1],只用 ≤idx 的 K 线);前向 1/5 日收益取 idx 之后价;
逐日横截面按真实交易日日期对齐;全宇宙基线 = 当日面板内全票 5 日收益等权(与融合分同口径)。

架构:**面板采一次(贵)→ 配置扫多次(廉)**。collect_panel 落 parquet 缓存;evaluate 从缓存复算。

用法:
  python -m tools.backtest.fusion_lab collect [--sample 800] [--step 15] [--start-idx 250] [--seed 7]
  python -m tools.backtest.fusion_lab eval    [--panel PATH] [--out PATH]
  python -m tools.backtest.fusion_lab run     # collect + eval 一条龙
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random

import numpy as np
import pandas as pd

from tools.analysis import experts, technical as ta
from tools.backtest.eval_v3 import stats as _st
from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.fusion_lab")

_DIR = {"看多": 1, "看空": -1, "中性": 0}
ACTIVE_EXPERTS = ["技术趋势", "超买超卖", "拐点"]
MIN_BARS = 60

PANEL_DEFAULT = "data/analysis/backtest/fusion_panel.parquet"
OUT_DEFAULT = "data/analysis/backtest/fusion_lab.json"

# 往返交易成本(单边合计;A股印花税+佣金+滑点约 0.1~0.2%)。net = gross − COST。
COSTS = {"net_10bp": 0.10, "net_20bp": 0.20}


# ═════════════════════════ ① 采面板(贵,采一次缓存) ═════════════════════════
def _fwd_ret(close: np.ndarray, idx: int, h: int) -> float | None:
    if idx + h >= len(close) or close[idx] <= 0:
        return None
    return float(close[idx + h] / close[idx] - 1.0) * 100.0


def _num(x) -> float:
    """None/NaN → np.nan;否则 float。"""
    if x is None:
        return float("nan")
    try:
        v = float(x)
        return v if np.isfinite(v) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def collect_panel(sample: int, step: int, start_idx: int, seed: int,
                  out: str = PANEL_DEFAULT) -> pd.DataFrame:
    """采横截面面板:每 (票, 信号日) 一行。存

      - 三专家 signed strength / 置信度(供 council 基线复算 + 融合正/反因子)
      - 结构化连续指标 bias20 / percent_b / trend_score / rev_score(策略11 状态指标)
      - 前向 1/5 日收益

    防未来函数:sub = df.iloc[:idx+1];前向取 idx 之后价。落 parquet 缓存。
    """
    codes = sorted(store.list_master_codes())
    rng = random.Random(seed)
    picks = rng.sample(codes, min(sample, len(codes)))
    rows: list[dict] = []
    used = 0
    for c in picks:
        try:
            df = market.load_kline(c).reset_index(drop=True)
        except Exception:                                       # noqa: BLE001
            continue
        if len(df) < start_idx + 10:
            continue
        close = df["close"].to_numpy(float)
        dates = df["date"].astype(str).str.slice(0, 10).tolist()
        used += 1
        for idx in range(start_idx, len(df) - 5, step):
            sub = df.iloc[:idx + 1].reset_index(drop=True)
            if len(sub) < MIN_BARS:
                continue
            f5 = _fwd_ret(close, idx, 5)
            if f5 is None:
                continue
            # technical.compute 只算一次:既拼最小记录(供专家),又取结构化连续指标
            full = ta.compute(sub)
            if not isinstance(full, dict) or "signal" not in full:
                continue
            rec = {"meta": {"code": c, "industry": None}, "snapshot": None,
                   "valuation": None, "fundamental": None,
                   "signals": {"trend": full["signal"], "reversal": full["reversal"],
                               "ob_os": full["ob_os"]},
                   "prediction": None, "sentiment": None, "fundflow": None, "events": None}
            row = {"date": dates[idx], "code": c,
                   "fwd1": _fwd_ret(close, idx, 1), "fwd5": f5}
            # 三专家信封(signed strength + 置信度)
            for name in ACTIVE_EXPERTS:
                v = experts.build(name, rec, sub)
                d = _DIR.get(v.方向, 0)
                s = float(v.强度)
                conf = float(v.置信度)
                if d == 0 or conf <= 0:
                    s = 0.0
                    conf = conf if (v.方向 == "中性" and conf > 0) else 0.0
                row[f"str_{name}"] = s
                row[f"conf_{name}"] = conf
            # 结构化连续指标(策略11 状态指标:用连续值,不用离散排序)
            row["trend_score"] = _num((full.get("signal") or {}).get("得分"))
            row["rev_score"] = _num((full.get("reversal") or {}).get("拐点评分"))
            row["bias20"] = _num((full.get("bias") or {}).get("bias20"))
            row["percent_b"] = _num((full.get("boll") or {}).get("percent_b"))
            rows.append(row)
    pdf = pd.DataFrame(rows)
    logger.info("面板:抽样 %d 票 / 可用 %d / 面板行 %d / 覆盖交易日 %d",
                len(picks), used, len(pdf), pdf["date"].nunique() if len(pdf) else 0)
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        pdf.to_parquet(out, index=False)
        logger.info("面板 → %s", out)
    return pdf


# ═════════════════════════ ② 融合分(逐日横截面 zscore 复合) ═════════════════════════
def add_xs_zscore(pdf: pd.DataFrame, raw_col: str, z_col: str) -> None:
    """逐日横截面 zscore(该列),写回 z_col。std=0 或全 NaN 的日 → z=NaN(不参与排序)。"""
    def _z(g: pd.Series) -> pd.Series:
        v = g.astype(float)
        mu = v.mean(skipna=True)
        sd = v.std(ddof=0, skipna=True)
        if not np.isfinite(sd) or sd == 0:
            return pd.Series(np.nan, index=g.index)
        return (v - mu) / sd
    pdf[z_col] = pdf.groupby("date")[raw_col].transform(_z)


# 融合配置:每项 = list[(z_col, weight, sign)]。sign=+1 看多倾斜、−1 反用。
# z 列在 evaluate 里按需生成。等权 = 各 weight 相同(1.0);IC 加权在运行时按 |IC| 覆盖。
def _base_signals() -> dict:
    """信号池 → (原始列, 融合内符号)。符号 = 该原始信号"越大越看多未来5日"的方向。
       str_超买超卖 已 signed(超卖→+);str_拐点 ≥0 看多;str_技术趋势 signed(评级越高越正)但
       历史 rank-IC 显著为负 → 反用(sign −1);bias20/percent_b 越低越超跌 → 反用(sign −1)。

    ★ WI-6 消息面预留挂点(不实现):融合分是线性可加结构,未来叠加消息面增量层只需在此注册
      新 key,例如 "news": ("news_score", +1),并在面板采集处补出 news_score 列即可;
      评测/权重/复合框架无需改动。见 docs/策略/技术合议融合基线_设计.md「消息面预留」。"""
    return {
        "os":      ("str_超买超卖", +1),   # 超买超卖(核心正)
        "rev":     ("str_拐点",     +1),   # 拐点(次级正)
        "trend":   ("str_技术趋势", -1),   # 技术趋势(反用,负权重候选)
        "bias":    ("bias20",       -1),   # 结构化:低乖离=超跌(反用)
        "pctb":    ("percent_b",    -1),   # 结构化:低布林位=贴/破下轨(反用)
    }


CONFIGS: dict[str, list[str]] = {
    # 名称 → 参与信号 key 列表(等权 zscore 复合;符号由 _base_signals 定)
    "F0_核心正(os+rev)":            ["os", "rev"],
    "F1_核心正+反用趋势":            ["os", "rev", "trend"],
    "F2_核心正+结构态":              ["os", "rev", "bias", "pctb"],
    "F3_全量(正+反趋势+结构态)":      ["os", "rev", "trend", "bias", "pctb"],
}


def compute_fusion(pdf: pd.DataFrame, keys: list[str], weights: dict | None = None,
                   score_col: str = "_fuse") -> None:
    """按 keys 复合融合分,写回 score_col。weights=None → 等权;否则 {key: |权重|}(符号仍取信号定义)。
       至少一个成分非 NaN 才有分(全 NaN → NaN)。"""
    sig = _base_signals()
    parts = []
    for k in keys:
        raw, s = sig[k]
        zc = f"z_{raw}"
        if zc not in pdf.columns:
            add_xs_zscore(pdf, raw, zc)
        w = 1.0 if weights is None else float(weights.get(k, 0.0))
        parts.append(s * w * pdf[zc])
    stacked = np.vstack([p.to_numpy(float) for p in parts])   # (n_sig, n_row)
    with np.errstate(invalid="ignore"):
        allnan = np.all(~np.isfinite(stacked), axis=0)
        summed = np.nansum(stacked, axis=0)
    summed[allnan] = np.nan
    pdf[score_col] = summed


# ═════════════════════════ ③ 评测(rank-IC + Top-N gross/net,按日聚类 p) ═════════════════════════
def _by_day(pdf: pd.DataFrame, score_col: str) -> dict:
    """{date: [(score, fwd5), ...]}(仅 score 有限)。"""
    day: dict[str, list] = {}
    sub = pdf[["date", score_col, "fwd5"]].to_numpy(object)
    for dt, sc, fw in sub:
        sc = float(sc) if sc is not None else float("nan")
        if not np.isfinite(sc):
            continue
        day.setdefault(dt, []).append((sc, float(fw)))
    return day


def eval_ranker(pdf: pd.DataFrame, score_col: str, topns=(5, 10, 20),
                costs: dict | None = None, seed: int = 20260828) -> dict:
    """rank-IC + Top-N(gross + net 扣成本)超额,按日聚类 p。全宇宙基线=当日面板内全票 5日均收益。"""
    costs = costs or COSTS
    day = _by_day(pdf, score_col)
    pairs = []
    for _dt, lst in day.items():
        if len(lst) < 3:
            continue
        pairs.append((np.array([x[0] for x in lst]), np.array([x[1] for x in lst])))
    ic = _st.rank_ic(pairs)

    out_topn = {}
    for N in topns:
        strat_day, mkt_day, days_used, sel_all, hit, n_sel = [], [], 0, [], 0, 0
        for _dt, lst in day.items():
            if len(lst) < N:
                continue
            days_used += 1
            top = sorted(lst, key=lambda x: x[0], reverse=True)[:N]
            top_fwd = np.array([x[1] for x in top], float)
            uni_fwd = np.array([x[1] for x in lst], float)
            strat_day.append(top_fwd)
            mkt_day.append(float(uni_fwd.mean()))
            sel_all.extend(top_fwd.tolist())
            hit += int((top_fwd > 0).sum())
            n_sel += len(top_fwd)
        if n_sel == 0:
            out_topn[f"Top{N}"] = {"预测日数": 0}
            continue
        gross = _st.cluster_bootstrap_excess(strat_day, mkt_day, seed=seed)
        rec = {
            "预测日数": days_used, "选中样本": n_sel,
            "均5日收益%": round(float(np.mean(sel_all)), 3),
            "命中率%": round(hit / n_sel * 100, 1),
            "gross超额%": gross.get("excess"),
            "gross_CI%": ([gross.get("lo"), gross.get("hi")] if gross.get("lo") is not None else None),
            "gross_p": gross.get("p_value"),
            "聚类交易日数": gross.get("n_days"),
        }
        # net:每日策略票收益扣往返成本后重算超额(市场腿买入持有,不计再平衡成本)
        for cname, cost in costs.items():
            net_strat = [arr - cost for arr in strat_day]
            netx = _st.cluster_bootstrap_excess(net_strat, mkt_day, seed=seed)
            rec[f"{cname}超额%"] = netx.get("excess")
            rec[f"{cname}_p"] = netx.get("p_value")
        out_topn[f"Top{N}"] = rec
    return {"rank_ic": ic, "topn": out_topn}


# ═════════════════════════ ④ 单信号 IC(供 IC 加权 + 反用达标判定) ═════════════════════════
def solo_ic(pdf: pd.DataFrame) -> dict:
    """各原始信号(未加符号,直接 z 值)单独当排序器的 rank-IC。用于 IC 加权与符号核验。"""
    sig = _base_signals()
    out = {}
    for k, (raw, _s) in sig.items():
        zc = f"z_{raw}"
        if zc not in pdf.columns:
            add_xs_zscore(pdf, raw, zc)
        r = eval_ranker(pdf, zc, topns=(10,))
        out[k] = {"raw": raw, "rank_ic": r["rank_ic"]["mean_ic"],
                  "t": r["rank_ic"]["t_stat"], "p": r["rank_ic"]["p_value"]}
    return out


def ic_weights(pdf: pd.DataFrame, keys: list[str]) -> dict:
    """按 |历史 rank-IC| 比例给权重(符号仍由信号定义;此处只出正权重幅度)。
       用信号的**有符号 z**(sign·z)对 fwd5 的 IC 幅度作权重。"""
    sig = _base_signals()
    raw_ic = {}
    for k in keys:
        raw, s = sig[k]
        zc = f"z_{raw}"
        if zc not in pdf.columns:
            add_xs_zscore(pdf, raw, zc)
        # 有符号 z 的 IC(应为正 = 该信号按定义方向确有正预测力)
        tmp = f"_signed_{raw}"
        pdf[tmp] = s * pdf[zc]
        r = eval_ranker(pdf, tmp, topns=(10,))
        raw_ic[k] = abs(r["rank_ic"]["mean_ic"] or 0.0)
    tot = sum(raw_ic.values()) or 1.0
    return {k: raw_ic[k] / tot for k in keys}


# ═════════════════════════ ⑤ council 基线(同面板复算,apples-to-apples) ═════════════════════════
def council_composite(pdf: pd.DataFrame, wmap: dict, score_col: str) -> None:
    """council 口径 S=Σ(强度×置信度×w)/Σ(w×置信度)(与 convene 同),写回 score_col。分母0→NaN。"""
    num = np.zeros(len(pdf))
    den = np.zeros(len(pdf))
    for name in ACTIVE_EXPERTS:
        w = float(wmap.get(name, 1.0))
        if w == 0:
            continue
        s = pdf[f"str_{name}"].to_numpy(float)
        conf = pdf[f"conf_{name}"].to_numpy(float)
        s = np.nan_to_num(s)
        conf = np.nan_to_num(conf)
        num += s * conf * w
        den += w * conf
    out = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    pdf[score_col] = out


# ═════════════════════════ ⑥ 主流程 ═════════════════════════
def evaluate(pdf: pd.DataFrame, out: str = OUT_DEFAULT) -> dict:
    n_days = pdf["date"].nunique()
    logger.info("评测面板行=%d 覆盖交易日=%d", len(pdf), n_days)

    # 单信号 IC(符号核验 + IC 加权基础)
    solos = solo_ic(pdf)

    # 融合分:等权 + IC 加权,各配置
    fusion_out = {}
    for cname, keys in CONFIGS.items():
        compute_fusion(pdf, keys, weights=None, score_col="_fuse")
        eqw = eval_ranker(pdf, "_fuse")
        w = ic_weights(pdf, keys)
        compute_fusion(pdf, keys, weights=w, score_col="_fuse_icw")
        icw = eval_ranker(pdf, "_fuse_icw")
        fusion_out[cname] = {"信号": keys, "等权": eqw, "IC加权": icw, "IC权重": {k: round(v, 3) for k, v in w.items()}}

    # council 基线(同面板)
    council_out = {}
    for bname, wmap in {"V0_council等权(1/1/1)": {"技术趋势": 1, "超买超卖": 1, "拐点": 1},
                        "V2_council删趋势(0/1/1)": {"技术趋势": 0, "超买超卖": 1, "拐点": 1}}.items():
        council_composite(pdf, wmap, "_council")
        council_out[bname] = eval_ranker(pdf, "_council")

    result = {
        "生成于": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "口径": ("横截面:逐日 zscore 复合技术信号 → rank-IC + Top-N gross/net(扣往返成本)超额"
                 "(按日聚类p);防未来函数;非投资建议"),
        "成本口径": {k: f"{v}%/往返" for k, v in COSTS.items()},
        "面板": {"行": len(pdf), "覆盖交易日": n_days},
        "单信号IC": solos,
        "融合配置": fusion_out,
        "council基线_同面板": council_out,
    }
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(result, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    _print_summary(result)
    if out:
        print("→", out)
    return result


def _print_summary(r: dict) -> None:
    print("\n===== 技术合议融合基线 横截面记分卡 =====")
    print("面板:", r["面板"], "成本:", r["成本口径"])
    print("\n[单信号 rank-IC(有符号前,原始 z 对 fwd5)]")
    for k, o in r["单信号IC"].items():
        print(f"  {k:<8}({o['raw']:<12}) IC={o['rank_ic']} t={o['t']} p={o['p']}")

    def _row(tag, o):
        ic = o["rank_ic"]
        t5, t10 = o["topn"].get("Top5", {}), o["topn"].get("Top10", {})
        print(f"  {tag:<22} IC={str(ic.get('mean_ic')):>8} t={str(ic.get('t_stat')):>6} "
              f"p={str(ic.get('p_value')):>6} | "
              f"T5 gross={str(t5.get('gross超额%')):>7}(p{t5.get('gross_p')}) "
              f"net20={str(t5.get('net_20bp超额%')):>7}(p{t5.get('net_20bp_p')}) | "
              f"T10 gross={str(t10.get('gross超额%')):>7}(p{t10.get('gross_p')}) "
              f"net20={str(t10.get('net_20bp超额%')):>7}(p{t10.get('net_20bp_p')})")

    print("\n[council 基线(同面板)]")
    for b, o in r["council基线_同面板"].items():
        _row(b, o)
    print("\n[融合配置:等权 vs IC加权]")
    for cname, o in r["融合配置"].items():
        print(f"— {cname}  信号={o['信号']}  IC权重={o['IC权重']}")
        _row("  等权", o["等权"])
        _row("  IC加权", o["IC加权"])


def load_panel(path: str) -> pd.DataFrame:
    return pd.read_parquet(path)


def _main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="技术合议融合基线 横截面回测")
    ap.add_argument("cmd", choices=["collect", "eval", "run"], help="collect采面板 / eval评测 / run一条龙")
    ap.add_argument("--sample", type=int, default=800)
    ap.add_argument("--step", type=int, default=15)
    ap.add_argument("--start-idx", type=int, default=250)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--panel", default=PANEL_DEFAULT)
    ap.add_argument("--out", default=OUT_DEFAULT)
    a = ap.parse_args(argv)

    if a.cmd in ("collect", "run"):
        pdf = collect_panel(a.sample, a.step, a.start_idx, a.seed, out=a.panel)
    else:
        pdf = load_panel(a.panel)
    if a.cmd in ("eval", "run"):
        evaluate(pdf, out=a.out)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
