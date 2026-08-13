"""策略 S02「放量后缩量回踩」持仓回测(信号日收盘机械买入基线)。

⚠️ 口径:本回测**复用 S01 的持仓回测器**(`position_backtest.simulate_position`:信号日收盘
P0 建仓 + 5 条离场状态机 + 一字板不可成交顺延),离场规则/参数一律读
`THRESHOLDS["趋势深跌反包"]`(未改一行离场逻辑)。因此它衡量的是 **S02 入场信号的原始
质量**——「信号日收盘机械买入、S01 离场」这条基线,**不代表 S02 的最终买法**。

与 S01 回测的唯一差异:信号来源换成 S02 的 `screen_s02.signal_at`(通过本文件新增的
`find_signals_s02` 适配),**不改 S01 的 find_signals 路径**。汇总/基准/最大回撤/Alpha
全部复用 `position_backtest` 的既有函数,保证与 S01 回测同口径可比。

防未来函数:S02 screener 本身只用 t 及之前(当周整周剔除);离场每日决策沿用 S01 状态机
(只用当日及之前)。数据只读复用 collectors.market.load_kline(缺则按 fetch 采集)。
入口:`python -m tools.backtest.backtest_s02 [--codes ...|--universe N] [--fetch] [--no-view]`。
"""
from __future__ import annotations

import logging

from tools.backtest import position_backtest as pb
from tools.pipeline import screen_s02
from tools.store import repo as store

logger = logging.getLogger("backtest.position_s02")

_VIEW = "放量后缩量回踩回测"
_MIN_SAMPLE = 10


def find_signals_s02(kdf, cfg: dict | None = None) -> list[int]:
    """扫 kdf 全历史,返回所有命中 S02 SELECT 的整数索引 t(升序)。历史不足自动跳过。"""
    n = len(kdf)
    start = max(screen_s02.min_history() - 1, 1)
    out = []
    for t in range(start, n):
        if screen_s02.signal_at(kdf, t).get("SELECT"):
            out.append(t)
    return out


# ———————————————————— 趋势过滤 A/B:全A 横截面 RS 面板 ————————————————————
def _dstr(series) -> list[str]:
    """把 date 列规整成 'YYYY-MM-DD' 字符串列表(横截面按日对齐用)。"""
    import pandas as pd
    return pd.to_datetime(series).dt.strftime("%Y-%m-%d").tolist()


def _bench_ret_by_date(bench, window: int) -> dict:
    """沪深300 各交易日的 window 日涨跌幅 close[i]/close[i-window]-1(按日期字符串索引)。"""
    import pandas as pd
    b = bench.copy()
    b["date"] = pd.to_datetime(b["date"])
    b = b.sort_values("date").reset_index(drop=True)
    dates = b["date"].dt.strftime("%Y-%m-%d").tolist()
    close = b["close"].astype(float).to_numpy()
    out: dict[str, float] = {}
    for i in range(window, len(close)):
        if close[i - window] > 0:
            out[dates[i]] = float(close[i] / close[i - window] - 1.0)
    return out


def build_rs_panel(kdf_map: dict, bench, window: int) -> tuple[dict, dict]:
    """全A 每日 RS 百分位面板(无未来函数:每日只用 ≤ 当日的 window 日涨跌幅)。

    RS(code,日) = 个股 window 日涨跌幅 − 沪深300 同日 window 日涨跌幅;
    每个交易日在**当日全A 出现的所有票**间取百分位 rank(pct→0–100,含并列均秩)。
    返回:(rank[date][code]=百分位, code_rs[code][date]=RS 原值)。code_rs 供「RS 曲线向上」查前值。
    """
    import pandas as pd
    bench_ret = _bench_ret_by_date(bench, window)
    by_date: dict[str, dict] = {}
    code_rs: dict[str, dict] = {}
    for code, kdf in kdf_map.items():
        dates = _dstr(kdf["date"])
        close = kdf["close"].astype(float).to_numpy()
        m: dict[str, float] = {}
        for i in range(window, len(close)):
            d = dates[i]
            br = bench_ret.get(d)
            if br is None or close[i - window] <= 0:
                continue
            rs = float(close[i] / close[i - window] - 1.0) - br
            by_date.setdefault(d, {})[code] = rs
            m[d] = rs
        code_rs[code] = m
    rank: dict[str, dict] = {}
    for d, cm in by_date.items():
        s = pd.Series(cm)
        rank[d] = (s.rank(pct=True) * 100.0).to_dict()
    return rank, code_rs


def _attach_alpha(tr: dict, bench) -> None:
    """给一笔已离场交易补同持有期基准收益 + Alpha(与 backtest_one_s02 同口径)。"""
    if tr["状态"] == "已离场" and bench is not None:
        br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
        tr["基准收益"] = br
        tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None


def _welch_t(a: list[float], b: list[float]) -> dict:
    """两独立样本 Welch t(不等方差);返回 {t, df, p_two_sided, n_a, n_b}。样本不足→None 值。"""
    import math
    import statistics as st
    na, nb = len(a), len(b)
    if na < 2 or nb < 2:
        return {"t": None, "df": None, "p双侧": None, "n_a": na, "n_b": nb,
                "说明": "任一组样本<2,t 检验不可算"}
    ma, mb = st.mean(a), st.mean(b)
    va, vb = st.variance(a), st.variance(b)
    if va == 0 and vb == 0:
        return {"t": None, "df": None, "p双侧": None, "n_a": na, "n_b": nb,
                "说明": "两组方差均为0,t 不可算"}
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return {"t": None, "df": None, "p双侧": None, "n_a": na, "n_b": nb, "说明": "SE=0"}
    t = (ma - mb) / se
    num = (va / na + vb / nb) ** 2
    den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = num / den if den > 0 else float("nan")
    # 正态近似双侧 p(大样本足够;小样本仅作参考,已在报告标注统计力不足)
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return {"t": round(t, 4), "df": (round(df, 2) if df == df else None),
            "p双侧(正态近似)": round(p, 4), "n_a": na, "n_b": nb,
            "均值差(a−b)": round(ma - mb, 6)}


def backtest_one_s02(kdf, code: str | None = None, bench=None) -> list[dict]:
    """单票:找所有 S02 信号 → 逐个用 S01 持仓回测器建仓跑离场 → 每笔补基准同持有期收益 + Alpha。"""
    trades = []
    for t in find_signals_s02(kdf):
        tr = pb.simulate_position(kdf, t, code=code)        # 复用 S01 离场状态机(未改)
        tr["code"] = code
        if tr["状态"] == "已离场" and bench is not None:
            br = pb._bench_ret(bench, tr["进场日"], tr["离场日"])
            tr["基准收益"] = br
            tr["Alpha"] = round(tr["收益"] - br, 6) if br is not None else None
        trades.append(tr)
    return trades


def summarize(codes: list[str] | None = None, fetch: bool = False,
              min_sample: int = _MIN_SAMPLE, generated_at: str | None = None) -> dict:
    """跨票**单进程串行**跑 S02 持仓回测并汇总(纯计算,不落库)。

    codes=None → 用本地所有滚动主档(store.list_master_codes)。缺 K线的票诚实跳过。
    绝不并行(防机器过热);慢无妨。数据不足时优雅标注,不报错、不编造。
    """
    from tools.collectors import market

    codes = codes if codes is not None else store.list_master_codes()
    bench = pb._load_bench(fetch)
    need = screen_s02.min_history()
    all_trades: list[dict] = []
    scanned = skipped = signal_codes = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < need:
            skipped += 1
            continue
        scanned += 1
        trades = backtest_one_s02(kdf, code=code, bench=bench)
        if trades:
            signal_codes += 1
        all_trades.extend(trades)

    summary = pb.summarize_trades(all_trades, min_sample=min_sample)
    result = {
        "策略": "放量后缩量回踩(S02)",
        "扫描票数": len(codes), "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped, "出信号票数": signal_codes,
        "有基准": bench is not None and len(bench) > 0,
        "汇总": summary,
        "口径": ("⚠️信号日收盘机械买入基线(复用 S01 持仓回测器:P0=信号日收盘 → 逐日 5 条离场"
                 "状态机撮合;离场参数读 THRESHOLDS['趋势深跌反包'])→ 衡量 S02 入场信号原始质量,"
                 "非 S02 最终买法;胜率/中位收益/盈亏比/最大回撤/平均持有天数 + 同持有期相对沪深300 Alpha;"
                 "防未来函数(S02 当周整周剔除、离场只用当日及之前);一字板不可成交顺延标注"),
        "免责声明": "历史回测证据,非投资建议;样本随主档积累与信号出现而增长,统计力逐步增强。",
    }
    if not result["有基准"]:
        result["Alpha说明"] = "缺沪深300指数K线 → Alpha 未计算(--fetch 采集后可得)"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_and_store(codes: list[str] | None = None, fetch: bool = False,
                  no_view: bool = False, min_sample: int = _MIN_SAMPLE,
                  generated_at: str | None = None) -> dict:
    """算汇总并落 view「放量后缩量回踩回测」(当前运行日期)。no_view=True 只算不落。"""
    result = summarize(codes=codes, fetch=fetch, min_sample=min_sample,
                       generated_at=generated_at)
    if not no_view:
        store.put_view(_VIEW, result)
    return result


# ———————————————————— 趋势过滤 A/B 回测(同口径:A=S02 原版 vs B=S02+趋势门)————————————————————
_VIEW_AB = "放量后缩量回踩趋势过滤AB回测"


def _alpha_stats(trades: list[dict], min_sample: int) -> dict:
    """一组交易的汇总(复用 pb.summarize_trades)+ 抽出 Alpha 列表供显著性检验。"""
    s = pb.summarize_trades(trades, min_sample=min_sample)
    alphas = [t["Alpha"] for t in trades
              if t["状态"] == "已离场" and t.get("Alpha") is not None]
    return s, alphas


def summarize_ab(codes: list[str] | None = None, fetch: bool = False,
                 min_sample: int = _MIN_SAMPLE, generated_at: str | None = None,
                 trend_cfg: dict | None = None) -> dict:
    """S02 趋势过滤 A/B 回测(单进程串行,只读主档,不并行)。

    同口径对照(**核心**):只在趋势门可计算窗(t+1 ≥ 趋势门最少历史根数)内取 S02 信号,
      A = 该窗内所有 S02 信号(原版);B = 该窗内 ∩ 趋势门 PASS(过滤版)。差异只来自过滤器。
    另给 A_full = S02 全历史信号(仅上下文参照)。离场一律复用 S01 持仓回测器(未改)。
    显著性:窗内「PASS 组 vs FAIL 组」Alpha 的 Welch t(样本小则标注统计力不足,不硬下结论)。
    """
    from tools.collectors import market

    tc = trend_cfg or screen_s02._TREND_CFG
    window = int(tc["RS回看"])
    curve_look = int(tc["RS曲线回看"])
    use_curve = bool(tc.get("启用RS曲线向上", False))
    trend_need = screen_s02.trend_min_history(tc)

    codes = codes if codes is not None else store.list_master_codes()
    bench = pb._load_bench(fetch)
    has_bench = bench is not None and len(bench) > 0

    # —— 一次性载入(去重 I/O:RS 面板与回测复用同一批 kdf)——
    kdf_map: dict = {}
    scanned = skipped = 0
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < screen_s02.min_history():
            skipped += 1
            continue
        scanned += 1
        kdf_map[code] = kdf

    rank, code_rs = ({}, {})
    if has_bench:
        rank, code_rs = build_rs_panel(kdf_map, bench, window)

    base_full: list[dict] = []      # A_full:全历史 S02 信号
    base_win: list[dict] = []       # A:窗内 S02 信号
    filt: list[dict] = []           # B:窗内 ∩ 趋势门 PASS
    fail_win: list[dict] = []       # 窗内 ∩ 趋势门 FAIL(显著性对照组)
    sig_codes_full = sig_codes_win = sig_codes_filt = 0
    n_signals_win = pass_n = fail_n = skip_n = no_rs_n = 0

    for code, kdf in kdf_map.items():
        dates = _dstr(kdf["date"])
        sigs = find_signals_s02(kdf)
        had_full = had_win = had_filt = False
        for t in sigs:
            tr = pb.simulate_position(kdf, t, code=code)
            tr["code"] = code
            _attach_alpha(tr, bench)
            base_full.append(tr)
            had_full = True
            if t + 1 < trend_need:                          # 趋势门不可计算窗(次新/历史不足)
                continue
            base_win.append(tr)
            had_win = True
            n_signals_win += 1
            d = dates[t]
            rs_rank = rank.get(d, {}).get(code)
            rs_up = None
            if use_curve:
                dprev = dates[t - curve_look] if t - curve_look >= 0 else None
                rprev = code_rs.get(code, {}).get(dprev) if dprev else None
                rcur = code_rs.get(code, {}).get(d)
                rs_up = (rcur is not None and rprev is not None and rcur > rprev)
            if rs_rank is None:
                no_rs_n += 1                                 # 无 RS(缺基准/窗不足)→ 趋势门条件8不通过
            gate = screen_s02._trend_template(kdf, t, rs_rank, rs_up=rs_up, cfg=tc)
            if gate.get("跳过"):
                skip_n += 1
                fail_win.append(tr)
                continue
            if gate.get("PASS"):
                pass_n += 1
                filt.append(tr)
                had_filt = True
            else:
                fail_n += 1
                fail_win.append(tr)
        sig_codes_full += 1 if had_full else 0
        sig_codes_win += 1 if had_win else 0
        sig_codes_filt += 1 if had_filt else 0

    s_full, _ = _alpha_stats(base_full, min_sample)
    s_win, a_win = _alpha_stats(base_win, min_sample)
    s_filt, a_pass = _alpha_stats(filt, min_sample)
    s_fail, a_fail = _alpha_stats(fail_win, min_sample)
    welch = _welch_t(a_pass, a_fail)                          # PASS vs FAIL 的 Alpha 差异

    result = {
        "策略": "放量后缩量回踩(S02) + Minervini 趋势模板过滤 · A/B",
        "扫描票数": len(codes), "有效样本票": scanned,
        "跳过票数(历史不足/无K线)": skipped,
        "出信号票数(全历史)": sig_codes_full,
        "有基准": has_bench,
        "趋势门参数": {"RS回看": window, "RS排名门槛": tc["RS排名门槛"],
                       "启用RS曲线向上": use_curve, "趋势门最少历史根数": trend_need,
                       "低点距离倍数": tc["低点距离倍数"], "高点距离倍数": tc["高点距离倍数"]},
        "窗内信号统计": {
            "窗内信号数": n_signals_win, "趋势门PASS": pass_n, "趋势门FAIL": fail_n,
            "其中无RS(缺基准/窗不足)": no_rs_n, "历史不足跳过": skip_n,
            "PASS占比": (round(pass_n / n_signals_win, 4) if n_signals_win else None),
        },
        "A_full_S02全历史": s_full,
        "A_S02窗内(原版·对照)": s_win,
        "B_S02+趋势过滤(窗内PASS)": s_filt,
        "对照_窗内FAIL": s_fail,
        "显著性_PASSvsFAIL_Alpha_Welch": welch,
        "口径": ("同口径 A/B:只在趋势门可计算窗(t+1≥趋势门最少历史根数)内取 S02 信号;"
                 "A=窗内全部 S02、B=窗内∩趋势门PASS,差异只来自过滤器。离场复用 S01 持仓回测器"
                 "(P0=信号日收盘 + 5 条离场,读 THRESHOLDS['趋势深跌反包'],未改);"
                 "Alpha=同持有期相对沪深300;RS 为当日全A 横截面百分位(无未来函数);"
                 "最大回撤沿用串行满仓复利口径(内部对照,非实盘组合回撤)。"),
        "免责声明": "历史回测证据,非投资建议;本地历史仅~16个月→趋势门窗内样本天然稀少,统计力可能不足,如实标注。",
    }
    if not has_bench:
        result["Alpha说明"] = "缺沪深300指数K线 → RS/Alpha 未计算(--fetch 采集后可得);趋势门条件8全不通过"
    if generated_at:
        result["生成时间"] = generated_at
    return result


def run_ab_and_store(codes: list[str] | None = None, fetch: bool = False,
                     no_view: bool = False, min_sample: int = _MIN_SAMPLE,
                     generated_at: str | None = None) -> dict:
    """算 A/B 汇总并落 view「放量后缩量回踩趋势过滤AB回测」。no_view=True 只算不落。"""
    result = summarize_ab(codes=codes, fetch=fetch, min_sample=min_sample,
                          generated_at=generated_at)
    if not no_view:
        store.put_view(_VIEW_AB, result)
    return result


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import datetime as _dt
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="策略 S02 放量后缩量回踩 持仓回测汇总(单进程串行)")
    ap.add_argument("--codes", help="逗号分隔代码(默认=本地所有滚动主档)")
    ap.add_argument("--universe", type=int, metavar="N", help="全A票池前 N 只(--codes 优先)")
    ap.add_argument("--fetch", action="store_true", help="缺 K线/基准时采集(默认只读缓存)")
    ap.add_argument("--no-view", action="store_true", help="只算不落库(打印汇总)")
    ap.add_argument("--min-sample", type=int, default=_MIN_SAMPLE, help="统计力阈值")
    ap.add_argument("--ab", action="store_true",
                    help="跑趋势过滤 A/B 对照(S02 原版 vs S02+Minervini 趋势门)")
    ap.add_argument("--sample", type=int, metavar="N",
                    help="A/B:从本地主档随机抽 N 只(默认全A 主档;--codes/--universe 优先)")
    ap.add_argument("--full", action="store_true", help="A/B:用全部本地主档(等价不传 --sample)")
    ap.add_argument("--seed", type=int, default=42, help="A/B 随机抽样种子(可复现)")
    a = ap.parse_args(argv)

    if a.codes:
        codes = [c.strip() for c in a.codes.split(",") if c.strip()]
    elif a.universe:
        from tools.collectors import universe
        codes = universe.universe_codes(limit=a.universe)
    else:
        codes = None
    stamp = _dt.datetime.now().isoformat(timespec="seconds")

    if a.ab:
        if codes is None and a.sample and not a.full:
            import random
            all_codes = store.list_master_codes()
            rng = random.Random(a.seed)
            codes = rng.sample(all_codes, min(a.sample, len(all_codes)))
            logger.info("A/B 随机抽样:%d / %d(seed=%d)", len(codes), len(all_codes), a.seed)
        r = run_ab_and_store(codes=codes, fetch=a.fetch, no_view=a.no_view,
                             min_sample=a.min_sample, generated_at=stamp)
        w = r["窗内信号统计"]
        logger.info("A/B:有效 %d / 窗内信号 %d / PASS %d / FAIL %d",
                    r["有效样本票"], w["窗内信号数"], w["趋势门PASS"], w["趋势门FAIL"])
        print(json.dumps({
            "有效样本票": r["有效样本票"], "窗内信号统计": w,
            "A_S02窗内(原版)": r["A_S02窗内(原版·对照)"],
            "B_S02+趋势过滤": r["B_S02+趋势过滤(窗内PASS)"],
            "对照_窗内FAIL": r["对照_窗内FAIL"],
            "显著性_PASSvsFAIL_Alpha_Welch": r["显著性_PASSvsFAIL_Alpha_Welch"],
        }, ensure_ascii=False, indent=2))
        return 0

    r = run_and_store(codes=codes, fetch=a.fetch, no_view=a.no_view,
                      min_sample=a.min_sample, generated_at=stamp)
    logger.info("扫描 %d / 有效 %d / 出信号 %d;汇总:%s",
                r["扫描票数"], r["有效样本票"], r["出信号票数"], r["汇总"]["状态"])
    print(json.dumps({"扫描票数": r["扫描票数"], "有效样本票": r["有效样本票"],
                      "出信号票数": r["出信号票数"], "汇总": r["汇总"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
