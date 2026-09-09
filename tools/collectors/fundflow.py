"""资金流向采集(主力/超大单/大单/中单/小单 净流入)。

数据源(三级 fallback,本机东财 TLS 墙时自动降级,见 docs/计划/资金流第二源接入设计.md):
  1. 东财 `push2his.eastmoney.com/.../fflow/daykline/get`(curl_cffi 伪装 chrome 绕 JA3)——
     全档多日历史,tier_history="full"。本机常被 TLS 墙(问题台账 B2)。
  2. 腾讯 `proxy.finance.qq.com/.../hsfundtab`(纯 requests,不吃墙)——今日五档 + 近20日主力净额,
     历史四档 NaN,tier_history="main_only"。
  3. 新浪 `MoneyFlow.ssl_qsfx_zjlrqs`——仅主力净额日序列(口径近似),五档 NaN,tier_history="main_only"。
落盘:走 store 层(kind="fundflow",parquet),旁记 meta.source=实际命中源 + meta.tier_history。
下游 `summarize()` 只用"主力净额日序列 + 今日五档",三源均满足;历史四档仅东财档全。
契约见 docs/计划/P3_Web展示与预测引擎.md P3-A + docs/计划/资金流第二源接入设计.md。
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
    """代码 → 东财 secid。沪(6/9)= 1.code;深/京(0/2/3/4/8)= 0.code;港股(5位)= 116.code。

    ⚠️ **疑似缺口,但本轮无实证故不改(2026-09-03)**:北交所现行 920 段按"9 开头"落到
    `1.`(沪市),疑应为 `0.`。本机 `push2his.eastmoney.com` 全部 `curl (56) Connection
    closed abruptly` —— 连对照组 `1.600000`/`0.000001` 也挂,是已知的东财路径墙
    (问题台账 B2),不是 secid 写错。**没实证不据猜改**;等东财路径可用后实测再定。
    注意 secid 的 `1./0.` 是东财市场编号,与 sh/sz/bj 并非一对一,不能直接套
    `tools.config.exchange` 的输出。
    """
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


def _to_float(v):
    """数值化;空串/非数/NaN → None(缺失就是缺失,不静默变 0)。"""
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _prefixed(code: str) -> str:
    """6 位代码 → 带交易所前缀符号(sh/sz/bj + code),腾讯/新浪资金流端点均用此格式。
    判据走 `tools.config.exchange` 单一真源;判不出回落 sz(端点对错票返空,上层按缺失处理)。"""
    from tools.config import exchange
    return f"{exchange.exchange_of(code) or 'sz'}{code}"


# —— 第二源:腾讯 proxy.finance(非东财,本机不吃 TLS 墙;今日五档 + 近20日主力净额)——
_TX_FF_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/fundflow/hsfundtab"
_TX_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"}


def _fetch_tencent(code: str) -> pd.DataFrame:
    """腾讯 hsfundtab → `_COLS` 帧:近 20 日主力净额(历史四档/占比 NaN)+ **今日五档**。

    腾讯历史仅给主力净流入(mainNetIn),五档拆分只在今日快照 todayFundFlow → 历史四档落 NaN
    (有声缺列,非 0)。今日行补五档并做**恒等式硬断言** `mainNetIn ≈ superFlow + bigFlow`
    (主力=超大+大,实测分毫不差),挡字段错位/单位错配。净额单位=元(与东财同)。空→空 df。
    """
    import requests

    r = requests.get(_TX_FF_URL, params={"code": _prefixed(code)},
                     headers=_TX_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != 0:
        raise ValueError(f"腾讯资金流 {code} code={js.get('code')} msg={js.get('msg')}")
    data = js.get("data") or {}
    hist = ((data.get("historyFundFlow") or {}).get("oneDayKlineList")) or []
    rows = [{"date": it.get("date"), "主力净流入": _to_float(it.get("mainNetIn")),
             "小单净流入": float("nan"), "中单净流入": float("nan"),
             "大单净流入": float("nan"), "超大单净流入": float("nan"),
             "主力净占比": float("nan")} for it in hist]
    df = pd.DataFrame(rows, columns=_COLS)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    # 今日行补五档(oneDayKlineList 末行即今日 = todayFundFlow)
    today = data.get("todayFundFlow") or {}
    main, sup, big = (_to_float(today.get(k)) for k in ("mainNetIn", "superFlow", "bigFlow"))
    if None not in (main, sup, big):
        if abs(main - (sup + big)) / max(abs(main), 1.0) > 1e-3:      # 恒等式硬断言
            raise ValueError(f"腾讯资金流 {code} 口径异常:main={main} ≠ super+big={sup + big}")
        i = df.index[-1]
        df.at[i, "主力净流入"] = main
        df.at[i, "超大单净流入"] = sup
        df.at[i, "大单净流入"] = big
        df.at[i, "中单净流入"] = _to_float(today.get("normalFlow"))
        df.at[i, "小单净流入"] = _to_float(today.get("smallFlow"))
    return df


# —— 第三源:新浪(仅主力净额 netamount 日序列,历史长;五档/占比 NaN,口径近似不可比)——
_SINA_FF_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
                "var%20FDFLOW=/MoneyFlow.ssl_qsfx_zjlrqs")
_SINA_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"}


def _fetch_sina(code: str) -> pd.DataFrame:
    """新浪 MoneyFlow → `_COLS` 帧:仅主力净额(netamount,元)日序列;五档/占比 NaN。

    新浪"主力"口径与东财/腾讯不同(降级代理,标 tier_history=main_only);主力净占比口径不可比 → NaN
    (宁缺勿凑)。返回 `var FDFLOW=([...])`(GBK),正则抠 JSON 数组。空→空 df。
    """
    import json
    import re

    import requests

    r = requests.get(_SINA_FF_URL, params={"daima": _prefixed(code)},
                     headers=_SINA_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    r.encoding = "gbk"
    m = re.search(r"var FDFLOW=\((.*)\)", r.text, re.S)
    if not m:
        raise ValueError(f"新浪资金流 {code} 返回结构异常")
    arr = json.loads(m.group(1))
    rows = [{"date": it.get("opendate"), "主力净流入": _to_float(it.get("netamount")),
             "小单净流入": float("nan"), "中单净流入": float("nan"),
             "大单净流入": float("nan"), "超大单净流入": float("nan"),
             "主力净占比": float("nan")} for it in arr if it.get("opendate")]
    df = pd.DataFrame(rows, columns=_COLS)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def _fetch_one_sourced(code: str, days: int | None = None) -> tuple[pd.DataFrame, str, str]:
    """三级 fallback:东财(全档多日历史)→ 腾讯 proxy(今日五档+近20日主力)→ 新浪(仅主力净额)。

    返回 `(df, source, tier_history)`:tier_history ∈ {"full", "main_only"}——供下游辨"历史四档可信否"。
    每源套 retry_call(瞬时网络错误退避);某源空/异常 → 记因回退下一源;三源全失败/空 → 抛(不返空 df)。
    """
    from tools.collectors._retry import retry_call

    def _trim(df):
        return df.tail(days).reset_index(drop=True) if days else df

    errors: list[str] = []
    for src, tier, fn, arg in (
        (_SOURCE, "full", lambda: _parse(retry_call(_http_get, _secid(code), label=f"资金流{code}")), None),
        ("tencent_proxy", "main_only", lambda: retry_call(_fetch_tencent, code, label=f"资金流腾讯{code}"), None),
        ("sina", "main_only", lambda: retry_call(_fetch_sina, code, label=f"资金流新浪{code}"), None),
    ):
        try:
            df = fn()
            if df is not None and not df.empty:
                return _trim(df), src, tier
            errors.append(f"{src}:空")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{src}:{type(e).__name__}")
    raise ValueError(f"{code} 资金流三源均失败/空: {errors}")


def fetch_one(code: str, days: int | None = None) -> pd.DataFrame:
    """拉单票资金流(不落盘)。三级 fallback(东财→腾讯proxy→新浪);全失败抛,不返空 df 伪装成功。"""
    df, _src, _tier = _fetch_one_sourced(code, days)
    return df


def fetch_fundflow(codes: list[str], days: int | None = None) -> dict[str, pd.DataFrame]:
    """拉取多票资金流并落盘。单票失败记 logger 并跳过,不中断整批。"""
    settings.ensure_dirs()
    out: dict[str, pd.DataFrame] = {}
    failed: list[str] = []
    n = len(codes)
    for i, code in enumerate(codes, 1):
        logger.info("[%d/%d] 资金流 %s 采集...", i, n, code)
        try:
            df, src, tier = _fetch_one_sourced(code, days)
            store.put_raw("fundflow", code, df, meta={"source": src, "tier_history": tier})
            out[code] = df
            logger.info("资金流 %s:%d 天(源 %s,tier %s)", code, len(df), src, tier)
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
