"""分时资金流采集(1min/5min · 主力/超大单/大单/中单/小单 净流入)。

数据源:东财 `push2his.eastmoney.com/api/qt/stock/fflow/kline/get`(klt=1 或 5),
用 curl_cffi 伪装 chrome TLS 指纹绕过 JA3 反爬(与 `fundflow.py` 同套路,见问题台账 B2)。
本机实测(2026-09-07,见 docs/每日分析_午盘Q/_数据源实测.md):
    push2his fflow/kline    ✅ 通(HTTP 200,交易日盘中返 klines)
    push2his kline           ❌ curl(56)  (价格家族被墙)
    push2 clist              ❌ curl(56)  (全 A 列表家族被墙)
    → 故本采集器仅拉资金流,不做全 A 排名/快照。

用途:午盘 Q · Q3 资金流前瞻策略的核心数据源。**只对候选池(≤50 只)拉**,不做全 A。

落盘:走 store 层(kind="fundflow_intraday",parquet),分区 date/code(见 `_RAW_KINDS`)。
契约见 docs/每日分析_午盘Q/M1_契约稿.md §三。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Literal

import pandas as pd

from tools.config import settings
from tools.store import repo as store

logger = logging.getLogger("collectors.fundflow_intraday")

_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))    # 被墙机快速失败降级(curl_cffi 单独传参)
_SOURCE = "eastmoney:fflow/kline"                     # 与日线 fundflow 区分
_FF_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get"
# fflow/kline 的 klines 字段顺序(东财固定):时间,主力,小单,中单,大单,超大单,主力占比...
_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
# 解析后列名(取前 7 项:时间 + 5 类净额 + 主力占比,单位:元 / 百分比)
_COLS = ["time", "主力净流入", "小单净流入", "中单净流入", "大单净流入",
         "超大单净流入", "主力净占比"]

FREQ_5MIN: Literal[5] = 5
FREQ_1MIN: Literal[1] = 1
_VALID_FREQ = (FREQ_1MIN, FREQ_5MIN)

MAX_BATCH_CODES = 50                                  # 候选池上限(超过说明用法错了,拦下)

_AS_OF_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")   # HH:MM 严格校验


def _secid(code: str) -> str:
    """代码 → 东财 secid(与 fundflow.py 同规则,保持单一真源)。

    本函数刻意与 fundflow.py._secid 保持行为一致(而非 import 复用)——因为 fundflow.py
    的 _secid 是模块私有,跨模块引私有函数违反宪法边界;若两处日后想统一,应上提到
    tools.config.exchange 里作真源(现规则同 fundflow.py 注释:920 段存疑但未实证)。
    """
    from tools.config import stock_pool
    if stock_pool.is_hk(code):
        return f"116.{code}"
    return f"1.{code}" if code[0] in ("6", "9") else f"0.{code}"


def _http_get(secid: str, klt: int) -> dict:
    """curl_cffi 伪装 chrome 拉东财分时资金流 JSON。抽出便于测试 mock。"""
    from curl_cffi import requests as creq

    params = {"lmt": "0", "klt": str(klt), "secid": secid,
              "fields1": "f1,f2,f3,f7", "fields2": _FIELDS2}
    r = creq.get(_FF_URL, params=params, impersonate="chrome", timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _parse(js: dict) -> pd.DataFrame:
    """把东财 klines 字符串数组解析成 DataFrame。列见 _COLS。

    - klines 时间是 "YYYY-MM-DD HH:MM" 字符串 → pd.Timestamp
    - 数值列缺失/非数 → NaN(不静默填 0,同 fundflow.py._parse 口径)
    - 空 klines → 空 DataFrame(空判由 fetch_one 抛错)
    """
    klines = (js.get("data") or {}).get("klines") or []
    rows = []
    for line in klines:
        parts = line.split(",")
        rec = {"time": parts[0]}
        for i, col in enumerate(_COLS[1:], start=1):
            try:
                rec[col] = float(parts[i])
            except (ValueError, IndexError):
                rec[col] = float("nan")
        rows.append(rec)
    df = pd.DataFrame(rows, columns=_COLS)
    if not df.empty:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)
    return df


def _truncate_by_as_of(df: pd.DataFrame, as_of: str | None) -> pd.DataFrame:
    """按 HH:MM 截断当日分时(防未来函数)。跨日行不受影响。

    比较口径:同一日期内,只保留 time.strftime("%H:%M") <= as_of 的行。
    df.time 已是 pd.Timestamp(见 _parse)。
    """
    if not as_of or df.empty:
        return df
    hh, mm = as_of.split(":")
    cutoff_minutes = int(hh) * 60 + int(mm)
    row_minutes = df["time"].dt.hour * 60 + df["time"].dt.minute
    return df[row_minutes <= cutoff_minutes].reset_index(drop=True)


def fetch_one(
    code: str,
    freq: int = FREQ_5MIN,
    as_of: str | None = None,
) -> pd.DataFrame:
    """拉单票分时资金流(不落盘)。契约见模块 docstring。

    异常:
        - freq 非 1/5 → ValueError
        - as_of 非 HH:MM → ValueError
        - 网络失败 → 走 retry_call 后仍败 → 抛
        - 空数据 → ValueError(不返回空 df 伪装成功)
    """
    if freq not in _VALID_FREQ:
        raise ValueError(f"freq 必须 ∈ {_VALID_FREQ},收到 {freq!r}")
    if as_of is not None and not _AS_OF_RE.match(as_of):
        raise ValueError(f"as_of 必须 HH:MM 格式,收到 {as_of!r}")

    from tools.collectors._retry import retry_call
    df = _parse(retry_call(_http_get, _secid(code), freq,
                            label=f"分时资金流{code}(klt={freq})"))
    if df.empty:
        raise ValueError(f"{code} 分时资金流为空(接口异常/代码错/非交易日)")
    return _truncate_by_as_of(df, as_of)


def collect(
    codes: list[str],
    freq: int = FREQ_5MIN,
    as_of: str | None = None,
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """批量拉候选池,单票失败进 errors,不炸整批。契约见模块 docstring。

    副作用:每只成功的票落盘 store.put_raw("fundflow_intraday", code, df, meta={
        "source": _SOURCE, "freq_min": freq, "as_of": as_of,
    })
    """
    if len(codes) > MAX_BATCH_CODES:
        raise ValueError(
            f"codes 数量 {len(codes)} 超 MAX_BATCH_CODES={MAX_BATCH_CODES}"
            "(本采集器仅对候选池,不做全A;要拉全A请另想办法)"
        )
    settings.ensure_dirs()
    out: dict[str, pd.DataFrame] = {}
    errors: list[dict] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 分时资金流 %s 采集(freq=%d,as_of=%s)...", i, n, code, freq, as_of)
        try:
            df = fetch_one(code, freq=freq, as_of=as_of)
            store.put_raw("fundflow_intraday", code, df,
                          meta={"source": _SOURCE, "freq_min": freq, "as_of": as_of})
            out[code] = df
            logger.info("分时资金流 %s:%d 行", code, len(df))
        except Exception as e:
            errors.append({"code": code, "reason": f"{type(e).__name__}: {e}"})
            logger.error("分时资金流 %s 失败: %s", code, e)
        time.sleep(settings.FETCH_SLEEP_SEC)
    if errors:
        logger.warning("分时资金流拉取失败 %d/%d: %s",
                       len(errors), n, [e["code"] for e in errors])
    return out, errors


def load_intraday(code: str, date: str | None = "latest") -> pd.DataFrame:
    """从本地缓存读单票分时资金流。缓存缺失抛 FileNotFoundError(同 fundflow.py.load_fundflow)。"""
    return store.get_raw("fundflow_intraday", code, date=date)
