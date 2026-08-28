"""治本 · 前向累积记分卡:每日 picks + 预测方向 + 净情绪分 → 配"到期实际前瞻收益"。

痛点:点位情绪/预测只存了极短窗口,研究 C 现在统计力≈0。治本靠**滚存累积**:
每天把当日 record 的方向判断(趋势评级 / 净情绪分)记一行,等 K 线走出 t+N 后**自动
回填**该行的实际前瞻收益与"方向命中没"。跑几周就攒出真正可回测的样本,是研究 C 的
长期数据来源,也是"昨日选股 vs 今日实际"记分卡。

幂等设计(可每天重跑):
  · 每次运行**重读全部历史 record**,重算每 (date, code) 的方向标签;
  · 前瞻收益按当前 K 线能算多少算多少(已到期→填实际值,未到期→留空 pending);
  · 全量覆盖写出(而非 append 去重),因此重复运行只会把"新到期"的行补上,不产生脏数据。

无未来函数:方向标签只用信号日 t 及之前(record 是 t 收盘后落的);前瞻收益 close[t+N]
仅作结果标签。命中 = sign(前瞻收益) 与预测方向一致。

字段:date, code, name, trend(趋势评级), senti(净情绪分), pred_dir(合成预测方向 +1/-1/0),
      persist(持续性:结构性持续/短暂事件/中性,读该 record 的根源 events→分类器,LLM不可用则空),
      persist_dir(持续性方向), persist_strength(印证强度), persist_basis(依据),
      r_1/r_5/r_10(到期实际%,未到期=空), hit_1/hit_5/hit_10(方向命中 1/0,未到期=空)。

持续性接入(治本主线 §2 之 live 腿):读该 record 的**根源 events**(层∈{政策,公司行为},非舆情),
拼成一条根源消息 → 调 news_persistence 分类器打「持续性」标签,随记分卡滚存,攒
**持续性×前瞻收益**样本。LLM 未配置/降级 → persist 留空(不崩,约法第5条);结果按文本 hash 缓存,
每日重跑幂等且不重复烧钱。无未来函数:events 是信号日 t 及之前落的,分类只用其文本。

用法:python -m tools.backtest.forward_scorecard [--out path.csv] [--horizon 1,5,10] [--no-persistence]
      缺省 --out 落 scratch;部署到每日闭环时由上层传持久路径(如 data/analysis/backtest/)。
非投资建议。
"""
from __future__ import annotations

import argparse
import logging
import os

import numpy as np
import pandas as pd

from tools.collectors import market
from tools.store import repo as store

logger = logging.getLogger("backtest.scorecard")

_ROOT_LAYERS = ("政策", "公司行为")   # 根源层(公告/政策),非舆情
_DIR_SIGN = {"看涨": 1, "看跌": -1, "中性": 0}   # 条件化方向 → 符号(数据不足→None)

_DEFAULT_OUT = os.path.join(
    "/private/tmp/claude-501/-Users-yqg-Documents-projects-stock-analysis/"
    "c3f60e01-bbca-41c6-b337-cf7966926ca4/scratchpad", "forward_scorecard.csv")


def _pred_dir(trend: str | None, senti: float | None) -> int:
    """合成预测方向:趋势评级为主(偏多+1/偏空-1/中性0),中性时用净情绪分符号兜底。"""
    if trend == "偏多":
        return 1
    if trend == "偏空":
        return -1
    if senti is not None and np.isfinite(senti):
        if senti > 0.05:
            return 1
        if senti < -0.05:
            return -1
    return 0


def _root_message(rec: dict) -> str | None:
    """把 record 里的**根源 events**(层∈{政策,公司行为},非舆情、非 error)拼成一条根源消息文本。

    无根源 events → None(该行持续性留空,降级)。只取披露/公告类,防舆情噪声污染持续性判定。
    """
    events = (rec.get("sentiment") or {}).get("events") or rec.get("events") or []
    parts = []
    for e in events:
        if e.get("层") in _ROOT_LAYERS and "error" not in e:
            title = str(e.get("标题") or "").strip()
            summary = str(e.get("摘要") or "").strip()
            seg = title if title else summary
            if summary and summary != title:
                seg = f"{seg}。{summary}" if seg else summary
            if seg:
                parts.append(seg)
    if not parts:
        return None
    return "公司公告/政策(根源消息):\n" + "\n".join(f"- {p}" for p in parts[:8])


def _persist_labeler(enabled: bool):
    """返回 record→持续性标签 dict 的函数。LLM 未配置/关闭 → 恒返回空标签(降级)。

    结果按根源消息文本 hash 缓存(news_persistence.classify 内),每日重跑幂等、不重复烧钱。
    """
    empty = {"持续性": None, "方向": None, "印证强度": None, "依据": None}
    if not enabled:
        return lambda rec: dict(empty)
    try:
        from tools.llm import client as lc
        if not lc.is_configured():
            logger.info("LLM 未配置,持续性标签降级为空")
            return lambda rec: dict(empty)
        from tools.analysis import news_persistence as npst
        client = lc.get_client()
    except Exception as e:  # noqa: BLE001
        logger.warning("持续性分类器不可用,降级为空: %s", str(e)[:80])
        return lambda rec: dict(empty)

    def _label(rec: dict) -> dict:
        msg = _root_message(rec)
        if not msg:
            return dict(empty)
        r = npst.classify(msg, client=client)
        if r.get("error"):
            return dict(empty)      # 配额/网络降级 → 留空,不写脏值
        return {"持续性": r.get("持续性"), "方向": r.get("方向"),
                "印证强度": r.get("印证强度"), "依据": r.get("依据")}

    return _label


def _load_pool_index():
    """加载全A横截面池索引;缺失/异常 → None(激进版倾斜列降级留空,不崩)。"""
    try:
        from tools.analysis import conditional_predict as cpred
        return cpred.get_pool_index()
    except Exception as e:  # noqa: BLE001
        logger.info("state_pool 不可用,激进版倾斜列降级留空: %s", str(e)[:80])
        return None


def _tilt_labels(rec, sub_kline, as_of, pool_idx, horizons) -> dict:
    """激进版·后验倾斜的中间标签。**无未来函数**:sub_kline 必须已切到信号日 as_of(≤当日),
    在其上算 tech/state_vector/conditional_scenarios;根源信号只读 record.sentiment。

    返回 {signal, p_cond_N, dir_cond_N, p_adj_N, dir_adj_N}(方向为 +1/-1/0/None)。
    pool_idx 缺失 / 数据不足 / 任一步失败 → 返回 {}(该行倾斜列留空)。
    """
    if pool_idx is None or sub_kline is None or len(sub_kline) < 30:
        return {}
    try:
        from tools.analysis import conditional_predict as cpred
        from tools.analysis import technical as ta
        from tools.config.strategy import THRESHOLDS
        tech = ta.compute(sub_kline)
        cond = cpred.conditional_scenarios(sub_kline, tech, pool_idx, as_of)
        P = THRESHOLDS["指标条件化"]
        signal = cpred.root_structural_signal(rec.get("sentiment"))
        dv = cpred.direction_view(
            cond, signal=signal, k=P.get("倾斜增益k", 0.0),
            tilt_horizons=tuple(f"{n}日" for n in P.get("倾斜持有期", [1, 5])))
        out = {"signal": signal}
        for N in horizons:
            v = dv.get(f"{N}日", {})
            out[f"p_cond_{N}"] = v.get("上涨概率%")
            out[f"dir_cond_{N}"] = _DIR_SIGN.get(v.get("方向"))
            out[f"p_adj_{N}"] = v.get("上涨概率%_修正")
            out[f"dir_adj_{N}"] = _DIR_SIGN.get(v.get("方向_修正"))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("倾斜标签失败(%s),该行留空", str(e)[:60])
        return {}


def _columns(horizons) -> list[str]:
    """记分卡列的**权威顺序**(全量/增量两条路径都按此拼行,保证列序与 schema 幂等一致)。"""
    cols = ["date", "code", "name", "trend", "senti", "pred_dir",
            "persist", "persist_dir", "persist_strength", "persist_basis", "signal"]
    for N in horizons:
        cols += [f"r_{N}", f"hit_{N}", f"p_cond_{N}", f"dir_cond_{N}",
                 f"hit_cond_{N}", f"p_adj_{N}", f"dir_adj_{N}", f"hit_adj_{N}"]
    return cols


def _path_mtime(path) -> float:
    """文件 mtime;取不到路径/不存在/异常 → +inf(**保守判定失效**,宁可重算不可漏)。"""
    try:
        if path is not None and os.path.exists(path):
            return os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        pass
    return float("inf")


def _record_mtime(code: str, date: str) -> float:
    """该 (code, date) record json 的 mtime(store 私有布局,零改动只读)。缺路径 → +inf。"""
    try:
        return _path_mtime(store._record_path(code, date))
    except Exception:  # noqa: BLE001
        return float("inf")


def _kline_price_stable(prev_row: dict, horizons, df, idx) -> bool | None:
    """**值校验**(替代旧"K线 parquet mtime"判定):对 prev 命中行,用当前 K线做一次
    廉价价格查表,重算某个**已到期**窗口的 r_N,与 prev 存的同一 r_N 比对(相对容差)。

    为什么不看 parquet mtime:每日 append 当日新 bar 也 bump mtime,但**未改历史前复权价**
    (老行仍有效);而除权 backfill 会**改写整段前复权价**(老行才真失效)。mtime 分不清二者,
    值校验直接看"锚定价链变没变":
      · 返回 True  → 已到期 r_N 与 prev 一致 → 前复权价链未变 → 可冻结/复用(跳过昂贵 _tilt_labels);
      · 返回 False → 不一致 / 现在反而取不到该窗口 / 拿不到价 → 发生改写(或数据缺)→ 保守重算;
      · 返回 None  → 该行无任何已到期 r_N 可校验(全 pending,prev 未存锚定价)→ 交上层保守处理。
    **绝不调 _tilt_labels/conditional_scenarios**,只读已缓存 kline + 常数次查表。
    无未来函数:校验用的 close[idx+N] 与全算口径完全一致。
    """
    if df is None or idx is None:
        return False   # 拿不到价 → 保守重算
    close = df["close"].to_numpy(float)
    for N in horizons:
        pr = prev_row.get(f"r_{N}")
        if pr is None or (isinstance(pr, float) and np.isnan(pr)):
            continue   # 该 N pending,无值可比
        try:
            prv = float(pr)
        except (TypeError, ValueError):
            return False
        if idx + N < len(close) and close[idx] > 0:
            r = float((close[idx + N] / close[idx] - 1.0) * 100.0)
            return bool(abs(r - prv) <= 1e-6 * max(1.0, abs(prv)))   # 收成 python bool(防 np.bool_ 破坏 `is True`)
        return False   # prev 说到期了但现在取不到该窗口 → 价链变了 → 重算
    return None        # 全 pending,无已到期 r_N


def _all_matured(prev_row: dict, horizons) -> bool:
    """prev 行是否所有 r_N 都已到期(非 NaN)→ 整行永久冻结,可直接复用。"""
    for N in horizons:
        v = prev_row.get(f"r_{N}")
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return False
    return True


def _reuse_row(prev_row: dict, cols) -> dict:
    """按权威列序原样复用 prev 行(冻结行:跳过 kline/tilt/persist)。"""
    return {c: prev_row.get(c) for c in cols}


def _refresh_pending(prev_row: dict, cols, horizons, kline_fn, kidx_cache,
                     code: str, date: str) -> dict:
    """pending 行只做**到期刷新**:补新到期的 r_N/hit_N/hit_cond_N/hit_adj_N,
    **不调 _tilt_labels**——命中用缓存里已冻结的 pred_dir/dir_cond_N/dir_adj_N,
    收益口径与全算完全一致(close[idx+N]/close[idx])。已到期的 N 保留 prev 值。
    """
    row = {c: prev_row.get(c) for c in cols}
    df = kline_fn(code)
    idx = kidx_cache.get(code, {}).get(date)
    close = df["close"].to_numpy(float) if (df is not None and idx is not None) else None
    pdir = prev_row.get("pred_dir")
    try:
        pdir = int(pdir) if (pdir is not None and not (isinstance(pdir, float) and np.isnan(pdir))) else 0
    except (TypeError, ValueError):
        pdir = 0
    for N in horizons:
        pr = prev_row.get(f"r_{N}")
        if pr is not None and not (isinstance(pr, float) and np.isnan(pr)):
            continue   # 该 N 已到期 → 冻结,保留 prev
        r = np.nan
        hit = None
        if close is not None and idx is not None and idx + N < len(close) and close[idx] > 0:
            r = float(close[idx + N] / close[idx] - 1.0) * 100.0
            if pdir != 0:
                hit = int(np.sign(r) == np.sign(pdir))
        row[f"r_{N}"] = r
        row[f"hit_{N}"] = hit
        dc, da = prev_row.get(f"dir_cond_{N}"), prev_row.get(f"dir_adj_{N}")
        row[f"hit_cond_{N}"] = int(np.sign(r) == np.sign(dc)) if (not np.isnan(r) and pd.notna(dc) and dc) else None
        row[f"hit_adj_{N}"] = int(np.sign(r) == np.sign(da)) if (not np.isnan(r) and pd.notna(da) and da) else None
    return row


def build_scorecard(dates=None, horizons=(1, 5, 10), classify_persist=True,
                    tilt=True, prev=None, csv_mtime=None) -> pd.DataFrame:
    """重读全部历史 record → 逐 (date, code) 一行,回填已到期前瞻收益 + 方向命中 + 持续性标签。

    classify_persist:是否给每行打持续性标签(读根源 events→分类器)。LLM 不可用则自动降级留空。
    tilt:是否附激进版·后验倾斜列(p_cond/dir_cond/hit_cond 基线 vs p_adj/dir_adj/hit_adj 倾斜)。
      需 state_pool(缺失则该组列留空)。用于"倾斜 vs 纯技术"前向累积 A/B(k 标定/放行闸依据)。

    增量(方案 B·值校验版):prev = 上次 CSV(pd.read_csv),csv_mtime = 其 mtime。逐 (date,code):
      失效 = prev 无此行 / record json mtime > csv_mtime(规则②) / prev 列集不匹配 schema(规则④,整表回退)。
      **规则③改为值校验(不再看 K线 parquet mtime)**:对 prev 命中且 record 未失效的复用候选,
      用当前 K线重算某个已到期 r_N 与 prev 比对(_kline_price_stable):一致→前复权价链未变→冻结/刷新;
      不一致→除权 backfill 改写→重算。全 pending 无值可校验的候选保守全算。
    未失效且全 r_N 到期 → 复用 prev 行;未失效但仍 pending → 只补到期(不跑 tilt)。
    prev=None(或 --rebuild)→ 全量从零。输出仍全量覆盖同一 --out,幂等不变。
    """
    if dates is None:
        dates = store.list_dates()
    label_persist = _persist_labeler(classify_persist)
    pool_idx = _load_pool_index() if tilt else None
    cols = _columns(horizons)

    # prev 索引 + schema 校验(失效条件④):列集不匹配当前 horizon → prev 作废,整表全量重算
    prev_index: dict[tuple[str, str], dict] = {}
    if prev is not None and not prev.empty and set(cols).issubset(set(prev.columns)):
        for pr in prev.to_dict("records"):
            prev_index[(str(pr.get("date")), str(pr.get("code")))] = pr

    kline_cache: dict[str, pd.DataFrame | None] = {}
    kidx_cache: dict[str, dict] = {}

    def _kline(code):
        if code not in kline_cache:
            try:
                dfk = market.load_kline(code).reset_index(drop=True)
                kline_cache[code] = dfk
                kidx_cache[code] = ({str(x)[:10]: i for i, x in enumerate(dfk["date"].tolist())}
                                    if "date" in dfk.columns else {})
            except Exception:
                kline_cache[code] = None
                kidx_cache[code] = {}
        return kline_cache[code]

    rows = []
    for d in dates:
        d = str(d)
        for rec in store.iter_records(date=d):
            meta = rec.get("meta") or {}
            code = meta.get("code")
            if not code:
                continue
            code = str(code)

            # ---- 失效判定(规则② record mtime + 规则③值校验 + prev 命中)----
            prev_row = prev_index.get((d, code))
            record_ok = (prev_row is not None and csv_mtime is not None
                         and _record_mtime(code, d) <= csv_mtime)
            if record_ok:
                # 规则③值校验:重算已到期 r_N 与 prev 比对,判前复权价链是否被除权 backfill 改写
                df = _kline(code)               # 已按 code 缓存;顺带建 date→idx 映射
                idx = kidx_cache.get(code, {}).get(d)
                stable = _kline_price_stable(prev_row, horizons, df, idx)
                if stable is True:
                    if _all_matured(prev_row, horizons):
                        rows.append(_reuse_row(prev_row, cols))        # 冻结行:整行复用
                    else:
                        rows.append(_refresh_pending(prev_row, cols, horizons,
                                                     _kline, kidx_cache, code, d))  # pending:只补到期
                    continue
                # stable False(价链被改写)/ None(全 pending 无值可校验)→ 落到全算(保守)

            # ---- 失效/新增:全算路径 ----
            trend = ((rec.get("signals") or {}).get("trend") or {}).get("评级")
            senti = ((rec.get("sentiment") or {}).get("净情绪分"))
            try:
                senti = float(senti) if senti is not None else None
            except (TypeError, ValueError):
                senti = None
            pdir = _pred_dir(trend, senti)
            plab = label_persist(rec)
            row = {"date": d, "code": code, "name": meta.get("name"),
                   "trend": trend, "senti": senti, "pred_dir": pdir,
                   "persist": plab.get("持续性"), "persist_dir": plab.get("方向"),
                   "persist_strength": plab.get("印证强度"), "persist_basis": plab.get("依据")}

            df = _kline(code)
            idx = kidx_cache.get(code, {}).get(d)
            close = df["close"].to_numpy(float) if (df is not None and idx is not None) else None

            # 激进版倾斜标签:kline 切到信号日 d(≤d,无未来函数)再算条件化 p 与倾斜
            tl = _tilt_labels(rec, df.iloc[:idx + 1].reset_index(drop=True), d, pool_idx, horizons) \
                if (tilt and idx is not None and df is not None) else {}
            row["signal"] = tl.get("signal")

            for N in horizons:
                r = np.nan
                hit = None
                if idx is not None and close is not None and idx + N < len(close) and close[idx] > 0:
                    r = float(close[idx + N] / close[idx] - 1.0) * 100.0
                    if pdir != 0:
                        hit = int(np.sign(r) == np.sign(pdir))
                row[f"r_{N}"] = r
                row[f"hit_{N}"] = hit
                # 倾斜 A/B 列(基线条件化 vs 含消息面倾斜);dir 为 +1/-1/0/None,0/None 不计命中
                dc, da = tl.get(f"dir_cond_{N}"), tl.get(f"dir_adj_{N}")
                row[f"p_cond_{N}"] = tl.get(f"p_cond_{N}")
                row[f"dir_cond_{N}"] = dc
                row[f"hit_cond_{N}"] = int(np.sign(r) == np.sign(dc)) if (not np.isnan(r) and dc) else None
                row[f"p_adj_{N}"] = tl.get(f"p_adj_{N}")
                row[f"dir_adj_{N}"] = da
                row[f"hit_adj_{N}"] = int(np.sign(r) == np.sign(da)) if (not np.isnan(r) and da) else None
            rows.append(row)
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values(["date", "code"]).reset_index(drop=True) if not df.empty else df


def summarize(sc: pd.DataFrame, horizons=(1, 5, 10)) -> dict:
    """当前记分卡的方向命中率概览(仅统计已到期 + 方向非中性的行)。"""
    out = {"总行数": int(len(sc))}
    for N in horizons:
        hcol = f"hit_{N}"
        matured = sc.dropna(subset=[hcol])
        n = len(matured)
        out[f"{N}日"] = {"已到期且有方向": int(n),
                        "方向命中率%": round(float(matured[hcol].mean()) * 100, 1) if n else None,
                        "待到期(pending)": int(sc[f"r_{N}"].isna().sum())}
    if "persist" in sc.columns:
        labeled = sc["persist"].notna()
        out["持续性"] = {"已打标行": int(labeled.sum()),
                        "分布": sc.loc[labeled, "persist"].value_counts().to_dict()}
    # 激进版倾斜 A/B:基线条件化 vs 含消息面倾斜(⚠️前向累积中,样本薄时不足为凭;放行闸看聚类t)
    if "signal" in sc.columns:
        ab = {"根源信号非零行": int(sc["signal"].fillna(0).ne(0).sum())}
        for N in horizons:
            cc, ca = f"hit_cond_{N}", f"hit_adj_{N}"
            if cc not in sc.columns or ca not in sc.columns:
                continue
            mc, ma = sc.dropna(subset=[cc]), sc.dropna(subset=[ca])
            both = sc.dropna(subset=[cc, ca, f"dir_cond_{N}", f"dir_adj_{N}"])
            changed = both[both[f"dir_cond_{N}"] != both[f"dir_adj_{N}"]]
            ab[f"{N}日"] = {
                "基线命中率%": round(float(mc[cc].mean()) * 100, 1) if len(mc) else None,
                "倾斜命中率%": round(float(ma[ca].mean()) * 100, 1) if len(ma) else None,
                "倾斜改判行": int(len(changed)),   # 倾斜真正改变了方向的已到期行
                "改判后命中率%": round(float(changed[ca].mean()) * 100, 1) if len(changed) else None,
            }
        out["激进版倾斜A/B"] = ab
    return out


def run(out=_DEFAULT_OUT, horizons=(1, 5, 10), classify_persist=True, tilt=True,
        rebuild=False):
    # 增量:build 前载入上次 CSV 作 prev,并记其 mtime(失效判定基线)。
    # 首次无 CSV / --rebuild → prev=None 走全量。code 强制读为 str(防 600000/000001 被推成 int 丢零)。
    prev, csv_mtime = None, None
    if not rebuild and os.path.exists(out):
        try:
            prev = pd.read_csv(out, dtype={"code": str})
            csv_mtime = os.path.getmtime(out)
        except Exception as e:  # noqa: BLE001
            logger.warning("载入上次记分卡失败,回退全量重算: %s", str(e)[:80])
            prev, csv_mtime = None, None
    sc = build_scorecard(horizons=horizons, classify_persist=classify_persist, tilt=tilt,
                         prev=prev, csv_mtime=csv_mtime)
    print("\n===== 前向累积记分卡(治本·滚存)=====")
    print("(无未来函数;历史回测≠未来保证,非投资建议)\n")
    if sc.empty:
        print("!! 无 record,记分卡为空")
        return sc
    # 幂等:全量覆盖写(重复运行只补新到期行)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    sc.to_csv(out, index=False, encoding="utf-8-sig")
    summ = summarize(sc, horizons)
    print(f"已写 {len(sc)} 行 → {out}")
    print(f"覆盖日期: {sorted(sc['date'].unique())}")
    for N in horizons:
        s = summ[f"{N}日"]
        print(f"  {N}日: 已到期且有方向={s['已到期且有方向']}  方向命中率={s['方向命中率%']}%  "
              f"待到期={s['待到期(pending)']}")
    if "持续性" in summ:
        p = summ["持续性"]
        print(f"  持续性: 已打标行={p['已打标行']}  分布={p['分布']}"
              + ("(LLM未配置/无根源消息→留空降级)" if p["已打标行"] == 0 else ""))
    if "激进版倾斜A/B" in summ:
        ab = summ["激进版倾斜A/B"]
        print(f"  激进版倾斜A/B(根源信号非零行={ab['根源信号非零行']}):")
        for N in horizons:
            a = ab.get(f"{N}日")
            if a:
                print(f"    {N}日: 基线命中={a['基线命中率%']}%  倾斜命中={a['倾斜命中率%']}%  "
                      f"改判行={a['倾斜改判行']}  改判后命中={a['改判后命中率%']}%")
        print("    ⚠️ 样本薄不足为凭;放行闸=按日聚类t显著为正才上调k,否则收敛0/负(退出判据)。")
    print("\n(样本仍薄;每日重跑此脚本即自动把新到期的前瞻收益补进记分卡,并滚存持续性×前瞻收益 + 倾斜A/B。)")
    return sc


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=_DEFAULT_OUT)
    ap.add_argument("--horizon", default="1,5,10")
    ap.add_argument("--no-persistence", action="store_true",
                    help="不打持续性标签(跳过 LLM 分类,只出方向命中记分卡)")
    ap.add_argument("--no-tilt", action="store_true",
                    help="不算激进版倾斜 A/B 列(跳过条件化池查询,只出基础记分卡)")
    ap.add_argument("--rebuild", action="store_true",
                    help="强制忽略上次 CSV 全量重算(排障/纠偏兜底,绕过增量)")
    a = ap.parse_args()
    run(out=a.out, horizons=tuple(int(x) for x in a.horizon.split(",")),
        classify_persist=not a.no_persistence, tilt=not a.no_tilt, rebuild=a.rebuild)
