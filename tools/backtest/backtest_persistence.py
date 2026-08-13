"""研究 · 消息持续性(结构性 vs 短暂)前瞻收益漂移 · 回测 slice。

承接 docs/计划/消息持续性分类_预注册与执行.md §2(现在可回测的 slice)+ §3 预注册假设。

命题(§0):净情绪分只判极性;真正决定"能不能拿住"的是**持续性**——结构性持续增长 vs 短暂催化。
可回测 slice = **历史业绩预告**(根源=公司公告,有披露日历)。用 LLM 分类器把抽样的预告分
**结构性持续 / 短暂事件**,看两组前瞻 5/10/20/60 日收益 + 相对沪深300 Alpha 形态:
  · 结构性利好 → 中长窗(20/60)应"持续升"(慢兑现);
  · 短暂利好 → 短窗"spike-then-fade"(见光死)。

⭐ 关键对照(§3 诚实预期):**持续性分组 vs 单纯超预期分组(预增/预减 = PEAD 的 pos/neg)**——
PEAD 已证"超预期无稳定漂移";本 slice 要看**持续性是否比超预期多分出漂移**(是否新增信息)。

数据源:复用 backtest_pead.fetch_forecasts(scratch 缓存 pead_forecasts.parquet;归母净利润口径,
每报告期×code 取最早披露)。事件锚=披露日,进场 t+1,前瞻收益 = close[entry+N]/close[entry]-1。

无未来函数(红线):
  · 分类器输入只用**披露日当日及之前**的信息——当条预告 + 该 code 在**更早披露日**的历史预告序列
    (用于识别"连续多期高增"=结构性);绝不喂入前瞻收益或事后兑现结果。
  · 前瞻收益 close[entry+N] 仅作标签(复用 pead.compute_event,进场 t+1,严格晚于披露日)。
  · 窗口越界(如最近报告期 60 日尚未走完)该窗记 None 并从该窗样本剔除,不外推。
  · spot-check 打印几条事件日期链,肉眼验证前瞻价严格晚于披露日。

统计:每组每窗 样本数/均值收益/胜率/均值Alpha + 单样本 t + **按披露日聚类的 t**(cluster-robust,
     同日披露的事件相关,朴素 t 会高估显著性)。组间差(结构性−短暂 / pos−neg)Welch + 聚类差。

LLM 成本控制:抽样(默认 ~400,分层)+ 缓存(news_persistence.classify 走 hash 缓存)+ 关思考 + 并行。

—— 降级(LLM 配额/不可用时)——
  --heuristic:用**非 LLM 的启发式代理**(仅按预告类型 + 是否连续多期同向)给持续性打标,
  **仅用于验证回测管线(收益/Alpha/聚类t/超预期对照)是否跑通并出预览数**,
  **不是分类器的真实判断**,不能用来回答 §3 假设。控制台/JSON 会显著标注 classifier=heuristic-proxy。

用法:
  python -m tools.backtest.backtest_persistence [--n 400] [--seed 7] [--windows 5,10,20,60] \
      [--periods ...] [--json out.json] [--heuristic]
非投资建议。历史回测≠未来保证。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from tools.backtest import backtest_pead as pead
from tools.collectors import index as idx_col
from tools.collectors import market

logger = logging.getLogger("backtest.persistence")

_DISCLAIMER = ("历史回测≠未来保证,非投资建议。分类只用披露日及之前信息,前瞻收益仅作标签。")
_SCRATCH = Path("/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
                "c3f60e01-bbca-41c6-b337-cf7966926ca4/scratchpad")

# 增长型 / 一次性型 预告类型(供启发式代理 + 消息文本语义提示)
_GROWTH_TYPES = {"预增", "略增", "续盈"}          # 主营增长(可能连续)
_ONEOFF_TYPES = {"扭亏", "减亏"}                   # 常含一次性因素/低基数反弹
_DECLINE_TYPES = {"预减", "略减", "首亏", "续亏", "增亏"}


# ————————————————————————— 抽样(分层) —————————————————————————
def stratified_sample(fc: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    """按预告类型分层抽样 ~n 条(控 LLM 成本 + 覆盖各类型)。仅取有明确方向(pos/neg)的。"""
    fc = fc[fc["方向"].isin(["pos", "neg"])].copy()
    if fc.empty:
        return fc
    frac = min(1.0, n / len(fc))
    parts = []
    for _t, g in fc.groupby("预告类型"):
        k = max(1, round(len(g) * frac))
        k = min(k, len(g))
        parts.append(g.sample(n=k, random_state=seed))
    out = pd.concat(parts).drop_duplicates(subset=["报告期", "code"])
    if len(out) > n:
        out = out.sample(n=n, random_state=seed)
    return out.sort_values("公告日期").reset_index(drop=True)


# ————————————————————————— 分类器输入(无未来函数) —————————————————————————
def _prior_history(fc_all: pd.DataFrame, code: str, before: pd.Timestamp) -> pd.DataFrame:
    """该 code 在 before(当前披露日)**之前**已披露的预告序列(升序)。无未来函数。"""
    h = fc_all[(fc_all["code"] == code) & (fc_all["公告日期"] < before)]
    return h.sort_values("公告日期")


def build_message(row: pd.Series, fc_all: pd.DataFrame) -> str:
    """构造送分类器的根源消息文本:当条业绩预告 + 该 code 更早披露的历史预告序列。

    只含披露日及之前信息 → 让分类器可识别"连续多期高增"(结构性)vs 单季波动(短暂)。
    """
    code = row["code"]
    d = row["公告日期"]
    cur = (f"公司公告(业绩预告):股票代码 {code},报告期 {row['报告期']},"
           f"预告类型「{row['预告类型']}」,业绩变动幅度约 {row['变动幅度']}%(归母净利润口径),"
           f"披露日 {pd.Timestamp(d).date()}。")
    hist = _prior_history(fc_all, code, pd.Timestamp(d))
    if len(hist):
        seq = "; ".join(f"{h['报告期']}期「{h['预告类型']}」变动{h['变动幅度']}%"
                        for _, h in hist.iterrows())
        cur += f"\n该公司更早披露的历史业绩预告序列(供判断是否连续多期同向增长):{seq}。"
    else:
        cur += "\n(该公司本地无更早披露的历史业绩预告可供对比。)"
    return cur


# ————————————————————————— 启发式代理(非 LLM,仅验证管线) —————————————————————————
def heuristic_persist(row: pd.Series, fc_all: pd.DataFrame) -> dict:
    """⚠️ 非 LLM 的启发式代理(仅用于验证回测管线是否跑通 + 预览),不是分类器真实判断。

    规则(粗):连续多期同向(有更早同向预告)→ 结构性持续;单次出现或一次性型(扭亏/减亏)→ 短暂事件。
    """
    code = row["code"]
    hist = _prior_history(fc_all, code, pd.Timestamp(row["公告日期"]))
    prior_dirs = set(hist["方向"].tolist())
    typ = row["预告类型"]
    direction = "利好" if row["方向"] == "pos" else "利空"
    # 连续同向(至少一期更早同向预告)→ 结构性
    if row["方向"] in prior_dirs and typ not in _ONEOFF_TYPES:
        persist = "结构性持续"
    elif typ in _ONEOFF_TYPES:
        persist = "短暂事件"          # 一次性扭亏/减亏
    elif len(hist) == 0:
        persist = "短暂事件"          # 单次出现,无连续性证据
    else:
        persist = "结构性持续" if typ in _GROWTH_TYPES else "短暂事件"
    strength = "中" if len(hist) >= 1 else "弱"
    return {"持续性": persist, "方向": direction, "印证强度": strength,
            "依据": f"[启发式代理] 类型={typ} 历史期数={len(hist)} 历史同向={row['方向'] in prior_dirs}"}


# ————————————————————————— 聚类 t(按披露日) —————————————————————————
def cluster_tstat(values: np.ndarray, clusters: np.ndarray) -> dict:
    """一维均值的按簇(披露日)cluster-robust t(H0: 均值=0)。

    截距回归 r_i = mu + e_i 的 cluster-robust 方差:
      Var(mu) = [Σ_g (Σ_{i∈g} u_i)^2] / N^2 · c ,  u_i=r_i-mu, c=G/(G-1) 小样本修正。
    同日披露的事件横截面相关,朴素 t 会高估显著性;聚类 t 把同日事件当一个"有效观测"。
    """
    m = np.isfinite(values)
    v = values[m]
    cl = clusters[m]
    n = len(v)
    if n < 3:
        return {"n": int(n), "n_clusters": int(len(set(cl.tolist()))) if n else 0,
                "均值": None, "聚类t": None}
    mu = float(v.mean())
    u = v - mu
    groups = {}
    for ui, gi in zip(u, cl):
        groups.setdefault(gi, 0.0)
        groups[gi] += ui
    G = len(groups)
    ss = sum(s * s for s in groups.values())
    corr = G / (G - 1) if G > 1 else 1.0
    var = corr * ss / (n * n)
    t = mu / np.sqrt(var) if var > 0 else None
    return {"n": int(n), "n_clusters": int(G), "均值": round(mu, 6),
            "聚类t": round(float(t), 3) if t is not None else None}


def cluster_diff_tstat(va, ca, vb, cb) -> dict:
    """两组均值差的聚类 t(把两组各自的簇并起来做 cluster-robust,H0: 差=0)。

    对合并样本做含组别哑变量的截距回归,按披露日聚类。近似实现:各组内按簇聚合后,
    用组间均值差 / 合并 cluster-robust 标准误。同日跨组事件也归同簇(市场共同冲击)。
    """
    va, ca = np.asarray(va, float), np.asarray(ca)
    vb, cb = np.asarray(vb, float), np.asarray(cb)
    ma, mb = np.isfinite(va), np.isfinite(vb)
    va, ca, vb, cb = va[ma], ca[ma], vb[mb], cb[mb]
    na, nb = len(va), len(vb)
    if na < 3 or nb < 3:
        return {"n_a": int(na), "n_b": int(nb), "差": None, "聚类t": None}
    mua, mub = va.mean(), vb.mean()
    diff = float(mua - mub)
    # 组内残差按簇求和,合并两组簇贡献(同一披露日在两组各自的贡献分别计入其组均值残差)
    ua = va - mua
    ub = vb - mub
    ga: dict = {}
    for ui, gi in zip(ua, ca):
        ga[gi] = ga.get(gi, 0.0) + ui
    gb: dict = {}
    for ui, gi in zip(ub, cb):
        gb[gi] = gb.get(gi, 0.0) + ui
    # Var(mua)=Σga^2/na^2, Var(mub)=Σgb^2/nb^2;差的方差=两者之和(组独立近似)
    var_a = sum(s * s for s in ga.values()) / (na * na)
    var_b = sum(s * s for s in gb.values()) / (nb * nb)
    Gtot = len(set(list(ga) + list(gb)))
    corr = Gtot / (Gtot - 1) if Gtot > 1 else 1.0
    var = corr * (var_a + var_b)
    t = diff / np.sqrt(var) if var > 0 else None
    return {"n_a": int(na), "n_b": int(nb), "n_clusters": int(Gtot),
            "差": round(diff, 6), "聚类t": round(float(t), 3) if t is not None else None}


# ————————————————————————— 分组汇总 —————————————————————————
def _group_stats(events: list[dict], windows) -> dict:
    """一组事件 → 每窗 样本数/均值收益/胜率/均Alpha/单样本t/**聚类t**(收益 & Alpha)。"""
    out = {"n": len(events)}
    same = np.array([e["披露日当日收益"] for e in events
                     if e["披露日当日收益"] is not None], dtype=float)
    out["披露日当日"] = {"n": int(len(same)),
                         "均值": round(float(same.mean()), 6) if len(same) else None,
                         "上涨占比": round(float((same > 0).mean()), 4) if len(same) else None}
    for n in windows:
        r = np.array([e["前瞻"][n] for e in events if e["前瞻"][n] is not None], dtype=float)
        rc = np.array([e["_disc"] for e in events if e["前瞻"][n] is not None])
        a = np.array([e["alpha"][n] for e in events if e["alpha"][n] is not None], dtype=float)
        ac = np.array([e["_disc"] for e in events if e["alpha"][n] is not None])
        out[n] = {
            "样本数": int(len(r)),
            "均值收益": round(float(r.mean()), 6) if len(r) else None,
            "胜率": round(float((r > 0).mean()), 4) if len(r) else None,
            "收益单样本t": pead._tstat(r)["t"],
            "收益聚类t": cluster_tstat(r, rc),
            "均值Alpha": round(float(a.mean()), 6) if len(a) else None,
            "Alpha胜率": round(float((a > 0).mean()), 4) if len(a) else None,
            "Alpha单样本t": pead._tstat(a)["t"],
            "Alpha聚类t": cluster_tstat(a, ac),
        }
    return out


def _spread(ea: list[dict], eb: list[dict], windows) -> dict:
    """A−B 组前瞻收益 & Alpha 的 Welch + 聚类差 t(A=结构性/pos,B=短暂/neg)。"""
    out = {}
    for n in windows:
        ra = np.array([e["前瞻"][n] for e in ea if e["前瞻"][n] is not None], float)
        ca = np.array([e["_disc"] for e in ea if e["前瞻"][n] is not None])
        rb = np.array([e["前瞻"][n] for e in eb if e["前瞻"][n] is not None], float)
        cb = np.array([e["_disc"] for e in eb if e["前瞻"][n] is not None])
        aa = np.array([e["alpha"][n] for e in ea if e["alpha"][n] is not None], float)
        aca = np.array([e["_disc"] for e in ea if e["alpha"][n] is not None])
        ab = np.array([e["alpha"][n] for e in eb if e["alpha"][n] is not None], float)
        acb = np.array([e["_disc"] for e in eb if e["alpha"][n] is not None])
        out[n] = {
            "收益_Welch": pead._welch(ra, rb),
            "收益_聚类差": cluster_diff_tstat(ra, ca, rb, cb),
            "Alpha_Welch": pead._welch(aa, ab),
            "Alpha_聚类差": cluster_diff_tstat(aa, aca, ab, acb),
        }
    return out


# ————————————————————————— 主流程 —————————————————————————
def run(n=400, seed=7, windows=(5, 10, 20, 60), periods=None,
        json_path=None, heuristic=False, spot_check=True):
    from tools.llm import client as lc
    from tools.analysis import news_persistence as npst

    periods = tuple(periods) if periods else pead.DEFAULT_PERIODS
    mode = "heuristic-proxy(非LLM,仅验证管线)" if heuristic else "LLM分类器(deepseek-v4-pro)"
    print("\n===== 研究 · 消息持续性(结构性 vs 短暂)前瞻收益漂移 =====")
    print(f"(事件锚=披露日, 进场 t+1, 前瞻 {windows} 交易日, 基准=沪深300; classifier={mode})")
    print(f"({_DISCLAIMER})\n")

    fc_all = pead.fetch_forecasts(periods)
    if fc_all.empty:
        print("!! 未拉到任何业绩预告")
        return {"错误": "无预告数据", "免责": _DISCLAIMER}

    sample = stratified_sample(fc_all, n, seed)
    print(f"—— 分层抽样:{len(sample)} 条(目标 {n},seed={seed})——")
    print(f"   报告期分布: {sample['报告期'].value_counts().to_dict()}")
    print(f"   预告类型分布: {sample['预告类型'].value_counts().to_dict()}")
    print(f"   方向(超预期)分布: {sample['方向'].value_counts().to_dict()}\n")

    # —— 分类(LLM 或启发式代理)——
    if heuristic:
        labels = [heuristic_persist(r, fc_all) for _, r in sample.iterrows()]
    else:
        if not lc.is_configured():
            print("!! LLM 未配置,无法用真实分类器;请加 --heuristic 跑管线预览,或配置 LLM 后重跑。")
            return {"错误": "LLM未配置", "免责": _DISCLAIMER}
        msgs = [build_message(r, fc_all) for _, r in sample.iterrows()]
        labels = npst.classify_batch(msgs)
    sample = sample.reset_index(drop=True)
    sample["持续性"] = [x.get("持续性") for x in labels]
    sample["印证强度"] = [x.get("印证强度") for x in labels]
    sample["_label_err"] = [x.get("error") for x in labels]

    n_err = int(sample["_label_err"].notna().sum())
    dist = sample["持续性"].value_counts(dropna=False).to_dict()
    print(f"—— 分类结果:{dist}  (分类失败/降级 {n_err} 条)——\n")
    if n_err and n_err == len(sample):
        print("!! 全部分类失败(多半 LLM 配额/网络);无法出持续性结论。")
        return {"错误": "分类全失败", "分类失败数": n_err,
                "样本数": len(sample), "免责": _DISCLAIMER}

    # —— 前瞻收益(复用 pead.compute_event,含 60 日)——
    bench = idx_col.load_index(idx_col.BENCHMARK)
    kcache: dict = {}

    def _kline(code):
        if code not in kcache:
            try:
                kcache[code] = market.load_kline(code).reset_index(drop=True)
            except Exception:
                kcache[code] = None
        return kcache[code]

    events = []
    dropped = 0
    for _, r in sample.iterrows():
        ev = pead.compute_event(r["公告日期"], _kline(r["code"]), bench, windows)
        if all(v is None for v in ev["前瞻"].values()):
            dropped += 1
            continue
        ev["_code"] = r["code"]; ev["_period"] = r["报告期"]; ev["_type"] = r["预告类型"]
        ev["_disc"] = str(r["公告日期"].date())          # 聚类键 = 披露日
        ev["_dir"] = r["方向"]                            # 超预期方向(pos/neg)
        ev["_persist"] = r["持续性"]; ev["_strength"] = r["印证强度"]
        events.append(ev)
    print(f"—— 可用事件 {len(events)}(前瞻全越界剔除 {dropped};注:最近报告期 60 日多未走完)——\n")

    # —— 分组:持续性(结构性 vs 短暂)——
    struct_ev = [e for e in events if e["_persist"] == "结构性持续"]
    trans_ev = [e for e in events if e["_persist"] == "短暂事件"]
    struct = _group_stats(struct_ev, windows)
    trans = _group_stats(trans_ev, windows)
    persist_spread = _spread(struct_ev, trans_ev, windows)

    # —— 对照:超预期(pos vs neg)同一抽样集 ——
    pos_ev = [e for e in events if e["_dir"] == "pos"]
    neg_ev = [e for e in events if e["_dir"] == "neg"]
    pos = _group_stats(pos_ev, windows)
    neg = _group_stats(neg_ev, windows)
    beat_spread = _spread(pos_ev, neg_ev, windows)

    # —— 对照:印证强度(强/中/弱)——
    strength_groups = {}
    for s in ("强", "中", "弱"):
        g = [e for e in events if e["_strength"] == s]
        if g:
            strength_groups[s] = _group_stats(g, windows)

    # ——— 打印 ———
    def _fmt(name, g):
        print(f"—— {name}(n={g['n']})—— 披露日当日: n={g['披露日当日']['n']} 均值={g['披露日当日']['均值']} 上涨={g['披露日当日']['上涨占比']}")
        for n in windows:
            b = g[n]
            ct = b["Alpha聚类t"]
            print(f"   {n}日: n={b['样本数']} 均收益={b['均值收益']} 胜率={b['胜率']} | "
                  f"均Alpha={b['均值Alpha']} Alpha胜率={b['Alpha胜率']} | "
                  f"收益单t={b['收益单样本t']} 收益聚类t={b['收益聚类t']['聚类t']}(簇{b['收益聚类t']['n_clusters']}) | "
                  f"Alpha单t={b['Alpha单样本t']} Alpha聚类t={ct['聚类t']}(簇{ct['n_clusters']})")

    print("### 持续性分组")
    _fmt("结构性持续", struct)
    _fmt("短暂事件", trans)
    print("\n—— 结构性−短暂 组差 ——")
    for n in windows:
        s = persist_spread[n]
        print(f"   {n}日: 收益差={s['收益_Welch']['差']}(Welch t={s['收益_Welch']['t']} p={s['收益_Welch']['p']}) "
              f"| Alpha差={s['Alpha_Welch']['差']}(Welch t={s['Alpha_Welch']['t']} p={s['Alpha_Welch']['p']} "
              f"聚类t={s['Alpha_聚类差']['聚类t']})")

    print("\n### 对照:超预期(pos vs neg)同一抽样集")
    _fmt("正超预期(pos)", pos)
    _fmt("负超预期(neg)", neg)
    print("\n—— 正−负(超预期)组差 ——")
    for n in windows:
        s = beat_spread[n]
        print(f"   {n}日: Alpha差={s['Alpha_Welch']['差']}(Welch t={s['Alpha_Welch']['t']} p={s['Alpha_Welch']['p']} "
              f"聚类t={s['Alpha_聚类差']['聚类t']})")

    if strength_groups:
        print("\n### 对照:印证强度")
        for s, g in strength_groups.items():
            _fmt(f"印证{s}", g)

    # ——— 无未来函数 spot-check ———
    if spot_check and events:
        print("\n—— 无未来函数 spot-check(前瞻价日期须严格晚于披露日)——")
        for e in events[:2]:
            k = _kline(e["_code"])
            kd = pd.to_datetime(k["date"]).tolist()
            t0 = pead._first_ge(kd, pd.to_datetime(e["_disc"]))
            chain = {"披露日": e["_disc"], "t0": str(kd[t0].date()), "进场t+1": e["进场日"]}
            for n in windows:
                j = t0 + 1 + n
                if j < len(kd):
                    chain[f"进场+{n}"] = str(kd[j].date())
            print(f"   {e['_code']}/{e['_period']}/{e['_type']}/{e['_persist']}: {chain}")

    res = {
        "classifier": "heuristic-proxy" if heuristic else "LLM(deepseek-v4-pro)",
        "periods": list(periods), "windows": list(windows), "seed": seed,
        "抽样数": int(len(sample)), "分类分布": {str(k): int(v) for k, v in dist.items()},
        "分类失败数": n_err, "可用事件": len(events), "前瞻越界剔除": dropped,
        "持续性分组": {"结构性持续": struct, "短暂事件": trans, "结构性减短暂": persist_spread},
        "超预期对照": {"正": pos, "负": neg, "正减负": beat_spread},
        "印证强度分组": strength_groups,
        "免责": _DISCLAIMER,
    }
    if json_path:
        Path(json_path).write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n结果已落盘:{json_path}")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--windows", default="5,10,20,60")
    ap.add_argument("--periods", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--heuristic", action="store_true", help="非LLM启发式代理,仅验证管线")
    a = ap.parse_args()
    run(n=a.n, seed=a.seed,
        windows=tuple(int(x) for x in a.windows.split(",")),
        periods=tuple(x for x in a.periods.split(",") if x) or None,
        json_path=a.json or None, heuristic=a.heuristic)
