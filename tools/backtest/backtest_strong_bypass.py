"""强势不回踩旁路 · 信号级回测(旁路臂 vs 回踩基线臂,同口径 Model A 绝对收益)。

设计见 docs/计划/2026-09-17_强势不回踩旁路_设计与回测方案.md(§4/§5)。

口径(Model A · 与 reselection.portfolio baseline 臂一致):
  D 选(信号) → D+1 入场(marketable 成交) → D+2 收盘无条件了结;净收益扣 10bps;α = 净收益 − 同持有期全A等权。
  · 旁路臂:entry_rule ∈ {open(追D+1开盘·下界代理), breakout(挂D日high·§3.2 fill_entry_exit 精确档)}
  · 回踩基线臂:entry_rule = limit_pc_k(挂 close_D×(1−k),k∈网格)——现有框架"等回踩"
两臂跑在**同一「够格旁路子集」**(§2 bypass_eligible)上,量三维度:成交率 / 绝对收益 / α。

够格判据 = 复用 tools.analysis.selection_synth.bypass_eligible(单一真源,cheap 向量化预筛 → 精确逐票判)。
主线口径(require_mainline=True)需历史 sector_focus;缺失 → 退化口径(--no-mainline,纯形态),结果分开报。

防未来:够格判据/信号只用 ≤D 数据(向量化只回看 ≤t);成交/收益用事后 master kline 的 D+1/D+2 OHLC;
判据阈值预注册(在 selection_synth 写死),绝不对既往赢家调参。⚠️ 研究模拟,非投资建议。产物只写本地。
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from tools.analysis import selection_synth as ss
from tools.research.selection_alpha import nextday_kernel as K

logger = logging.getLogger("backtest.strong_bypass")

COST_BPS = 10.0
MIN_BARS = ss.MIN_BARS            # 60,与 pattern_metrics 一致
DEFAULT_ROOT = "/Users/yqg/Documents/projects/stock_analysis"

# 预注册网格(§4.5;动手前写死,跑完不改)
BASELINE_KS = (0.01, 0.02)       # 回踩基线:挂 close_D×(1−k)
CAP_MULTS = (1.02, 1.03)         # 旁路追高硬顶倍数(OOS 选;这里各跑一档报出,不事后凑)


# ════════════════ 向量化 §2 形态特征(与 pattern_metrics 语义一致) ════════════════
def precompute_bypass(df: pd.DataFrame) -> Optional[dict]:
    """一次性算出每根 bar 的 §2 够格所需形态字段(全部只回看 ≤t,无前视)。K 线不足 → None。"""
    if df is None or len(df) < MIN_BARS + 2:
        return None
    c = df["close"].to_numpy(float)
    h = df["high"].to_numpy(float)
    lo = df["low"].to_numpy(float)
    o = df["open"].to_numpy(float)
    vol = df["volume"].to_numpy(float)
    dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
    n = len(c)
    sc = pd.Series(c)
    ma5 = sc.rolling(5, min_periods=5).mean().to_numpy()
    ma10 = sc.rolling(10, min_periods=10).mean().to_numpy()
    ma20 = sc.rolling(20, min_periods=20).mean().to_numpy()
    # 量比 = vol[t] / mean(vol[t-5:t])(前 5 日均量,不含当日)——同 pattern_metrics
    vol_ma5_prev = pd.Series(vol).shift(1).rolling(5, min_periods=5).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = vol / vol_ma5_prev
        chg = c / np.concatenate([[np.nan], c[:-1]]) - 1.0        # 当日涨跌(昨收→今收)
    hi60 = pd.Series(h).rolling(60, min_periods=60).max().to_numpy()
    lo60 = pd.Series(lo).rolling(60, min_periods=60).min().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        pos60 = (c - lo60) / (hi60 - lo60)
        d60 = c / hi60 - 1.0
    duotou = (ma5 > ma10) & (ma10 > ma20) & (c >= ma5)
    return dict(dates=dates, o=o, h=h, lo=lo, c=c, ma5=ma5, ma10=ma10, ma20=ma20,
                vr=vr, chg=chg, pos60=pos60, d60=d60, duotou=duotou, n=n)


def _form_at(F: dict, t: int) -> dict:
    """第 t 根 bar 的 form(喂 bypass_eligible / fill_entry_exit)。与 pattern_metrics 字段对齐。"""
    return {
        "均线多头": bool(F["duotou"][t]),
        "量比": _f(F["vr"][t]), "位置pos60": _f(F["pos60"][t]), "距60高": _f(F["d60"][t]),
        "当日涨跌": _f(F["chg"][t]), "涨停不可买": bool(_涨停不可买(F, t)),
        "ma5": _f(F["ma5"][t]), "ma10": _f(F["ma10"][t]), "ma20": _f(F["ma20"][t]),
        "现价": _f(F["c"][t]), "当日high": _f(F["h"][t]), "当日low": _f(F["lo"][t]),
        "前低": _f(pd.Series(F["lo"][max(0, t - 19):t + 1]).min()),
    }


def _涨停不可买(F: dict, t: int) -> bool:
    return False  # 占位:D 日涨停由 chg 判(下方 form),此处不用


def _f(v):
    return float(v) if v is not None and np.isfinite(v) else None


# ════════════════ 单臂信号回测 ════════════════
def _entry_price(method: str, form: dict, code: str, cap_mult: float, k: float) -> Optional[float]:
    """据臂口径算挂价 P。旁路 breakout 复用 fill_entry_exit(单一真源);回踩用 close×(1−k);open 用 None(=D+1开盘)。"""
    if method == "open":
        return None                                   # 特判:D+1 开盘价成交(下界代理)
    if method == "breakout":
        st = {"code": code, "入场方式": ss._旁路入场方式}
        old = ss._旁路_CAP_MULT
        try:
            ss._旁路_CAP_MULT = cap_mult
            ss.fill_entry_exit(st, form)
        finally:
            ss._旁路_CAP_MULT = old
        return st.get("挂单价")
    if method == "baseline":
        px = form.get("现价")
        return px * (1.0 - k) if isinstance(px, (int, float)) else None
    raise ValueError(method)


def run(data_root: str, start: str, end: str, *, require_mainline: bool = False,
        universe_limit: int = 0, json_path: Optional[str] = None,
        sector_focus_dir: Optional[str] = None) -> dict:
    print("\n===== 强势不回踩旁路 · 信号级回测(旁路 vs 回踩基线 · Model A 绝对收益)=====")
    print(f"(区间={start}~{end} 口径={'主线强制' if require_mainline else '退化·纯形态'} "
          f"成本={COST_BPS}bps 成交=marketable)")
    print("(⚠️ 研究模拟,非投资建议;防未来:判据/信号≤D,成交/收益用D+1/D+2事后K线)\n")

    codes = K.universe_codes(data_root)
    if universe_limit:
        codes = codes[:universe_limit]
    min_date = f"{int(start[:4]) - 1}-06-01"
    feats_mkt = {}
    F_by_code: dict[str, dict] = {}
    print(f"票池 {len(codes)} 只;加载 K 线 + 向量化形态(min_date≈{min_date})...")
    for code in codes:
        df = K.load_kline(data_root, code, min_date=min_date)
        if df is None:
            continue
        F = precompute_bypass(df)
        if F is not None:
            F_by_code[code] = F
        feats_mkt[code] = K.precompute(df)            # 供全A等权基准
    market = K.build_market(feats_mkt)
    mkt_cc = market["mkt_cc"]
    print(f"有效票 {len(F_by_code)} 只;全A等权基准日 {len(mkt_cc)} 个。开始扫信号...\n")

    # 主线口径:读历史 sector_focus 强利好板块 → {date: {code...}}(缺失则本口径样本为 0,提示退化)
    mainline_codes = _load_mainline(sector_focus_dir, data_root) if require_mainline else None

    arms = ["open"] + [f"breakout×{m}" for m in CAP_MULTS] + [f"baseline_k{k}" for k in BASELINE_KS]
    rec: dict[str, list] = {a: [] for a in arms}       # arm -> list of (year, filled, net, alpha)

    n_sig = 0
    for code, F in F_by_code.items():
        dates, c, o, h, lo = F["dates"], F["c"], F["o"], F["h"], F["lo"]
        n = F["n"]
        # cheap 向量化预筛:均线多头 & pos60∈[0.5,0.85] & 距60高≤-8% & 量比∈[1,2.5] & 当日∈(0, lim-2%)
        pos, d60, vr, chg, duotou = F["pos60"], F["d60"], F["vr"], F["chg"], F["duotou"]
        lim = K.board_limit(code)
        pre = (duotou & (pos >= ss._旁路_pos60_下) & (pos <= ss._旁路_pos60_上)
               & (d60 <= ss._旁路_距60高_上) & (vr >= ss._旁路_量比_下) & (vr <= ss._旁路_量比_上)
               & (chg > 0.0) & (chg < lim - 0.02))
        cand_idx = np.nonzero(pre)[0]
        for t in cand_idx:
            d = dates[t]
            if d < start or d > end:
                continue
            if t + 2 >= n:
                continue                               # 需 D+1、D+2 两根前瞻 bar
            form = _form_at(F, int(t))
            # 精确判(单一真源):够格 + 未命中 §3.2 专属闸(通用 hard_veto 的极高位由判据 G5/G6 已排除,
            # 财报/龙虎在信号级回测无 council,不适用;涨停不可买由 G8 排除)
            role_cand = {"code": code, "role": ss.ROLE_LEADER,
                         "board": ("主线" if not require_mainline else None),
                         "board_ctx": {"强弱": "强"} if not require_mainline else None}
            if require_mainline:
                if mainline_codes is None or code not in mainline_codes.get(d, set()):
                    continue
                role_cand["board"] = "主线"; role_cand["board_ctx"] = {"强弱": "强"}
            if not ss.bypass_eligible(role_cand, form, require_mainline=False):
                continue
            if ss.bypass_hard_gate(form):
                continue
            # D+1 开盘涨停不可买 → 两臂一致剔除(不进分母)
            if bool(K.limit_up_unbuyable(code, np.array([c[t]]), np.array([o[t + 1]]))[0]):
                continue
            n_sig += 1
            year = d[:4]
            o1, h1, l1, c1 = o[t + 1], h[t + 1], lo[t + 1], c[t + 1]
            c2 = c[t + 2]
            bench = mkt_cc.get(dates[t + 2])           # D+1收盘→D+2收盘 全A等权(1日基准)
            bench = float(bench) if bench is not None and np.isfinite(bench) else 0.0
            for arm in arms:
                method, cap_mult, k = _arm_spec(arm)
                P = _entry_price(method, form, code, cap_mult, k)
                filled, net = _fill_modelA(method, P, o1, h1, l1, c1, c2)
                if filled:
                    rec[arm].append((year, 1, net, net - bench))
                else:
                    rec[arm].append((year, 0, np.nan, np.nan))

    summary = {a: _agg(rec[a]) for a in arms}
    verdict = _adopt_verdict(summary)
    out = {
        "config": dict(start=start, end=end, require_mainline=require_mainline,
                       cost_bps=COST_BPS, baseline_ks=BASELINE_KS, cap_mults=CAP_MULTS,
                       n_universe=len(codes), n_valid=len(F_by_code), n_signals=n_sig),
        "口径": "Model A:D选→D+1 marketable入场→D+2收盘无条件了结;α=净收益−同持有期全A等权。",
        "臂": summary,
        "by_year": {a: _agg_by_year(rec[a]) for a in arms},
        "采用判定": verdict,
        "防未来": "判据/信号≤D;成交/收益用D+1/D+2事后K线;阈值预注册不对既往赢家调参。",
        "免责": "⚠️ 研究模拟,非投资建议。历史回测≠未来保证。",
    }
    _print_summary(summary, verdict, n_sig)
    if json_path:
        Path(json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(json_path).write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                                   encoding="utf-8")
        print(f"\n结果已落盘:{json_path}")
    return out


def _arm_spec(arm: str):
    if arm == "open":
        return "open", 1.03, 0.0
    if arm.startswith("breakout×"):
        return "breakout", float(arm.split("×")[1]), 0.0
    if arm.startswith("baseline_k"):
        return "baseline", 1.03, float(arm.split("_k")[1])
    raise ValueError(arm)


def _fill_modelA(method, P, o1, h1, l1, c1, c2):
    """D+1 成交判定 + D+2 了结净收益。open:D+1开盘价买;其余:挂价 P marketable。"""
    if method == "open":
        if not (o1 > 0) or not np.isfinite(c2):
            return False, np.nan
        fill = o1
    else:
        if P is None or not np.isfinite(c2):
            return False, np.nan
        f, _, ok = K.fill_and_return(np.array([P]), np.array([o1]), np.array([h1]),
                                     np.array([l1]), np.array([c1]), "marketable")
        if not bool(ok[0]):
            return False, np.nan
        fill = float(f[0])
    if not (fill > 0):
        return False, np.nan
    gross = c2 / fill - 1.0
    net = (1.0 + gross) * (1.0 - COST_BPS / 1e4) - 1.0    # 单边成本近似(入场;了结成本可略,两臂一致)
    return True, float(net)


def _agg(rows: list) -> dict:
    n = len(rows)
    filled = [r for r in rows if r[1] == 1]
    nf = len(filled)
    nets = np.array([r[2] for r in filled], float) if filled else np.array([])
    alphas = np.array([r[3] for r in filled], float) if filled else np.array([])
    return dict(
        n_signals=n, n_filled=nf,
        成交率=round(nf / n, 4) if n else None,
        胜率=round(float(np.mean(nets > 0)), 4) if nf else None,
        平均净收益=round(float(np.mean(nets)), 4) if nf else None,
        中位净收益=round(float(np.median(nets)), 4) if nf else None,
        平均α=round(float(np.mean(alphas)), 4) if nf else None,
    )


def _agg_by_year(rows: list) -> dict:
    years = sorted(set(r[0] for r in rows))
    return {y: _agg([r for r in rows if r[0] == y]) for y in years}


def _adopt_verdict(summary: dict) -> dict:
    """§5 采用门槛:旁路臂(取 breakout 最优 cap)对比回踩基线(取最优 k)。五条全过才建议采用。"""
    bys = [k for k in summary if k.startswith("breakout×") or k == "open"]
    base = [k for k in summary if k.startswith("baseline_k")]
    if not bys or not base:
        return {"结论": "数据不足", "说明": "缺臂"}
    # 旁路取平均α最高的臂;基线取平均α最高的臂
    def _best(keys):
        cand = [(k, summary[k].get("平均α")) for k in keys if summary[k].get("平均α") is not None]
        return max(cand, key=lambda x: x[1])[0] if cand else keys[0]
    b, g = _best(bys), _best(base)
    sb, sg = summary[b], summary[g]
    checks = {}
    checks["①正收益"] = bool((sb.get("平均净收益") or -1) > 0 and (sb.get("平均α") or -1) > 0)
    checks["②跑赢基线收益"] = bool((sb.get("平均净收益") or -9) >= (sg.get("平均净收益") or 9)
                              and (sb.get("平均α") or -9) >= (sg.get("平均α") or 9))
    cr_b, cr_g = sb.get("成交率"), sg.get("成交率")
    checks["③成交率真增量(≥基线+15pp且≥1.3×)"] = bool(
        cr_b is not None and cr_g is not None
        and cr_b >= cr_g + 0.15 and cr_b >= 1.3 * cr_g)
    adopt = all(checks.values())
    return {"旁路臂": b, "基线臂": g, "逐条": checks,
            "建议": "采用" if adopt else "否掉",
            "说明": ("旁路臂满足正收益+跑赢基线+成交率真增量(其余分年稳健/硬闸不放水见 by_year 与判据设计,"
                    "样本稀缺时以近段首证为准)" if adopt
                    else "至少一条未过,按用户原则『跑赢才采用』建议否掉;见逐条。")}


def _load_mainline(sector_focus_dir, data_root) -> Optional[dict]:
    """主线口径:遍历 sector_focus_dir/*/sector_focus.json,取强利好板块龙头/中军 code。缺失 → None。"""
    base = Path(sector_focus_dir) if sector_focus_dir else Path(data_root) / "data" / "analysis"
    if not base.exists():
        logger.warning("主线口径:sector_focus 目录不存在(%s),退化口径请用 --no-mainline", base)
        return None
    out: dict[str, set] = {}
    for p in sorted(base.glob("*/sector_focus.json")):
        try:
            focus = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        date = p.parent.name
        codes: set = set()
        for blk in ((focus.get("消息驱动") or {}).get("利好板块") or []):
            if "强" not in str(blk.get("强弱") or ""):
                continue
            for d in (blk.get("龙头候选") or []) + (blk.get("跟涨候选") or []):
                if d.get("code"):
                    codes.add(d["code"])
        if codes:
            out[date] = codes
    logger.info("主线口径:载 %d 个交易日的强利好板块 code", len(out))
    return out or None


def _print_summary(summary, verdict, n_sig):
    print(f"信号总数 {n_sig}。各臂(成交率 / 胜率 / 平均净 / 平均α):")
    for a, s in summary.items():
        print(f"  {a:16s} 成交率={s['成交率']} 胜率={s['胜率']} "
              f"净={s['平均净收益']} α={s['平均α']} (filled {s['n_filled']}/{s['n_signals']})")
    print(f"\n采用判定:旁路臂[{verdict.get('旁路臂')}] vs 基线臂[{verdict.get('基线臂')}] → "
          f"**{verdict.get('建议')}**")
    for k, v in (verdict.get("逐条") or {}).items():
        print(f"    {'✓' if v else '✗'} {k}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="强势不回踩旁路 信号级回测")
    ap.add_argument("--data-root", default=DEFAULT_ROOT)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--mainline", action="store_true", help="主线强制口径(需历史 sector_focus)")
    ap.add_argument("--sector-focus-dir", default=None)
    ap.add_argument("--universe-limit", type=int, default=0)
    ap.add_argument("--json", dest="json_path", default=None)
    a = ap.parse_args()
    run(a.data_root, a.start, a.end, require_mainline=a.mainline,
        universe_limit=a.universe_limit, json_path=a.json_path,
        sector_focus_dir=a.sector_focus_dir)


if __name__ == "__main__":
    main()
