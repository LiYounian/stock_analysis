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

⚠️ 骨架期:函数体尚未实现(2026-09-07),仅锁 I/O 契约。M1 审阅后填实现。
"""
from __future__ import annotations

import logging
import os
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
    """curl_cffi 伪装 chrome 拉东财分时资金流 JSON。抽出便于测试 mock。

    klt: 1(1min) 或 5(5min)。其他值由调用方在 fetch_one 处校验后再进来。
    """
    raise NotImplementedError("M1 骨架:审阅后填实现(套路 = fundflow.py._http_get + klt 参)")


def _parse(js: dict) -> pd.DataFrame:
    """把东财 klines 字符串数组解析成 DataFrame。列见 _COLS。

    - klines 里的时间是 "YYYY-MM-DD HH:MM" 字符串,转 pd.Timestamp
    - 数值列缺失/非数 → NaN(不静默填 0,同 fundflow.py._parse 口径)
    - 空 klines → 返回列齐全的空 DataFrame(空判由 fetch_one 抛错)
    """
    raise NotImplementedError("M1 骨架:审阅后填实现(套路 = fundflow.py._parse + time 而非 date)")


def fetch_one(
    code: str,
    freq: int = FREQ_5MIN,
    as_of: str | None = None,
) -> pd.DataFrame:
    """拉单票分时资金流(不落盘)。

    参数:
        code:  6 位股票代码(A 股)
        freq:  1 或 5(分钟);默认 5min
        as_of: "HH:MM" 或 None
               · 生产 14:30 首判传 "14:30",14:50 复核传 "14:50"(未来函数红线)
               · None → 拿全天(仅回测/历史补录场景使用)

    返回:
        DataFrame 列 = _COLS。time 列为 pd.Timestamp,升序。
        - 单位:主力/大/中/小/超大单 = 元;主力净占比 = 百分比
        - as_of 生效时,`time.strftime("%H:%M") > as_of` 的行被剔除

    异常:
        - freq 非法 → ValueError
        - as_of 格式非 HH:MM → ValueError
        - 网络失败 → 走 retry_call 后仍败 → 抛(curl_cffi.CurlError / RemoteDisconnected 等)
        - 数据为空(接口返 klines=[]) → ValueError(不返回空 df 伪装成功)
    """
    raise NotImplementedError("M1 骨架:审阅后填实现")


def collect(
    codes: list[str],
    freq: int = FREQ_5MIN,
    as_of: str | None = None,
) -> tuple[dict[str, pd.DataFrame], list[dict]]:
    """批量拉候选池,单票失败进 errors,不炸整批。

    参数:
        codes:  候选池代码,长度 ≤ MAX_BATCH_CODES(超过抛 ValueError,防误用全 A)
        freq / as_of:  同 fetch_one

    返回:
        ({code: DataFrame}, [{"code": code, "reason": str}, ...])
        - out 只含成功的票
        - errors 包含所有失败原因(网络/空数据)
        - 批间走 settings.FETCH_SLEEP_SEC 节流,同 fundflow.py.fetch_fundflow

    副作用:
        每只成功的票落盘 store.put_raw("fundflow_intraday", code, df, meta={
            "source": _SOURCE, "freq_min": freq, "as_of": as_of,
        })
    """
    raise NotImplementedError("M1 骨架:审阅后填实现")


def load_intraday(code: str, date: str | None = "latest") -> pd.DataFrame:
    """从本地缓存读单票分时资金流。缓存缺失抛 FileNotFoundError(同 fundflow.py.load_fundflow)。"""
    raise NotImplementedError("M1 骨架:审阅后填实现(单行 return store.get_raw)")
