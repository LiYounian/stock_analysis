"""资金流向采集(主力/超大单/大单/中单/小单 净流入)。

数据源:东财 `push2his.eastmoney.com/api/qt/stock/fflow/daykline/get`,
用 curl_cffi 伪装 chrome TLS 指纹绕过 JA3 反爬(见问题台账 B2)。
落盘:走 store 层(kind="fundflow",parquet),旁记 meta.source="eastmoney"。
契约见 docs/计划/P3_Web展示与预测引擎.md P3-A。
"""
from __future__ import annotations

import logging
import os
import time

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.fundflow")

_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))  # 被墙机快速失败降级(curl_cffi 走 libcurl,单独传参)
_SOURCE = "eastmoney"  # 东财
_FF_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
# fflow/daykline 的 klines 字段顺序(东财固定):日期,主力,小单,中单,大单,超大单,主力占比...
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
# 解析后列名(取前 7 项:日期 + 5 类净额 + 主力占比)
_COLS = ["date", "主力净流入", "小单净流入", "中单净流入", "大单净流入",
         "超大单净流入", "主力净占比"]


def _secid(code: str) -> str:
    """代码 → 东财 secid。沪(6/9)= 1.code;深/京(0/2/3/4/8)= 0.code;港股(5位)= 116.code。"""
    from tools.config import stock_pool
    if stock_pool.is_hk(code):
        return f"116.{code}"
    return f"1.{code}" if code[0] in ("6", "9") else f"0.{code}"


def _http_get(secid: str) -> dict:
    """curl_cffi 伪装 chrome 拉东财资金流 JSON。抽出便于测试 mock。"""
    from curl_cffi import requests as creq

    params = {"lmt": "0", "klt": "101", "secid": secid,
              "fields1": "f1,f2,f3,f7", "fields2": _FIELDS2}
    r = creq.get(_FF_URL, params=params, impersonate="chrome", timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _parse(js: dict) -> pd.DataFrame:
    """把东财返回的 klines 字符串数组解析成 DataFrame。"""
    klines = (js.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        rec = {"date": parts[0]}
        for i, col in enumerate(_COLS[1:], start=1):
            try:
                rec[col] = float(parts[i])
            except (ValueError, IndexError):
                rec[col] = float("nan")
        rows.append(rec)
    df = pd.DataFrame(rows, columns=_COLS)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
    return df


def fetch_one(code: str, days: int | None = None) -> pd.DataFrame:
    """拉单票资金流(不落盘)。空数据抛错,不返回空 df 伪装成功。

    东财 push2his 对 IP 有连接层限流,单次偶发 curl(56)/RemoteDisconnected → 走 retry_call
    对**瞬时网络错误**指数退避重试(默认 3 次);空数据(ValueError)不重试、原样抛。
    """
    from tools.collectors._retry import retry_call
    df = _parse(retry_call(_http_get, _secid(code), label=f"资金流{code}"))
    if df.empty:
        raise ValueError(f"{code} 资金流为空(接口异常/代码错)")
    return df.tail(days).reset_index(drop=True) if days else df


def fetch_fundflow(codes: list[str], days: int | None = None) -> dict[str, pd.DataFrame]:
    """拉取多票资金流并落盘。单票失败记 logger 并跳过,不中断整批。"""
    settings.ensure_dirs()
    out: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 资金流 %s 采集...", i, n, code)
        try:
            df = fetch_one(code, days)
            store.put_raw("fundflow", code, df, meta={"source": _SOURCE})
            out[code] = df
            logger.info("资金流 %s:%d 天", code, len(df))
        except Exception as e:
            failed.append(code)
            logger.error("资金流 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if failed:
        logger.warning("资金流拉取失败(%d): %s", len(failed), failed)
    return out


def load_fundflow(code: str) -> pd.DataFrame:
    """从本地缓存读单票资金流。缓存缺失抛 FileNotFoundError。"""
    return store.get_raw("fundflow", code)


def summarize(df: pd.DataFrame) -> dict:
    """派生资金流摘要:今日主力净流入/占比、近5日主力合计、主力连续净流入天数。"""
    if df is None or df.empty:
        return {"今日主力净流入": None, "今日主力净占比": None,
                "近5日主力合计": None, "主力连续净流入天数": 0}
    zhu = df["主力净流入"]
    last = df.iloc[-1]
    # 从最后一天往前数连续 >0 的天数
    streak = 0
    for v in reversed(zhu.tolist()):
        if pd.notna(v) and v > 0:
            streak += 1
        else:
            break

    def _f(x, nd=0):
        return None if pd.isna(x) else round(float(x), nd)

    return {
        "今日主力净流入": _f(last["主力净流入"]),
        "今日主力净占比": _f(last["主力净占比"], 2),
        "近5日主力合计": _f(zhu.tail(5).sum()),
        "主力连续净流入天数": streak,
    }
