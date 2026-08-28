"""主力资金行为采集:龙虎榜 + 大宗交易 + 股东户数。

借鉴 a-stock-data §3.5/4.2/4.3。相比 fundflow 的分钟级净流入,这三类是**主力行为的直接痕迹**,
解释力更强:
  - 龙虎榜(lhb)       个股上榜记录:净买额、买卖席位强度、上榜原因。游资/机构动向。
  - 大宗交易(block)   折溢价、成交额、买卖营业部。大额换手与机构接盘/出货。
  - 股东户数(holder)  季度户数与环比:持续减少=筹码集中(主力吸筹),激增=散户化。

数据源:东财 datacenter-web(经 akshare 封装,字段稳定、免自己拼 RPT 表)。
  - 龙虎榜/大宗:一次拉**全市场区间明细**再按代码切片(单请求覆盖整批,省网络);
  - 股东户数:逐票拉详情(akshare 无全市场快照接口)。
所有列名按 akshare 现行输出做**防御式取数**(列名漂移只降级该字段,不炸整批)。
港股无此三类数据 → 直接落空(降级)。
落盘:走 store 层,kind ∈ {"lhb","block_trade","holder_num"}(json),旁记 meta.source="eastmoney"。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.smart_money")

_SOURCE = "eastmoney"


def _f(v):
    """尽力转 float,失败/缺失 → None。"""
    try:
        x = float(v)
        return None if pd.isna(x) else x
    except (TypeError, ValueError):
        return None


def _pick(row: pd.Series, *names, cast=None):
    """从一行里按候选列名(容错 akshare 列名漂移)取首个存在且非空的值。"""
    for nm in names:
        if nm in row.index and pd.notna(row[nm]):
            v = row[nm]
            return cast(v) if cast else v
    return None


# ————————————————————————————————————————————————
# 龙虎榜
# ————————————————————————————————————————————————
def _fetch_lhb_market(start: str, end: str) -> pd.DataFrame:
    """东财龙虎榜区间明细(全市场)。返回原始 df(含代码列),失败抛错。"""
    import akshare as ak
    df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
    if df is None or len(df) == 0:
        raise ValueError("龙虎榜区间明细为空")
    return df


def _lhb_rows_of(df: pd.DataFrame, code: str) -> list[dict]:
    """从全市场龙虎榜切出某代码的上榜记录(按上榜日倒序)。"""
    col_code = next((c for c in ("代码", "股票代码", "证券代码") if c in df.columns), None)
    if not col_code:
        return []
    sub = df[df[col_code].astype(str).str.zfill(6) == code]
    items = []
    for _, r in sub.iterrows():
        items.append({
            "date": str(_pick(r, "上榜日", "交易日期", "上榜日期") or "")[:10],
            "reason": _pick(r, "上榜原因", "解读"),
            "net_buy": _f(_pick(r, "龙虎榜净买额", "净买额")),
            "buy": _f(_pick(r, "龙虎榜买入额", "买入额")),
            "sell": _f(_pick(r, "龙虎榜卖出额", "卖出额")),
            "turnover_rate": _f(_pick(r, "换手率")),
            "pct_chg": _f(_pick(r, "涨跌幅")),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ————————————————————————————————————————————————
# 大宗交易
# ————————————————————————————————————————————————
def _fetch_block_market(start: str, end: str) -> pd.DataFrame:
    """东财大宗交易每日明细(全市场 A股)。返回原始 df,失败抛错。"""
    import akshare as ak
    df = ak.stock_dzjy_mrmx(symbol="A股", start_date=start, end_date=end)
    if df is None or len(df) == 0:
        raise ValueError("大宗交易明细为空")
    return df


def _block_rows_of(df: pd.DataFrame, code: str) -> list[dict]:
    """从全市场大宗交易切出某代码记录(按交易日倒序)。"""
    col_code = next((c for c in ("证券代码", "代码") if c in df.columns), None)
    if not col_code:
        return []
    sub = df[df[col_code].astype(str).str.zfill(6) == code]
    items = []
    for _, r in sub.iterrows():
        items.append({
            "date": str(_pick(r, "交易日期") or "")[:10],
            "price": _f(_pick(r, "成交价", "成交价格")),
            "volume": _f(_pick(r, "成交量")),
            "amount": _f(_pick(r, "成交额")),
            "premium_rate": _f(_pick(r, "折溢率", "溢价率")),
            "buyer": _pick(r, "买方营业部"),
            "seller": _pick(r, "卖方营业部"),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


# ————————————————————————————————————————————————
# 股东户数
# ————————————————————————————————————————————————
def _fetch_holder_num(code: str) -> list[dict]:
    """东财某票股东户数详情(按统计截止日倒序取近若干期)。空/失败抛错。"""
    import akshare as ak
    df = ak.stock_zh_a_gdhs_detail_em(symbol=code)
    if df is None or len(df) == 0:
        raise ValueError("股东户数为空")
    items = []
    for _, r in df.iterrows():
        items.append({
            "date": str(_pick(r, "股东户数统计截止日", "截止日") or "")[:10],
            "holders": _f(_pick(r, "股东户数-本次", "股东户数")),
            "holders_prev": _f(_pick(r, "股东户数-上次")),
            "change_ratio": _f(_pick(r, "股东户数-增减比例", "股东户数-增减")),
            "avg_hold_value": _f(_pick(r, "户均持股市值")),
            "pct_chg_range": _f(_pick(r, "区间涨跌幅")),
        })
    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def summarize_holder(items: list[dict]) -> dict:
    """派生股东户数趋势:最新户数、环比、连续减少期数(筹码集中信号)。"""
    if not items:
        return {"最新股东户数": None, "户数环比": None, "连续减少期数": 0}
    streak = 0
    for it in items:                       # items 已按日期倒序
        cr = it.get("change_ratio")
        if cr is not None and cr < 0:
            streak += 1
        else:
            break
    return {
        "最新股东户数": items[0].get("holders"),
        "户数环比": items[0].get("change_ratio"),
        "连续减少期数": streak,
    }


# ————————————————————————————————————————————————
# 编排
# ————————————————————————————————————————————————
def fetch_smart_money(codes: list[str], days: int | None = None) -> dict[str, dict]:
    """采集龙虎榜/大宗/股东户数并落盘。返回 {code: {"lhb":n,"block":n,"holder":n}} 计数概览。

    龙虎榜/大宗走**一次全市场区间拉取 + 按代码切片**;股东户数逐票拉。
    任一源整体失败只降级该源(记 warning),不阻断其余;港股整体落空。
    """
    from tools.config import stock_pool

    settings.ensure_dirs()
    days = days or 90              # 龙虎榜/大宗默认回看 90 天(季度窗口,兼顾上榜稀疏)
    start = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y%m%d")
    end = pd.Timestamp.today().strftime("%Y%m%d")
    a_codes = [c for c in codes if not stock_pool.is_hk(c)]

    # 全市场区间明细(best-effort,失败则该源整体降级为空)
    lhb_all = _safe_market(_fetch_lhb_market, start, end, name="龙虎榜")
    block_all = _safe_market(_fetch_block_market, start, end, name="大宗交易")

    out: dict[str, dict] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 主力行为 %s 采集...", i, n, code)
        if stock_pool.is_hk(code):         # 港股无此三类,统一落空降级
            for kind in ("lhb", "block_trade", "holder_num"):
                store.put_raw(kind, code, [], meta={"source": "none(hk)"})
            out[code] = {"lhb": 0, "block": 0, "holder": 0}
            continue
        try:
            lhb = _lhb_rows_of(lhb_all, code) if lhb_all is not None else []
            block = _block_rows_of(block_all, code) if block_all is not None else []
            try:
                holder = _fetch_holder_num(code)
            except Exception as e:         # 股东户数逐票,单票失败只降级该项
                logger.debug("股东户数 %s 失败: %s", code, e)
                holder = []
            store.put_raw("lhb", code, lhb, meta={"source": _SOURCE})
            store.put_raw("block_trade", code, block, meta={"source": _SOURCE})
            store.put_raw("holder_num", code, holder,
                          meta={"source": _SOURCE, **summarize_holder(holder)})
            out[code] = {"lhb": len(lhb), "block": len(block), "holder": len(holder)}
            logger.info("主力行为 %s:龙虎榜 %d / 大宗 %d / 户数 %d 期",
                        code, len(lhb), len(block), len(holder))
        except Exception as e:
            failed.append(code)
            logger.error("主力行为 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("主力行为拉取失败(%d): %s", len(failed), failed)
    return out


def _safe_market(fn, start: str, end: str, name: str) -> pd.DataFrame | None:
    """全市场区间拉取兜底:失败记 warning 返 None(该源整体降级,不炸整批)。"""
    try:
        df = fn(start, end)
        logger.info("%s 全市场区间明细:%d 行", name, len(df))
        return df
    except Exception as e:
        logger.warning("%s 全市场拉取失败(该源整体降级): %s", name, e)
        return None


def load_lhb(code: str) -> list[dict]:
    """读单票龙虎榜。缺失抛 FileNotFoundError。"""
    return store.get_raw("lhb", code)


def load_block_trade(code: str) -> list[dict]:
    """读单票大宗交易。缺失抛 FileNotFoundError。"""
    return store.get_raw("block_trade", code)


def load_holder_num(code: str) -> list[dict]:
    """读单票股东户数。缺失抛 FileNotFoundError。"""
    return store.get_raw("holder_num", code)
