"""行业财报专家阈值标定 harness(离线、时序严格、非投资建议)。

下划线前缀 → 行业专家自动发现(见 __init__._discover)**跳过本模块**,不污染专家注册表。

目的:把各行业专家 extra_flags 里"工程占位"的判定阈值,用**历史财报截面分布 + 后续暴雷标签**
做数据支撑的标定,替换拍脑袋的圆整数。

无未来函数红线(硬):
  - 每个样本点锚定在该报告期的 **披露日 disclosure_date**(as_of),而非报告期结束日。
  - 标定用的"暴雷"结局取 as_of **之后** N 个交易日的行情/结果,严禁用 as_of 之前或当日之后回填。

产物:
  build_panel() -> 每 (code, period) 一行的 tidy 面板:
      code, industry, as_of(披露日), 各阈值对应的连续底层度量, 审计意见,
      fwd_ret_120(披露后120交易日收益), fwd_maxdd_120(披露后120日内最大回撤), blowup(暴雷标签)
  summarize() -> 每 (industry, metric) 的分布分位 + 现阈值命中率 + 暴雷区分度(样本够时)

用法:
    python -m tools.analysis.financial.industry._calibration            # 打印摘要
    python -m tools.analysis.financial.industry._calibration --dump csv # 落 tidy 面板

依赖数据(绝对路径,默认取主仓 data/):financial_report(raw)、master/kline(parquet)、
config/code_industry.json。样本薄是本任务已知事实——薄的地方只报分布、不硬标。
"""
from __future__ import annotations

import glob
import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger("analysis.financial.industry.calib")

# 数据根:默认主仓(worktree 下无 data/,故显式指向主仓)。可用环境变量覆盖。
DATA_ROOT = os.environ.get(
    "CALIB_DATA_ROOT", "/Users/yqg/Documents/projects/stock_analysis")

FWD_DAYS = 120          # 披露后前瞻交易日数(约半年)
BLOWUP_RET = -0.30      # 前瞻区间收益 ≤ -30% 记暴雷
BLOWUP_DD = -0.45       # 前瞻区间最大回撤 ≤ -45% 记暴雷
NONSTD_AUDIT = {"标准无保留意见", "无保留意见", None, ""}   # 不在此集合的审计意见=非标(暴雷)


# ── 数据加载 ─────────────────────────────────────────────────────────────
def _fin_codes() -> list[str]:
    codes = set()
    for f in glob.glob(f"{DATA_ROOT}/data/raw/*/financial_report/*.json"):
        if f.endswith(".meta.json"):
            continue
        codes.add(os.path.basename(f).split(".")[0])
    return sorted(codes)


@lru_cache(maxsize=1)
def _industry_map() -> dict:
    """{code: 申万一级}。用 config/code_industry.json + industry_map 归一。"""
    from tools.analysis import industry_map
    p = f"{DATA_ROOT}/config/code_industry.json"
    raw = json.loads(open(p, encoding="utf-8").read())
    out = {}
    for c, name in raw.items():
        out[str(c).zfill(6)] = industry_map.to_sw(name) or name
    return out


def _latest_fin_raw(code: str) -> dict | None:
    """取该票最新快照的 financial_report raw(含全部报告期与披露日)。"""
    cands = sorted(glob.glob(f"{DATA_ROOT}/data/raw/*/financial_report/{code}.json"))
    cands = [c for c in cands if not c.endswith(".meta.json")]
    if not cands:
        return None
    try:
        return json.loads(open(cands[-1], encoding="utf-8").read())
    except Exception as e:                                   # noqa: BLE001
        logger.warning("读 %s 财报失败: %s", code, e)
        return None


@lru_cache(maxsize=8192)
def _kline(code: str):
    import pandas as pd
    p = f"{DATA_ROOT}/data/master/kline/{code}.parquet"
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p, columns=["date", "close"])
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:                                        # noqa: BLE001
        return None


def _forward_outcome(code: str, as_of: str, n: int = FWD_DAYS) -> dict:
    """披露日 as_of 之后(含当日起首个可交易日)前瞻 n 交易日收益 + 最大回撤。
    严格只用 date > as_of 起的行情(未来函数红线:入场价用披露日之后首个交易日收盘)。"""
    df = _kline(code)
    if df is None or df.empty:
        return {"fwd_ret": None, "fwd_maxdd": None, "n_fwd": 0}
    import pandas as pd
    aod = pd.Timestamp(as_of)
    fut = df[df["date"] > aod].head(n + 1)
    if len(fut) < 20:            # 前瞻窗口太短(临近数据尾端)→ 不产出结局标签
        return {"fwd_ret": None, "fwd_maxdd": None, "n_fwd": len(fut)}
    entry = fut["close"].iloc[0]
    if not entry or entry <= 0:
        return {"fwd_ret": None, "fwd_maxdd": None, "n_fwd": len(fut)}
    closes = fut["close"].to_numpy()
    end = closes[-1]
    mn = closes.min()
    return {"fwd_ret": float(end / entry - 1.0),
            "fwd_maxdd": float(mn / entry - 1.0),
            "n_fwd": len(fut)}


# ── 每票底层度量提取(阈值比较的连续量)─────────────────────────────────────
def _ratio(a, b):
    if a is None or b is None or abs(b) < 1e-9:
        return None
    return a / b


def _metrics_for_period(derived: dict, rec: dict) -> dict:
    """抽出各行业阈值实际比较的连续底层度量(跨行业超集;缺值→None)。
    命名与各专家模块 _常量 对应,便于标定回填。"""
    d = derived or {}
    lp = (rec or {}).get("利润表", {}) or {}
    bs = (rec or {}).get("资产负债表", {}) or {}
    cf = (rec or {}).get("现金流量表", {}) or {}
    营收 = lp.get("营业总收入") or lp.get("营业收入")
    营业利润 = lp.get("营业利润")
    归母 = lp.get("归母净利润")
    m = {
        # 通用/多行业
        "毛利率": d.get("毛利率"),
        "净利率": d.get("净利率"),
        "营收增速": d.get("营收增速"),
        "归母净利增速": d.get("归母净利增速"),
        "扣非净利增速": d.get("扣非净利增速"),
        "扣非占归母": d.get("扣非占归母"),
        "现金含量_CFO比净利": d.get("现金含量_CFO比净利"),
        "资产负债率": d.get("资产负债率"),
        "研发费用率": d.get("研发费用率"),
        "应收占营收": _ratio(bs.get("应收账款") or bs.get("应收票据及应收账款"), 营收),
        "应收营收增速差": d.get("应收营收增速差"),
        "存货营收增速差": (round(d["存货增速"] - d["营收增速"], 4)
                    if d.get("存货增速") is not None and d.get("营收增速") is not None else None),
        "商誉占净资产比": _ratio(bs.get("商誉"), bs.get("归母股东权益") or bs.get("股东权益合计")),
        "合同负债环比": d.get("合同负债环比"),
        "在建占总资产": _ratio(bs.get("在建工程"), bs.get("资产总计")),
        "在建占固定资产": _ratio(bs.get("在建工程"), bs.get("固定资产")),
        "FCF比营收": _ratio(d.get("自由现金流"), 营收),
        "capex比营收": _ratio(cf.get("购建固定资产无形资产等支付现金"), 营收),
        # 研发资本化(医药)
        "开发支出比研发费用": _ratio(bs.get("开发支出"), lp.get("研发费用")),
        # 减值占归母(化工/有色/电力设备/传媒)
        "减值占归母": _ratio(
            abs((lp.get("资产减值损失") or 0)) + abs((lp.get("信用减值损失") or 0)),
            abs(归母) if 归母 else None),
        # 金融
        "自营占比": _ratio((lp.get("投资收益") or 0) + (lp.get("公允价值变动收益") or 0), 营收),
        "浮盈占营业利润": _ratio(lp.get("公允价值变动收益"), 营业利润),
        "杠杆倍数": _ratio(bs.get("资产总计"), bs.get("归母股东权益") or bs.get("股东权益合计")),
        "成本收入比": d.get("成本收入比"),
        "存贷比": d.get("存贷比"),
        "非息收入占比": d.get("非息收入占比"),
        "减值占PPOP": d.get("减值占PPOP"),
        # 交通运输 重资产
        "折旧摊销占营收": _ratio(
            (cf.get("固定资产折旧、油气资产折耗、生产性生物资产折旧") or 0)
            + (cf.get("无形资产摊销") or 0), 营收),
        "有息负债占总资产": _ratio(d.get("有息负债"), bs.get("资产总计")),
        # 房地产 三条红线
        "净负债率": _ratio(
            (d.get("有息负债") or 0) - (bs.get("货币资金") or 0),
            bs.get("归母股东权益") or bs.get("股东权益合计")),
        "现金短债比": _ratio(bs.get("货币资金"), d.get("短期有息负债")),
        "少数股东权益占比": _ratio(
            (bs.get("股东权益合计") or 0) - (bs.get("归母股东权益") or 0),
            bs.get("股东权益合计")),
    }
    return m


def build_panel() -> "list[dict]":
    """时序严格的标定面板:每 (code, period) 一行。缺 as_of 或缺行情前瞻窗口 → 结局标签为 None。"""
    from tools.analysis.financial import metrics as metrics_mod
    imap = _industry_map()
    rows: list[dict] = []
    for code in _fin_codes():
        raw = _latest_fin_raw(code)
        if not raw:
            continue
        periods = raw.get("periods", {})
        derived_all = metrics_mod.compute_derived(periods)
        ind = imap.get(code)
        for p, rec in periods.items():
            disc = rec.get("disclosure_date")
            if not disc:
                continue          # 无披露日 → 无法时序锚定,弃
            m = _metrics_for_period(derived_all.get(p, {}), rec)
            out = _forward_outcome(code, disc)
            op = rec.get("audit_opinion")
            nonstd = bool(op) and op not in NONSTD_AUDIT
            blowup = None
            if out["fwd_ret"] is not None:
                blowup = bool(out["fwd_ret"] <= BLOWUP_RET
                              or out["fwd_maxdd"] <= BLOWUP_DD)
            if nonstd:
                blowup = True     # 审计非标独立记暴雷(与行情无关)
            row = {"code": code, "industry": ind, "period": p, "as_of": disc,
                   "audit_nonstd": nonstd,
                   "fwd_ret_120": out["fwd_ret"], "fwd_maxdd_120": out["fwd_maxdd"],
                   "n_fwd": out["n_fwd"], "blowup": blowup}
            row.update(m)
            rows.append(row)
    return rows


# ── 摘要/区分度 ──────────────────────────────────────────────────────────
def _pctl(vals, q):
    import numpy as np
    v = [x for x in vals if x is not None]
    return None if not v else float(np.percentile(v, q))


def summarize(rows=None) -> dict:
    """每 (industry, metric) 的分布分位 + 有效样本数 + 暴雷区分度(有结局标签的子集上)。"""
    import numpy as np
    rows = rows or build_panel()
    metric_keys = [k for k in rows[0] if k not in
                   ("code", "industry", "period", "as_of", "audit_nonstd",
                    "fwd_ret_120", "fwd_maxdd_120", "n_fwd", "blowup")]
    by_ind: dict = {}
    for r in rows:
        by_ind.setdefault(r["industry"], []).append(r)
    summary: dict = {}
    for ind, rs in sorted(by_ind.items(), key=lambda x: -len(x[1])):
        labeled = [r for r in rs if r["blowup"] is not None]
        summary[ind] = {"n_rows": len(rs), "n_codes": len({r["code"] for r in rs}),
                        "n_labeled": len(labeled),
                        "n_blowup": sum(1 for r in labeled if r["blowup"]),
                        "metrics": {}}
        for mk in metric_keys:
            vals = [r[mk] for r in rs if r.get(mk) is not None]
            if len(vals) < 5:
                continue
            entry = {"n": len(vals),
                     "p10": round(_pctl(vals, 10), 4), "p25": round(_pctl(vals, 25), 4),
                     "p50": round(_pctl(vals, 50), 4), "p75": round(_pctl(vals, 75), 4),
                     "p90": round(_pctl(vals, 90), 4)}
            # 区分度:该 metric 在暴雷 vs 非暴雷组的中位数差(样本够才给)
            pos = [r[mk] for r in labeled if r["blowup"] and r.get(mk) is not None]
            neg = [r[mk] for r in labeled if (not r["blowup"]) and r.get(mk) is not None]
            if len(pos) >= 3 and len(neg) >= 3:
                entry["blowup_med"] = round(float(np.median(pos)), 4)
                entry["ok_med"] = round(float(np.median(neg)), 4)
                entry["n_pos"] = len(pos)
                entry["n_neg"] = len(neg)
            summary[ind]["metrics"][mk] = entry
    return summary


# ── 阈值登记表 + 分布锚定推荐 ──────────────────────────────────────────────
# 每项:(行业, 常量名, 现值, 面板 metric_key, 方向 hi/lo, unit_scale)
#   方向 hi = 命中于 metric > 阈值(上尾为坏)→ 锚定 p90;lo = metric < 阈值(下尾为坏)→ 锚定 p10。
#   unit_scale = 常量单位 / metric 单位(如常量用 pct、metric 用比率 → 100)。
# 仅登记「样本充足(≥8 票)」8 行业中、能映射到面板连续度量的阈值;composite/多期/文本类不入表。
THRESHOLD_REGISTRY = [
    # 电子
    ("电子", "_存货增速超营收_pct", 15.0, "存货营收增速差", "hi", 1),
    ("电子", "_研发费用率下限_pct", 3.0, "研发费用率", "lo", 1),
    ("电子", "_在建占固定资产_下限", 0.35, "在建占固定资产", "hi", 1),
    # 基础化工
    ("基础化工", "_HI_周期顶_营收增速", 40.0, "营收增速", "hi", 1),
    ("基础化工", "_HI_周期顶_毛利率", 25.0, "毛利率", "hi", 1),
    ("基础化工", "_存货超营收增速_GAP", 20.0, "存货营收增速差", "hi", 1),
    ("基础化工", "_HI_减值占归母", 0.30, "减值占归母", "hi", 1),
    ("基础化工", "_HI_在建占总资产", 0.20, "在建占总资产", "hi", 1),
    ("基础化工", "_LO_毛利率", 8.0, "毛利率", "lo", 1),
    ("基础化工", "_LO_现金含量", 0.3, "现金含量_CFO比净利", "lo", 1),
    # 医药生物
    ("医药生物", "_HI_研发资本化比", 1.0, "开发支出比研发费用", "hi", 1),
    ("医药生物", "_LO_毛利率_集采", 30.0, "毛利率", "lo", 1),
    ("医药生物", "_HI_应收占营收", 0.5, "应收占营收", "hi", 1),
    ("医药生物", "_应收超营收增速_GAP", 20.0, "应收营收增速差", "hi", 1),
    ("医药生物", "_存货超营收增速_GAP", 20.0, "存货营收增速差", "hi", 1),
    ("医药生物", "_HI_商誉占净资产", 0.30, "商誉占净资产比", "hi", 1),
    ("医药生物", "_LO_现金含量", 0.3, "现金含量_CFO比净利", "lo", 1),
    # 食品饮料
    ("食品饮料", "_应收超营收增速_GAP", 20.0, "应收营收增速差", "hi", 1),
    ("食品饮料", "_存货超营收增速_GAP", 25.0, "存货营收增速差", "hi", 1),
    ("食品饮料", "_LO_毛利率", 25.0, "毛利率", "lo", 1),
    ("食品饮料", "_LO_现金含量", 0.5, "现金含量_CFO比净利", "lo", 1),
    ("食品饮料", "_LO_合同负债环比", -15.0, "合同负债环比", "lo", 1),
    # 交通运输
    ("交通运输", "_崩塌_毛利率低_pct", 10.0, "毛利率", "lo", 1),
    ("交通运输", "_双杀_资产负债率_pct", 65.0, "资产负债率", "hi", 1),
    ("交通运输", "_有息负债占资产_pct", 30.0, "有息负债占总资产", "hi", 100),
    ("交通运输", "_折旧占营收_上限", 0.25, "折旧摊销占营收", "hi", 1),
    # 机械设备
    ("机械设备", "_HI_应收占营收", 0.4, "应收占营收", "hi", 1),
    ("机械设备", "_应收超营收增速_GAP", 20.0, "应收营收增速差", "hi", 1),
    ("机械设备", "_HI_在建占总资产", 0.15, "在建占总资产", "hi", 1),
    ("机械设备", "_存货超营收增速_GAP", 25.0, "存货营收增速差", "hi", 1),
    ("机械设备", "_LO_研发费用率", 2.0, "研发费用率", "lo", 1),
    ("机械设备", "_LO_合同负债环比", -15.0, "合同负债环比", "lo", 1),
    # 有色金属
    ("有色金属", "_HI_周期顶毛利率", 45.0, "毛利率", "hi", 1),
    ("有色金属", "_HI_周期顶营收增速", 40.0, "营收增速", "hi", 1),
    ("有色金属", "_LO_成本崩毛利率", 10.0, "毛利率", "lo", 1),
    ("有色金属", "_存货超营收增速_GAP", 25.0, "存货营收增速差", "hi", 1),
    ("有色金属", "_HI_减值占归母", 0.30, "减值占归母", "hi", 1),
    ("有色金属", "_HI_在建占总资产", 0.20, "在建占总资产", "hi", 1),
    # 电力设备
    ("电力设备", "_存货超营收增速_GAP", 20.0, "存货营收增速差", "hi", 1),
    ("电力设备", "_HI_减值占归母", 0.30, "减值占归母", "hi", 1),
    ("电力设备", "_HI_在建占总资产", 0.20, "在建占总资产", "hi", 1),
    ("电力设备", "_LO_毛利率", 8.0, "毛利率", "lo", 1),
    ("电力设备", "_LO_现金含量", 0.3, "现金含量_CFO比净利", "lo", 1),
    ("电力设备", "_HI_应收占营收", 0.4, "应收占营收", "hi", 1),
]


def recommend(summary=None) -> "list[dict]":
    """对登记表每项:算现值命中率位次 + 分布锚定推荐值(hi→p90,lo→p10)。
    命中率 = 现阈值下会触发红旗的历史样本占比(判断'现值是否离谱':~0.5=太松,~0=太严从不发声)。"""
    import numpy as np
    summary = summary or summarize()
    recs = []
    for ind, const, cur, mk, direction, scale in THRESHOLD_REGISTRY:
        s = summary.get(ind, {}).get("metrics", {}).get(mk)
        if not s:
            recs.append({"行业": ind, "常量": const, "现值": cur, "metric": mk,
                         "note": "面板无该度量(数据薄/字段缺),保留现占位"})
            continue
        # 需要原始 vals 算命中率与推荐分位——从 summary 无法反推,故重取
        recs.append({"行业": ind, "常量": const, "现值": cur, "metric": mk,
                     "方向": direction, "scale": scale, "_summary": s})
    return recs


def recommend_full(rows=None) -> "list[dict]":
    """完整推荐:用面板原始值算 现阈值命中率 + p10/p85/p90 锚定推荐(带单位换算)。"""
    import numpy as np
    rows = rows or build_panel()
    by_ind_metric: dict = {}
    for r in rows:
        for k, v in r.items():
            if k in ("code", "industry", "period", "as_of", "audit_nonstd",
                     "fwd_ret_120", "fwd_maxdd_120", "n_fwd", "blowup"):
                continue
            if v is None:
                continue
            by_ind_metric.setdefault((r["industry"], k), []).append(v)
    out = []
    for ind, const, cur, mk, direction, scale in THRESHOLD_REGISTRY:
        vals = by_ind_metric.get((ind, mk), [])
        n = len(vals)
        if n < 8:
            out.append({"行业": ind, "常量": const, "现值": cur, "n": n,
                        "推荐": cur, "锚": "样本<8,保留占位"})
            continue
        arr = np.array(vals, dtype=float)
        cur_metric = cur / scale                     # 阈值换算到 metric 单位
        if direction == "hi":
            hit = float((arr > cur_metric).mean())
            anchor = float(np.percentile(arr, 90))
        else:
            hit = float((arr < cur_metric).mean())
            anchor = float(np.percentile(arr, 10))
        rec_const = anchor * scale
        out.append({"行业": ind, "常量": const, "现值": cur, "n": n,
                    "方向": direction, "现命中率": round(hit, 3),
                    "锚p": (90 if direction == "hi" else 10),
                    "推荐": round(rec_const, 2)})
    return out


def _print_recommend(recs):
    print("\n===== 阈值分布锚定推荐(hi→p90 / lo→p10;现命中率≈0.5=太松,≈0=从不发声)=====")
    print(f"{'行业':<6}{'常量':<24}{'n':>4}{'现值':>10}{'现命中率':>9}{'推荐':>10}  锚")
    for r in recs:
        if "现命中率" in r:
            print(f"{r['行业']:<6}{r['常量']:<24}{r['n']:>4}{r['现值']:>10.2f}"
                  f"{r['现命中率']:>9.3f}{r['推荐']:>10.2f}  p{r['锚p']}")
        else:
            print(f"{r['行业']:<6}{r['常量']:<24}{r['n']:>4}{r['现值']:>10.2f}"
                  f"{'--':>9}{r['推荐']:>10.2f}  {r['锚']}")


def _print_summary(summary: dict):
    for ind, s in summary.items():
        print(f"\n### {ind}  票{s['n_codes']} 行{s['n_rows']} "
              f"有结局{s['n_labeled']}(暴雷{s['n_blowup']})")
        for mk, e in s["metrics"].items():
            base = (f"    {mk:<16} n={e['n']:>3}  "
                    f"p10={e['p10']} p25={e['p25']} p50={e['p50']} "
                    f"p75={e['p75']} p90={e['p90']}")
            if "blowup_med" in e:
                base += (f"  | 暴雷中位{e['blowup_med']}(n{e['n_pos']}) "
                         f"vs 正常{e['ok_med']}(n{e['n_neg']})")
            print(base)


if __name__ == "__main__":
    import sys
    rows = build_panel()
    print(f"面板:{len(rows)} 行(code×period);"
          f"有结局标签 {sum(1 for r in rows if r['blowup'] is not None)} 行")
    if "--dump" in sys.argv:
        import csv
        out = f"{DATA_ROOT}/data/analysis/industry_calib_panel.csv"
        keys = list(rows[0].keys())
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print("dumped:", out)
    if "--recommend" in sys.argv:
        _print_recommend(recommend_full(rows))
    else:
        _print_summary(summarize(rows))
