"""通达信「最大范围选股」全 A 广度策略。

原公式::

    C / HHV(C, 250) >= 0.82 AND
    C <= HHV(C, 250) * 1.1 AND
    C > MA(C, 20) AND C > MA(C, 50) AND
    COUNT(C > REF(C, 1) * 1.06, 32) >= 1 AND
    C > MA(C, 10) AND C > MA(C, 20) AND C > MA(C, 50) AND
    REF(C, 1) / C - 1 <= 0.04 AND
    FINANCE(3)!=2 AND C > LOW;

每天把入选池和「入选数 / 有效样本」落成同名 view。跨日 view 的占比序列就是
市场情绪广度；不补造历史，只有实际跑过的日期才会出现在曲线中。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.collectors import market
from tools.config.strategy import THRESHOLDS
from tools.store import repo as store

logger = logging.getLogger("pipeline.screen_max_range")

VIEW_NAME = "最大范围选股"
_CFG = THRESHOLDS[VIEW_NAME]


def min_history(cfg: dict | None = None) -> int:
    """满足 250 日高点、50 日均线和 32 日 COUNT 所需的最少日线根数。"""
    c = cfg or _CFG
    return max(int(c["高点窗口"]), max(c["均线周期"]), int(c["大阳窗口"]))


def _is_finance3_two_equivalent(code: str, cfg: dict) -> bool:
    """按用户通达信版本口径：FINANCE(3)!=2 排除北交所。"""
    return any(str(code).startswith(prefix) for prefix in cfg["排除北交所前缀"])


def signal_latest(kdf: pd.DataFrame, code: str = "", cfg: dict | None = None) -> dict:
    """以最后一根日 K 判定原公式，并返回可展示的逐项依据。

    HHV/MA/COUNT 都包含当日，与通达信默认序列函数口径一致。C<=HHV*1.1
    在 HHV 包含当日时恒成立，仍保留该项以忠实还原原公式。
    """
    c = cfg or _CFG
    need = min_history(c)
    if kdf is None or len(kdf) < need:
        return {"SELECT": False, "原因": f"历史不足({0 if kdf is None else len(kdf)}<{need})"}
    required = {"close", "low"}
    if not required.issubset(kdf.columns):
        return {"SELECT": False, "原因": f"K线缺列:{','.join(sorted(required - set(kdf.columns)))}"}

    close = pd.to_numeric(kdf["close"], errors="coerce").reset_index(drop=True)
    low = pd.to_numeric(kdf["low"], errors="coerce").reset_index(drop=True)
    if close.iloc[-need:].isna().any() or pd.isna(low.iloc[-1]):
        return {"SELECT": False, "原因": "K线存在空值"}

    win = int(c["高点窗口"])
    hhv = float(close.iloc[-win:].max())
    now = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    mas = {p: float(close.iloc[-p:].mean()) for p in c["均线周期"]}
    up_window = int(c["大阳窗口"])
    surge_count = int((close.iloc[-up_window:] > close.shift(1).iloc[-up_window:] *
                       (1 + float(c["单日上涨阈值"]))).sum())
    retrace = (prev / now - 1) if now else float("inf")

    checks = {
        "距250日高点": bool(now / hhv >= float(c["距高点下限"])),
        "不高于250日高点110%": bool(now <= hhv * float(c["距高点上限"])),
        "站上MA10": bool(now > mas[10]),
        "站上MA20": bool(now > mas[20]),
        "站上MA50": bool(now > mas[50]),
        "32日内至少一次涨超6%": bool(surge_count >= 1),
        "当日回撤不超过4%": bool(retrace <= float(c["最大单日回撤"])),
        "非北交所(FINANCE(3)!=2)": not _is_finance3_two_equivalent(code, c),
        "收盘高于最低价": bool(now > float(low.iloc[-1])),
    }
    return {
        "SELECT": bool(all(checks.values())),
        "明细": {
            "close": round(now, 4), "low": round(float(low.iloc[-1]), 4),
            "HHV250": round(hhv, 4), "距250日高点%": round(now / hhv * 100, 2),
            "MA": {str(p): round(v, 4) for p, v in mas.items()},
            "32日涨超6%次数": surge_count, "当日回撤%": round(retrace * 100, 2),
            "checks": checks,
        },
    }


def signal_at(kdf: pd.DataFrame, pos: int, code: str = "", cfg: dict | None = None) -> dict:
    """在指定行判定，历史回放只读取该日及其之前的 K 线。"""
    return signal_latest(kdf.iloc[:pos + 1], code, cfg)


def backfill_max_range(codes: list[str], start: str, end: str, scope: str = "全A") -> int:
    """按主档已有交易日回放并写入每日横截面快照。"""
    # 流式按股票读取，避免把 5,000 多份 parquet 同时放进内存。
    selected_by_day, eligible_by_day = {}, {}
    for nth, code in enumerate(codes, 1):
        if _is_finance3_two_equivalent(code, _CFG):
            continue
        try:
            df = market.load_kline(code).reset_index(drop=True)
        except FileNotFoundError:
            continue
        for pos, raw_day in enumerate(df.date):
            day = pd.Timestamp(raw_day).strftime("%Y-%m-%d")
            if day < start or day > end:
                continue
            r = signal_at(df, pos, code)
            if str(r.get("原因", "")).startswith("历史不足"):
                continue
            eligible_by_day[day] = eligible_by_day.get(day, 0) + 1
            if r.get("SELECT"):
                selected_by_day.setdefault(day, []).append({"code": code, "明细": r["明细"]})
        if nth % 500 == 0:
            logger.info("最大范围历史回放 %d/%d", nth, len(codes))
    dates = sorted(eligible_by_day)
    for day in dates:
        selected, eligible = selected_by_day.get(day, []), eligible_by_day[day]
        ratio = len(selected) / eligible if eligible else 0.0
        store.put_view(VIEW_NAME, {"as_of": day, "策略": VIEW_NAME, "范围": scope,
            "扫描数": len(codes), "有效样本": eligible, "跳过数(历史不足)": len(codes)-eligible,
            "入选数": len(selected), "占比": round(ratio, 6), "占比%": round(ratio*100, 2),
            "行情最新日期": day, "入选清单": selected,
            "口径": "全 A 历史回放；分母为至少250根日线且非北交所的有效股票。"}, date=day)
    return len(dates)


def run_max_range_screen(codes: list[str], as_of: str | None = None,
                         fetch: bool = False, scope: str = "全A") -> dict:
    """扫描全 A（或传入子集），写入每日最大范围选股 view。"""
    if as_of:
        store.set_active_date(as_of)
    selected: list[dict] = []
    eligible = skipped = 0
    latest_bar_dates: list[str] = []
    for code in codes:
        try:
            kdf = market.load_kline(code)
        except FileNotFoundError:
            kdf = market.fetch_kline([code]).get(code) if fetch else None
        if kdf is None or len(kdf) < min_history():
            skipped += 1
            continue
        r = signal_latest(kdf, code)
        # 用户口径下，北交所不进入分母；历史不足同样不进入有效样本。
        if _is_finance3_two_equivalent(code, _CFG):
            continue
        eligible += 1
        if "date" in kdf.columns and len(kdf):
            latest_bar_dates.append(str(pd.Timestamp(kdf["date"].iloc[-1]).date()))
        if r.get("SELECT"):
            selected.append({"code": code, "明细": r["明细"]})

    ratio = len(selected) / eligible if eligible else 0.0
    view = {
        "as_of": as_of,
        "策略": VIEW_NAME, "范围": scope,
        "扫描数": len(codes), "有效样本": eligible, "跳过数(历史不足)": skipped,
        "入选数": len(selected), "占比": round(ratio, 6), "占比%": round(ratio * 100, 2),
        "行情最新日期": max(latest_bar_dates) if latest_bar_dates else None,
        "入选清单": selected,
        "公式": "C/HHV(C,250)>=0.82 AND C<=HHV(C,250)*1.1 AND C>MA(C,10/20/50) AND "
                "COUNT(C>REF(C,1)*1.06,32)>=1 AND REF(C,1)/C-1<=0.04 AND "
                "FINANCE(3)!=2 AND C>LOW",
        "口径": "分母=有至少250根日线且非北交所的有效股票；FINANCE(3)!=2 按用户口径排除北交所。",
    }
    p = store.put_view(VIEW_NAME, view, date=as_of)
    logger.info("最大范围选股:扫描 %d / 有效 %d / 跳过(历史不足)%d / 入选 %d / 占比 %.2f%% → %s",
                len(codes), eligible, skipped, len(selected), ratio * 100, p)
    return view
