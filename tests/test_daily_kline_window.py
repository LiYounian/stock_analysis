"""日筛「近史加载」护栏 · 语义锁测试。

为什么:主档回补到多年后(供回测),日筛若读全历史 → 全A内存爆。护栏是分叉加载器
`load_kline_recent`(日筛用,只取近 DAILY_KLINE_ROWS 根)与 `load_kline`(回测用,全历史)。
本测试锁住四条不变量,防未来有人:①误把回测切成近史 ②加了 >N 根的日筛回看却忘扩窗。
"""
import pytest

from tools.config import settings
from tools.collectors import market
from tools.store import repo as store


def _a_code_with_long_master():
    """找一只主档根数 > DAILY_KLINE_ROWS 的票;无(空仓/CI 无数据)则跳过。"""
    try:
        codes = store.list_master_codes() if hasattr(store, "list_master_codes") else []
    except Exception:
        codes = []
    for c in codes:
        try:
            if store.has_master_kline(c) and len(store.get_master_kline(c)) > settings.DAILY_KLINE_ROWS:
                return c
        except Exception:
            continue
    return None


def test_recent_is_tail_of_full():
    """近史加载器 == 全历史的尾部 N 根,且行数 ≤ N。"""
    code = _a_code_with_long_master()
    if code is None:
        pytest.skip("无主档数据(CI 无 data/),跳过")
    full = market.load_kline(code)
    recent = market.load_kline_recent(code)
    n = settings.DAILY_KLINE_ROWS
    assert len(recent) <= n
    assert len(recent) == min(len(full), n)
    # 尾部逐行相等(收盘序列)
    assert list(recent["close"].to_numpy()) == list(full["close"].tail(n).to_numpy())


def test_full_still_multiyear():
    """回测加载器仍返回全历史(远多于近史窗口)——防误把回测截断。"""
    code = _a_code_with_long_master()
    if code is None:
        pytest.skip("无主档数据,跳过")
    full = market.load_kline(code)
    assert len(full) > settings.DAILY_KLINE_ROWS


def test_daily_signal_parity_recent_vs_full():
    """核心语义锁:同一票的技术信号,用近史 vs 全历史算出的**最后一行**必须完全一致。

    因 DAILY_KLINE_ROWS(500) > 日筛最长回看(~251:MA200+52周高),尾部指标不受更早
    历史影响。若哪天有人加了 >500 根的回看,此断言会挂 → 提醒扩窗或改回全历史。
    """
    code = _a_code_with_long_master()
    if code is None:
        pytest.skip("无主档数据,跳过")
    from tools.analysis import technical as ta
    full = market.load_kline(code)
    recent = market.load_kline_recent(code)
    r_full = ta.compute(full)
    r_recent = ta.compute(recent)
    # 除行数 n(按设计不同)外,所有技术指标字段(只依赖尾部 ≤251 根)应逐字段相等
    for k in ("last", "ma", "macd", "kdj", "rsi", "bias", "vol", "signal", "reversal", "ob_os"):
        assert r_full.get(k) == r_recent.get(k), f"字段 {k} 近史/全历史不一致(窗口可能小于回看)"
