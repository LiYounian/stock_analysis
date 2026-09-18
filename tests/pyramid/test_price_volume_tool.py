"""price_volume 档位语义锁 + 真数据冒烟。守则6：锁住"为什么改"防未来重写删规则。"""
import os
import pytest

from tools.pyramid.registry import get
from tools.pyramid._common import 格档
import tools.pyramid.tools  # noqa: F401  触发注册
from tools.pyramid.tools.price_volume_tool import 量比档, pos60档

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


# ── 档位语义锁：量比表边界写死 ──
def test_量比档_边界锁():
    assert 格档(0.5, 量比档)[0] == "缩量"
    assert 格档(0.7, 量比档)[0] == "缩量"   # ≤上界命中
    assert 格档(0.71, 量比档)[0] == "平量"
    assert 格档(1.2, 量比档)[0] == "平量"
    assert 格档(1.21, 量比档)[0] == "放量"
    assert 格档(2.5, 量比档)[0] == "放量"
    assert 格档(2.51, 量比档)[0] == "爆量"
    assert 格档(99, 量比档)[0] == "爆量"     # 末档兜底
    assert 格档(None, 量比档)[0] == "无档"


def test_pos60档_边界锁():
    assert 格档(0.2, pos60档)[0] == "低"
    assert 格档(0.3, pos60档)[0] == "低"
    assert 格档(0.31, pos60档)[0] == "中"
    assert 格档(0.7, pos60档)[0] == "中"
    assert 格档(0.71, pos60档)[0] == "高"
    assert 格档(1.0, pos60档)[0] == "高"


# ── 真数据冒烟 ──
def test_price_volume_真数据():
    res = get("price_volume").run(AS_OF, "300308", root=ROOT)
    if res.fields.get("数据不足"):
        pytest.skip("无 300308 数据")
    f = res.fields
    assert f["session"] == "收盘"
    assert res.防未来 is True
    assert res.freshness == "fresh"
    assert f["量比档"] in ("缩量", "平量", "放量", "爆量")
    assert f["pos60档"] in ("低", "中", "高")
    # 浓缩块 ≤8 行由 ToolResult 契约保证；此处核关键字段进了浓缩块
    assert "量比" in res.浓缩块 and "pos60" in res.浓缩块


def test_price_volume_数据不足不编造():
    res = get("price_volume").run(AS_OF, "000000", root=ROOT)
    assert res.freshness == "missing"
    assert res.fields.get("数据不足") is True
    assert "人工确认" in res.浓缩块
