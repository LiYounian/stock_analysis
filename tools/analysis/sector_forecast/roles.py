"""四角色识别(产出 B 内核)——龙头 / 中军 / 补涨先锋 / 弹性股。§5 可计算定义。

**因果 PIT**:每只成员的特征只用 ≤date 的 master kline;市值用 ≤date 的 valuation 快照。
口径 = **申万一级**(概念级细分成分缺 = 瓶颈 B1,延后 P2+)。

P1 角色打分只用**可离线因果算**的量(价/量/换手/位置/趋势/β/市值);龙虎榜净买、基本面
quality、北向仅在成员恰好有该数据时作**加分项**,缺失不惩罚(honest degrade,标 flags)。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from tools.analysis import industry_map

logger = logging.getLogger("sector_forecast.roles")

ROLES_VERSION = "v1-2026-09-16"

# 种子板块概念 → 申万一级(诚实:概念级如"低空经济/机器人/算力"无法在申万一级隔离,记为瓶颈B1)
SEED_SW = ["电子", "计算机", "通信", "电力设备", "医药生物",
           "国防军工", "非银金融", "有色金属", "机械设备", "汽车"]
SEED_NOTE = {
    "电子": "含 半导体/AI算力/消费电子(概念级需P2补成分)",
    "计算机": "含 算力/AI应用",
    "电力设备": "含 新能源/储能/锂电",
    "国防军工": "含 军工/低空经济(概念级需P2)",
    "非银金融": "含 券商",
    "有色金属": "含 黄金/稀土",
    "机械设备": "含 机器人/自动化(概念级需P2)",
    "汽车": "含 智能车/消费电子链",
}


def _sector_index_ret(sw: str) -> Optional[pd.Series]:
    """申万一级板块指数日收益(≤全序列)。缺 board kline → None。"""
    from tools.collectors import board
    name = industry_map.to_sw(sw) or sw
    try:
        bk = board.load_board_kline(name)
    except Exception:
        return None
    if bk is None or bk.empty:
        return None
    s = bk[["date", "close"]].copy()
    s["date"] = pd.to_datetime(s["date"]).dt.strftime("%Y-%m-%d")
    close = s.dropna().sort_values("date").set_index("date")["close"].astype(float)
    return close.pct_change().dropna()


def _mktcap(code: str, date: str) -> Optional[float]:
    """≤date 最近一期总市值(亿)。复用 iet_probe 的 PIT 估值读取器(自动走主仓 raw,单一真源)。"""
    from tools.backtest.iet_probe import data as D
    try:
        return D._asof_val(code, date, "总市值")
    except Exception:
        return None


def member_features(sw: str, members: list[str], date: str) -> pd.DataFrame:
    """算板块内成员的角色特征(因果 PIT)。一行一票。"""
    from tools.backtest.iet_probe import data as D
    D.bind_main_repo()
    from tools.store import repo as store
    from tools.analysis.market_forecast import breadth as B

    idx_ret = _sector_index_ret(sw)
    rows = []
    for c in members:
        try:
            df = store.get_master_kline(c)
        except Exception:
            continue
        df = df[df["date"].astype(str).str.slice(0, 10) <= date].copy()
        if len(df) < 25:
            continue
        df["d"] = df["date"].astype(str).str.slice(0, 10)
        close = df["close"].astype(float)
        vol = df["volume"].astype(float)
        cur = df.iloc[-1]
        if cur["d"] != date:            # 当日无K线(停牌)→ 跳过
            continue

        def _ret(n):
            return float(close.iloc[-1] / close.iloc[-1 - n] - 1.0) * 100 if len(close) > n else None
        ret3, ret5, ret10, ret20 = _ret(3), _ret(5), _ret(10), _ret(20)
        # 位置:现价 / 近60日最高
        hi60 = float(df["high"].astype(float).iloc[-60:].max())
        pos60 = float(close.iloc[-1] / hi60) if hi60 else None
        # 放量:今量 / 20日均量
        vma20 = float(vol.iloc[-21:-1].mean()) if len(vol) > 21 else None
        vol_ratio = float(vol.iloc[-1] / vma20) if vma20 else None
        # 趋势:MA5>MA10>MA20 多头
        ma5, ma10, ma20 = close.iloc[-5:].mean(), close.iloc[-10:].mean(), close.iloc[-20:].mean()
        多头 = bool(ma5 > ma10 > ma20)
        # 首次涨停日(近20交易日内最早;越早越先锋)——复用 breadth.is_limit_hit
        first_limit_ago = None
        for i in range(max(0, len(df) - 20), len(df)):
            r = df.iloc[i]
            if B.is_limit_hit(c, _f(r.get("pct_chg")), _f(r.get("close")),
                              _f(r.get("high")), _f(r.get("low")), up=True):
                first_limit_ago = len(df) - 1 - i
                break
        limit_today = bool(B.is_limit_hit(c, _f(cur.get("pct_chg")), _f(cur.get("close")),
                                          _f(cur.get("high")), _f(cur.get("low")), up=True))
        # β:成员日收益 vs 板块指数日收益(近60日回归斜率)
        beta = None
        if idx_ret is not None:
            mr = close.pct_change().dropna()
            mr.index = df["d"].iloc[1:].values
            join = pd.concat([mr.rename("m"), idx_ret.rename("i")], axis=1, join="inner").dropna().iloc[-60:]
            if len(join) >= 20 and join["i"].var() > 0:
                beta = float(np.cov(join["m"], join["i"])[0, 1] / join["i"].var())

        rows.append({
            "code": c, "board": B.board_of(c),
            "close": float(close.iloc[-1]), "pct_chg": _f(cur.get("pct_chg")),
            "amount": _f(cur.get("amount")), "turnover": _f(cur.get("turnover")),
            "ret3": ret3, "ret5": ret5, "ret10": ret10, "ret20": ret20,
            "pos60": round(pos60, 4) if pos60 else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "多头": 多头, "first_limit_ago": first_limit_ago, "limit_today": limit_today,
            "beta": round(beta, 2) if beta is not None else None,
            "市值亿": _mktcap(c, date),
        })
    return pd.DataFrame(rows)


def _rank(s: pd.Series) -> pd.Series:
    return s.rank(pct=True, method="average")


def identify_roles(sw: str, feat: pd.DataFrame, *, top_k: int = 5) -> dict:
    """给板块 feat 打四角色分,各取 top_k(主选前2/备选其余)。返回 {角色: [候选...]}。"""
    if feat.empty:
        return {r: [] for r in ("龙头", "中军", "补涨先锋", "弹性股")}
    f = feat.copy()
    med_beta = f["beta"].median() if f["beta"].notna().any() else None

    # —— 龙头:涨幅领先(5/10日)+ 成交额top + 率先涨停 + 今日涨停/带量 ——
    f["s_龙头"] = (_rank(f["ret5"].fillna(-99)) + _rank(f["ret10"].fillna(-99))
                  + _rank(f["amount"].fillna(0))
                  + f["limit_today"].astype(float) * 0.5
                  + f["first_limit_ago"].notna().astype(float)
                    * (1 - f["first_limit_ago"].fillna(20) / 20) * 0.5)
    # —— 中军:大市值 + 高成交 + 趋势多头 + 抗跌(板块普跌时相对强)——
    f["s_中军"] = (_rank(f["市值亿"].fillna(0)) * 1.5 + _rank(f["amount"].fillna(0))
                  + f["多头"].astype(float) * 0.5
                  + _rank(f["ret20"].fillna(-99)) * 0.5)
    # —— 补涨先锋:位置低 + 短期滞涨 + 近期首次放量/首板 ——
    f["s_补涨"] = ((1 - _rank(f["pos60"].fillna(1))) + (1 - _rank(f["ret5"].fillna(99)))
                  + _rank(f["vol_ratio"].fillna(0)) * 0.8
                  + f["limit_today"].astype(float) * 0.6)
    # —— 弹性股:小市值 + 高换手 + 20/30cm板 + β高于板块中位 ——
    board_elastic = f["board"].isin(["创业板", "科创板", "北交所"]).astype(float)
    beta_hi = (f["beta"] > med_beta).astype(float) if med_beta is not None else 0.0
    f["s_弹性"] = ((1 - _rank(f["市值亿"].fillna(f["市值亿"].max())))
                  + _rank(f["turnover"].fillna(0)) + board_elastic * 0.5 + beta_hi * 0.5)

    def _pick(score_col, reason_fn):
        top = f.nlargest(top_k, score_col)
        out = []
        for i, (_, r) in enumerate(top.iterrows()):
            out.append({
                "code": r["code"], "选级": "主选" if i < 2 else "备选",
                "市值亿": _round(r["市值亿"]), "涨幅5日": _round(r["ret5"]),
                "涨幅10日": _round(r["ret10"]), "换手": _round(r["turnover"]),
                "pos60": _round(r["pos60"]), "beta": _round(r["beta"]),
                "板": r["board"], "今日涨停": bool(r["limit_today"]),
                "理由": reason_fn(r),
            })
        return out

    return {
        "龙头": _pick("s_龙头", lambda r: f"5日涨{_round(r['ret5'])}%/10日{_round(r['ret10'])}%,成交额靠前"
                      + ("、今日涨停" if r["limit_today"] else "")
                      + (f"、{r['first_limit_ago']}日前率先涨停" if pd.notna(r["first_limit_ago"]) else "")),
        "中军": _pick("s_中军", lambda r: f"市值{_round(r['市值亿'])}亿、成交靠前"
                      + ("、均线多头" if r["多头"] else "") + f"、20日{_round(r['ret20'])}%"),
        "补涨先锋": _pick("s_补涨", lambda r: f"位置低(现价/60日高={_round(r['pos60'])})、5日仅{_round(r['ret5'])}%"
                      + (f"、放量{_round(r['vol_ratio'])}x" if pd.notna(r["vol_ratio"]) else "")
                      + ("、今日首板" if r["limit_today"] else "")),
        "弹性股": _pick("s_弹性", lambda r: f"{r['board']}、换手{_round(r['turnover'])}%、市值{_round(r['市值亿'])}亿"
                      + (f"、β={_round(r['beta'])}" if pd.notna(r["beta"]) else "")),
    }


def _f(v):
    try:
        x = float(v); return None if np.isnan(x) else x
    except Exception:
        return None


def _round(v, n=2):
    return None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), n)
