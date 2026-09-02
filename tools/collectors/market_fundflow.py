"""市场级资金流采集(SSE 两融总额)—— 大盘预测 v1「资金流」维的真实数据源。

需求:docs/计划/大盘预测策略.md §3(四维:技术+广度+消息面+资金流)、§4 v1(补真资金流)。

===== 数据源选型(本机可得性实测,2026-09-02) =====
本机东财 push2/push2his 资金流接口被墙(stock_market_fund_flow → RemoteDisconnected),
故**不走东财**。实测可用的市场级资金流权威口径:
  · ✅ akshare stock_margin_sse(start_date, end_date) —— 上交所**市场级两融总额**日序列,
       一次调用回溯 2022(~1100+ 交易日),交易所披露口径,非东财、未被墙。字段:
       信用交易日期/融资余额/融资买入额/融券余量/融券余量金额/融券卖出量/融资融券余额(单位:元)。
  · ✗ stock_market_fund_flow(大盘主力净流入)—— 东财,被墙,弃用。
  · ✗ 北向资金净买(stock_hsgt_hist_em)—— 2024-08-16 起停止披露(近端恒 NaN),
       训练/服务口径不一致(旧样本有、当下无),不宜作生产因子,弃用(仅备注)。

选定 **SSE 市场级两融** 作资金流维:融资余额=杠杆资金存量、融资买入额=杠杆资金增量,
连续覆盖 2022→今、生产当日可得(盘后披露),与代理指数(回溯2018)/沪深300 回测窗口相容。

===== 防未来函数(红线) =====
两融为**盘后披露**(交易日 T 的两融数据在 T 收盘后才公开)。本采集器只搬运披露值、按
交易日 T 记 date;**滞后处理由消费方(analysis.market_forecast.fundflow)负责**:某 as_of=T
的资金流特征只用 date ≤ T−1(严格早于 T)的两融,绝不用 T 当日(收盘后才出)。

===== 落盘契约 =====
- 缓存到 <data_root>/raw/market_fundflow/sse_margin.parquet(raw/ 已 gitignore,不入库)。
- 幂等 + 前向增量并集:同 date 去重合并(新覆盖旧),按 date 升序。重跑不产重复。
- 优雅降级:限流/非200/空/结构漂移 → 保留旧缓存、返回已有,不中断。

⚠️ 非投资建议;两融披露数据仅供研究。
"""
from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger("collectors.market_fundflow")

_SOURCE = "akshare:stock_margin_sse"
# 标准化后的列(单位统一为元)
COLS = ["date", "rz_bal", "rz_buy", "rzrq_bal"]


def _norm_date(d) -> str:
    """'YYYYMMDD' / 'YYYY-MM-DD' / date → 'YYYY-MM-DD'。"""
    s = str(d).strip().replace("-", "").replace("/", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(d)[:10]


def _to_float(v):
    try:
        if v is None or v == "" or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def cache_path(data_root: Path) -> Path:
    """市场级两融缓存 parquet 路径。"""
    return Path(data_root) / "raw" / "market_fundflow" / "sse_margin.parquet"


def fetch_sse_margin(start: str, end: str) -> pd.DataFrame:
    """拉 [start,end] 上交所市场级两融日序列 → 归一 DataFrame(COLS,date 升序)。

    融资余额=rz_bal(杠杆存量)、融资买入额=rz_buy(杠杆增量)、融资融券余额=rzrq_bal。
    空/失败 → 空 DataFrame(不抛,交由 collect 降级)。
    """
    import akshare as ak
    s, e = start.replace("-", ""), end.replace("-", "")
    df = ak.stock_margin_sse(start_date=s, end_date=e)
    if df is None or df.empty:
        return pd.DataFrame(columns=COLS)
    rows = []
    for r in df.to_dict("records"):
        d = _norm_date(r.get("信用交易日期"))
        rows.append({
            "date": d,
            "rz_bal": _to_float(r.get("融资余额")),
            "rz_buy": _to_float(r.get("融资买入额")),
            "rzrq_bal": _to_float(r.get("融资融券余额")),
        })
    out = pd.DataFrame(rows, columns=COLS)
    out = out.dropna(subset=["date"]).drop_duplicates("date").sort_values("date")
    return out.reset_index(drop=True)


def _merge_incremental(new: pd.DataFrame, prev: pd.DataFrame) -> pd.DataFrame:
    """前向增量并集:同 date 去重(新覆盖旧),按 date 升序。"""
    if prev is None or prev.empty:
        merged = new
    elif new is None or new.empty:
        merged = prev
    else:
        merged = pd.concat([prev, new], ignore_index=True)
        merged = merged.drop_duplicates("date", keep="last")
    return merged.sort_values("date").reset_index(drop=True)


def _load_cache(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            return pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读缓存失败(视为空): %s", str(exc)[:120])
    return pd.DataFrame(columns=COLS)


def collect(start: str | None = None, end: str | None = None,
            data_root=None) -> pd.DataFrame:
    """采集市场级两融并写缓存(前向增量、幂等)。返回合并后全序列。

    · start 缺省 = 2022-01-01(SSE 两融 akshare 覆盖起点);end 缺省 = 今天。
    · 拉取失败/空 → 返回已有缓存(优雅降级),不覆盖旧数据。
    """
    from tools.analysis.market_forecast.dataroot import ensure_data_root
    root = ensure_data_root(str(data_root) if data_root else None)
    path = cache_path(root)
    prev = _load_cache(path)

    s = start or "20220101"
    e = end or date.today().strftime("%Y%m%d")
    try:
        new = fetch_sse_margin(s, e)
        logger.info("市场级两融拉取:%d 行(区间 %s..%s)", len(new), s, e)
    except Exception as exc:  # noqa: BLE001
        logger.warning("市场级两融拉取失败(降级用旧缓存): %s", str(exc)[:150])
        new = pd.DataFrame(columns=COLS)

    merged = _merge_incremental(new, prev)
    if merged.empty:
        logger.warning("市场级两融无数据(拉取空且无旧缓存),不落盘")
        return merged
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(path, index=False)
    logger.info("市场级两融落盘:%s(共 %d 行,%s..%s)", path, len(merged),
                merged["date"].iloc[0], merged["date"].iloc[-1])
    return merged


def _main():
    ap = argparse.ArgumentParser(description="采集 SSE 市场级两融(大盘预测资金流维)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD/YYYYMMDD(缺省2022-01-01)")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD/YYYYMMDD(缺省今天)")
    ap.add_argument("--data-root", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    df = collect(start=a.start, end=a.end, data_root=a.data_root)
    if not df.empty:
        print(df.tail(5).to_string(index=False))
        print(f"[done] {len(df)} 行,{df['date'].iloc[0]}..{df['date'].iloc[-1]}")


if __name__ == "__main__":
    _main()
