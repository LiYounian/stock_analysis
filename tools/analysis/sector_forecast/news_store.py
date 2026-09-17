"""独立板块/宏观新闻库(产出 D)——**脱离选股闭环、独立采集、独立落库**。

三块(需求文档 §5 + 用户 P2 强化):
  ① 宏观指标快照(真采,PIT):中国10Y国债 / 中美利差 / LPR / 人民币中间价(akshare,≤date)。
  ② 宏观/板块新闻(解耦):复用当日 sentiment_policy(采集侧已产),按宏观主题打标独立落库。
  ③ 资金资讯(best-effort):大宗(block_trade raw)/两融(margin);北向本地无 raw → 标注待接。
→ 派生 **宏观净方向 {偏多/中性/偏空}** + **宏观情景 {加息紧缩/降息宽松/关税冲击/中性}**(供选股预案查表)。

独立落库:data/sector_news/<date>.json(不依赖选股链;不选股也每日可采)。
铁律:forward-shadow·non-gating;防未来只用 ≤date 已披露数据;akshare 单源失败优雅降级不阻断。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import json
import logging
import signal
from pathlib import Path
from typing import Optional

from tools.llm import rubric_map as rm

logger = logging.getLogger("sector_forecast.news_store")

STORE_VERSION = "v1-2026-09-16"

# 宏观主题关键词(与 sector_macro_avoid 同源,取宏观子集;命中 = 新闻 title+summary+keyword 含任一)
THEME_KW = {
    "加息紧缩": ("加息", "美联储", "鲍威尔", "FOMC", "议息", "缩表", "紧缩", "CPI超预期", "通胀"),
    "降息宽松": ("降息", "降准", "LPR下调", "宽松", "放水", "MLF", "逆回购加量"),
    "关税冲击": ("关税", "出口管制", "实体清单", "BIS", "301", "制裁", "封锁"),
}


class _TO(Exception):
    pass


def _with_timeout(fn, secs=25):
    def _h(s, f):
        raise _TO()
    old = signal.signal(signal.SIGALRM, _h)
    signal.alarm(secs)
    try:
        return fn()
    except _TO:
        logger.warning("akshare 调用超时 >%ss,降级 None", secs)
        return None
    except Exception as e:
        logger.warning("akshare 调用失败 %s: %s", type(e).__name__, str(e)[:80])
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# ───────────────────────── ① 宏观指标(真采,PIT) ─────────────────────────

def macro_indicators(date: str) -> dict:
    """中国10Y国债 / 中美利差 / LPR / 人民币中间价,每项带 ≤date 最新值 + 环比 + 对A股方向。"""
    import akshare as ak
    import pandas as pd
    out: dict = {}

    def _asof(df, dcol, date):
        d = df[pd.to_datetime(df[dcol]).dt.strftime("%Y-%m-%d") <= date]
        return d.sort_values(dcol).iloc[-1] if not d.empty else None

    # 国债(中美)
    bond = _with_timeout(lambda: ak.bond_zh_us_rate())
    if bond is not None and not bond.empty:
        row = _asof(bond, "日期", date)
        if row is not None:
            cn10 = _f(row.get("中国国债收益率10年"))
            us10 = _f(row.get("美国国债收益率10年"))
            prev = bond[pd.to_datetime(bond["日期"]).dt.strftime("%Y-%m-%d") < str(row["日期"])[:10]]
            cn10_prev = _f(prev.iloc[-1].get("中国国债收益率10年")) if not prev.empty else None
            chg = (cn10 - cn10_prev) if (cn10 is not None and cn10_prev is not None) else None
            out["国债"] = {
                "中国10Y": cn10, "美国10Y": us10,
                "中美利差": round(cn10 - us10, 4) if (cn10 is not None and us10 is not None) else None,
                "中国10Y环比": round(chg, 4) if chg is not None else None,
                "对A股": ("利多(利率下行)" if chg and chg < -0.01 else
                        "利空(利率上行)" if chg and chg > 0.01 else "中性"),
                "asof": str(row["日期"])[:10],
            }
    # LPR
    lpr = _with_timeout(lambda: ak.macro_china_lpr())
    if lpr is not None and not lpr.empty:
        row = _asof(lpr, "TRADE_DATE", date)
        if row is not None:
            out["LPR"] = {"1Y": _f(row.get("LPR1Y")), "5Y": _f(row.get("LPR5Y")),
                          "asof": str(row["TRADE_DATE"])[:10]}
    # 汇率(人民币中间价,USD/CNY×100;上行=贬值)
    fx = _with_timeout(lambda: ak.currency_boc_sina(
        symbol="美元", start_date=(pd.to_datetime(date) - pd.Timedelta(days=20)).strftime("%Y%m%d"),
        end_date=date.replace("-", "")))
    if fx is not None and not fx.empty:
        row = _asof(fx, "日期", date)
        if row is not None:
            mid = _f(row.get("央行中间价"))
            prev = fx[pd.to_datetime(fx["日期"]).dt.strftime("%Y-%m-%d") < str(row["日期"])[:10]]
            mid_prev = _f(prev.iloc[-1].get("央行中间价")) if not prev.empty else None
            chg = (mid - mid_prev) if (mid is not None and mid_prev is not None) else None
            out["汇率"] = {
                "美元中间价": mid, "环比": round(chg, 2) if chg is not None else None,
                "对A股": ("利空(人民币贬值)" if chg and chg > 1 else
                        "利多(人民币升值)" if chg and chg < -1 else "中性"),
                "asof": str(row["日期"])[:10],
            }
    return out


# ───────────────────────── ② 宏观/板块新闻(解耦落库) ─────────────────────────

def macro_news(date: str) -> dict:
    """复用当日 sentiment_policy,拆'宏观主题命中'与'板块新闻',独立结构落库。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    p = resolve_analysis_file(date, "sentiment_policy.json")
    if not p:
        return {"来源": "缺 sentiment_policy", "宏观命中": [], "n条": 0}
    try:
        msgs = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"来源": "读取失败", "宏观命中": [], "n条": 0}

    theme_hits = {t: [] for t in THEME_KW}
    for m in msgs:
        text = f"{m.get('title','')} {m.get('summary','')} {m.get('keyword','')}"
        for t, kws in THEME_KW.items():
            if any(k in text for k in kws):
                theme_hits[t].append({"title": m.get("title", "")[:50], "方向": m.get("影响方向"),
                                      "强度": m.get("影响强度")})
    return {"来源": "sentiment_policy(选股链副产,独立库过渡期复用)",
            "n条": len(msgs),
            "宏观命中": {t: v for t, v in theme_hits.items() if v},
            "note": "国际龙头(英伟达/台积电)专用海外源为待接项;当前经国内财经媒体覆盖间接捕获"}


# ───────────────────────── ③ 资金资讯(best-effort) ─────────────────────────

def capital_flows(date: str) -> dict:
    """大宗(block_trade raw 聚合)+ 两融(margin);北向本地无 raw → 标注待接。"""
    from tools.analysis.sector_forecast.market_step import resolve_analysis_file
    out: dict = {"北向": {"状态": "本地无 raw,待接采集"}, "两融": None, "大宗": None}
    # 大宗:data/raw/<date>/block_trade/*.json 求和(best-effort,缺则跳过)
    from tools.config import settings
    from tools.backtest.iet_probe.data import _MAIN
    for base in (settings.PROJECT_ROOT, _MAIN):
        bt = Path(base) / "data" / "raw" / date / "block_trade"
        if bt.exists():
            files = list(bt.glob("*.json"))
            files = [f for f in files if not f.name.endswith(".meta.json")]
            out["大宗"] = {"当日成交票数": len(files),
                          "note": "block_trade raw 存在;金额聚合口径待细化"}
            break
    return out


# ───────────────────────── 派生:宏观净方向 + 情景 ─────────────────────────

def derive_macro(indicators: dict, news: dict) -> dict:
    """规则合成(预注册):宏观净方向 {偏多/中性/偏空} + 宏观情景离散标签。"""
    score = 0.0
    reasons = []
    b = indicators.get("国债", {})
    if b.get("中国10Y环比") is not None:
        if b["中国10Y环比"] < -0.01:
            score += 1; reasons.append("10Y利率下行(利多)")
        elif b["中国10Y环比"] > 0.01:
            score -= 1; reasons.append("10Y利率上行(利空)")
    fx = indicators.get("汇率", {})
    if fx.get("环比") is not None:
        if fx["环比"] > 1:
            score -= 1; reasons.append("人民币贬值(利空)")
        elif fx["环比"] < -1:
            score += 1; reasons.append("人民币升值(利多)")

    hits = news.get("宏观命中", {}) if isinstance(news.get("宏观命中"), dict) else {}
    def _net(theme):
        # 主题的**净方向×强度**(利好+/利空−):命中主题词≠利空,须看该条实际方向
        # (如"算力+出口管制"多为国产替代利好,不能当加息式利空计数)。
        return sum({"利好": 1, "利空": -1}.get(x.get("方向"), 0)
                   * rm.strength_to_num(x.get("强度"), default=0.0)   # 文字档→数值(兼容legacy)
                   for x in hits.get(theme, []))
    # 宏观净方向以**硬指标(利率/汇率)为主**;新闻只作辅助且**封顶**——大量板块新闻
    # 顺带提到宏观词(如"算力+出口管制"=国产替代利好)属**板块催化**(focus 已计),
    # 不该再灌进市场级宏观,否则双计且淹没硬指标。关税主题**只在真净利空(冲击)时**减分。
    def _clip(v, lo, hi):
        return max(lo, min(hi, v))
    net_加息, net_关税, net_降息 = _net("加息紧缩"), _net("关税冲击"), _net("降息宽松")
    news_score = (_clip(net_加息, -4, 4) * 0.15
                  + min(0, net_关税) * 0.1            # 只算负向(真冲击),正向国产替代归板块催化
                  + _clip(net_降息, 0, 4) * 0.15)
    score += news_score
    if net_加息:
        reasons.append(f"加息主题净{net_加息:+.0f}")
    if net_关税 < 0:
        reasons.append(f"关税/出口管制净利空{net_关税:+.0f}")
    if net_降息:
        reasons.append(f"降息宽松主题净{net_降息:+.0f}")

    净方向 = "偏多" if score >= 1 else ("偏空" if score <= -1 else "中性")
    # 情景离散标签(供选股预案查表):阈值收严,单条新闻不足以定情景,需硬指标或强净信号
    hard_10y = b.get("中国10Y环比") or 0
    hard_fx = fx.get("环比") or 0
    if net_关税 <= -6:
        情景 = "关税冲击"
    elif net_降息 >= 3 or hard_10y < -0.05:
        情景 = "降息宽松"
    elif net_加息 <= -6 or hard_10y > 0.05 or hard_fx > 3:
        情景 = "加息紧缩"
    else:
        情景 = "中性"
    return {"宏观净方向": 净方向, "宏观情景": 情景, "score": round(score, 2),
            "依据": "；".join(reasons) or "各宏观维居中"}


# ───────────────────────── 组装 + 落库 ─────────────────────────

def build_news_store(date: str) -> dict:
    indicators = macro_indicators(date)
    news = macro_news(date)
    flows = capital_flows(date)
    macro = derive_macro(indicators, news)
    return {
        "date": date, "version": STORE_VERSION, "独立采集": True,
        "宏观指标": indicators, "宏观新闻": news, "资金资讯": flows,
        "宏观研判": macro,
        "诚实边界": ["国际龙头(英伟达/台积电)专用海外feed待接,现经国内财经媒体间接覆盖",
                    "北向 raw 本地缺待接;大宗金额聚合口径待细化",
                    "宏观新闻过渡期复用 sentiment_policy(独立源就绪后替换)"],
        "免责": "测试环境研究模拟,非投资建议。",
    }


def write_news_store(date: str, *, out_root: Optional[str] = None) -> Path:
    from tools.config import settings
    root = Path(out_root) if out_root else settings.PROJECT_ROOT / "data" / "sector_news"
    root.mkdir(parents=True, exist_ok=True)
    payload = build_news_store(date)
    out = root / f"{date}.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.rename(out)
    logger.info("落盘 %s:宏观净方向=%s 情景=%s", out,
                payload["宏观研判"]["宏观净方向"], payload["宏观研判"]["宏观情景"])
    return out


def _f(v):
    try:
        import math
        x = float(v)
        return None if math.isnan(x) else x
    except Exception:
        return None
