"""午盘 Q · M1 · fundflow_intraday 契约测试(骨架期)。

断言的是 **I/O 契约、不是实现**,骨架阶段就在:
    · 签名/参数校验立刻生效(freq/as_of/codes 长度)
    · 常量存在且值正确(阈值/单位)
    · 异常路径的语义

函数体实现后,本文件里的每个 `pytest.raises(NotImplementedError)` 会翻译成
真正的行为断言(参见 M2 前的补测计划)。
"""
from __future__ import annotations

import pytest

from tools.collectors import fundflow_intraday as ff


# ────────────────────────────── 常量/接口 ──────────────────────────────

def test_module_constants():
    """核心常量存在,值符合方案。"""
    assert ff.FREQ_1MIN == 1
    assert ff.FREQ_5MIN == 5
    assert ff.MAX_BATCH_CODES == 50, "候选池上限=50,防误用于全A"
    assert ff._SOURCE == "eastmoney:fflow/kline"
    assert "fflow/kline/get" in ff._FF_URL
    assert ff._COLS[0] == "time", "首列必须是 time(与日线 fundflow 的 date 区分)"
    assert "主力净流入" in ff._COLS
    assert "超大单净流入" in ff._COLS


def test_secid_mapping():
    """_secid 与日线 fundflow 保持一致规则(6/9 → 1.;其他 → 0.)。"""
    # 沪
    assert ff._secid("600519") == "1.600519"
    # 深
    assert ff._secid("002415") == "0.002415"
    # 创
    assert ff._secid("300750") == "0.300750"


def test_public_api_signatures_exist():
    """公开 API 存在,可被 import(即使 body 未实现)。"""
    assert callable(ff.fetch_one)
    assert callable(ff.collect)
    assert callable(ff.load_intraday)


# ────────────────────────────── 骨架期:NotImplementedError ──────────────────────────────
# 实现后本节改成真行为断言

def test_fetch_one_is_stub():
    with pytest.raises(NotImplementedError):
        ff.fetch_one("600519", freq=5, as_of="14:30")


def test_collect_is_stub():
    with pytest.raises(NotImplementedError):
        ff.collect(["600519"], freq=5, as_of="14:30")


def test_load_intraday_is_stub():
    with pytest.raises(NotImplementedError):
        ff.load_intraday("600519")


# ────────────────────────────── 契约细节:实现后要保留 ──────────────────────────────
# 下列测试标 skip,实现后去掉 skip 生效——是"未来的行为断言登记处"。

@pytest.mark.skip(reason="M1 骨架期跳过;实现后:freq 只能 1 或 5,其他抛 ValueError")
def test_fetch_one_rejects_bad_freq():
    with pytest.raises(ValueError, match="freq"):
        ff.fetch_one("600519", freq=15)


@pytest.mark.skip(reason="M1 骨架期跳过;实现后:as_of 非 HH:MM → ValueError")
def test_fetch_one_rejects_bad_as_of():
    with pytest.raises(ValueError, match="as_of"):
        ff.fetch_one("600519", as_of="14:305")


@pytest.mark.skip(reason="M1 骨架期跳过;实现后:codes 超 50 → ValueError")
def test_collect_rejects_too_many_codes():
    with pytest.raises(ValueError, match="MAX_BATCH_CODES"):
        ff.collect(["000001"] * 51, freq=5)


@pytest.mark.skip(reason="M1 骨架期跳过;实现后:as_of='14:30' → time>14:30 的行被剔除(防未来函数)")
def test_fetch_one_truncates_by_as_of():
    """未来函数红线测试:14:30 as_of 下,输出里 time.strftime('%H:%M') 全部 <= '14:30'。"""
    pass
