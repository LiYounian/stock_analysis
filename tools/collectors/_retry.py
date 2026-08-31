"""高频数据接口的瞬时错误重试(东财 push2*/datacenter、百度 pae 等)。

东财对 IP 有连接层限流,单次请求偶发 curl(56) 连接重置 / RemoteDisconnected / 超时。
`retry_call` 对**瞬时网络错误**指数退避重试(默认 3 次:1s/2s/4s + jitter),非网络错误
(如空数据 ValueError)不重试、原样抛。全挂的冷却窗口重试也救不了(退避后仍抛最后一次)。

用法:
    from tools.collectors._retry import retry_call
    js = retry_call(_http_get, secid, label=f"资金流{code}")
"""
from __future__ import annotations

import logging
import os
import random
import time

logger = logging.getLogger("collectors.retry")

# 默认重试次数/退避;可用环境变量覆盖(不改代码调激进/保守)
FETCH_RETRY = int(os.getenv("FETCH_RETRY", "3"))
FETCH_RETRY_BASE = float(os.getenv("FETCH_RETRY_BASE", "1.0"))

# 瞬时(可重试)错误特征串(curl_cffi/requests/urllib3 各家措辞,小写匹配)
_TRANSIENT = (
    "curl: (56)",           # connection closed abruptly
    "curl: (28)",           # timeout
    "curl: (52)",           # empty reply
    "curl: (35)", "curl: (7)",   # tls/connect
    "connection closed", "connection aborted", "connection reset",
    "remotedisconnected", "remote end closed",
    "timed out", "timeout", "max retries", "temporarily",
)


def is_transient(e: Exception) -> bool:
    """异常是否为可重试的瞬时网络错误(据类型名+消息串启发式判断)。"""
    s = f"{type(e).__name__} {e}".lower()
    return any(t in s for t in _TRANSIENT)


def retry_call(fn, *args, attempts: int | None = None, base_delay: float | None = None,
               max_delay: float = 8.0, label: str = "", **kwargs):
    """调用 fn(*args, **kwargs);**瞬时网络错误**→ 指数退避重试(1s/2s/4s… + jitter)。

    非瞬时错误(空数据/逻辑错)不重试、原样抛。重试耗尽 → 抛最后一次异常。
    attempts/base_delay 缺省取 FETCH_RETRY / FETCH_RETRY_BASE(可环境变量覆盖)。
    """
    attempts = FETCH_RETRY if attempts is None else attempts
    base_delay = FETCH_RETRY_BASE if base_delay is None else base_delay
    last: Exception | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:                       # noqa: BLE001
            last = e
            if i >= attempts or not is_transient(e):
                raise
            delay = min(base_delay * (2 ** (i - 1)), max_delay)
            delay += random.uniform(0, delay * 0.3)  # jitter 防同步重试打同一冷却窗
            logger.info("%s 瞬时错误,退避 %.1fs 后重试 %d/%d: %s",
                        label or getattr(fn, "__name__", "call"), delay, i, attempts, e)
            time.sleep(delay)
    raise last                                       # 理论不达(耗尽在循环内已 raise)
