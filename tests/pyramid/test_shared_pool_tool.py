"""shared_pool 合成语义锁：平权并集 / 缺来源标 missing / 来源标签。守则6。

用纯函数 build_result + 各 raw set 直接锁死合成口径，不跑重扫描（K线过闸另有真数据冒烟）。
"""
import os
import pytest

from tools.pyramid.tools.shared_pool_tool import (
    build_result,
    _extract_codes,
    _SOURCE_ORDER,
)
from tools.pyramid.registry import get
import tools.pyramid.tools  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"
_NAME, _层, _SRC = "shared_pool", "①塔基", "test"


def _br(raw, code=None):
    return build_result(_NAME, _层, _SRC, AS_OF, code, raw)


# ── 平权并集口径锁 ──
def test_平权并集与共识():
    raw = {
        "多策略并集": {"A", "B"},
        "council": {"B", "C"},
        "板块roster": {"C", "D"},
        "agent主线": {"A", "E"},
        "K线过闸": {"A", "F"},
    }
    r = _br(raw)
    # 并集 = A B C D E F = 6
    assert r.fields["池规模"] == 6
    # A(3次) B(2次) C(2次) 命中≥2来源 = 3 票
    assert r.fields["共识数"] == 3
    assert r.fields["缺来源"] == []
    assert r.freshness == "fresh"


def test_缺来源标missing不编():
    raw = {k: None for k in _SOURCE_ORDER}
    raw["K线过闸"] = {"X", "Y"}
    r = _br(raw)
    assert set(r.fields["缺来源"]) == {"多策略并集", "council", "板块roster", "agent主线"}
    assert r.fields["池规模"] == 2
    assert "missing" in r.浓缩块


def test_全缺则freshness_missing():
    r = _br({k: None for k in _SOURCE_ORDER})
    assert r.freshness == "missing"
    assert r.fields["池规模"] == 0


def test_来源标签_命中多来源():
    raw = {
        "多策略并集": {"300308"},
        "council": None,
        "板块roster": {"300308", "600995"},
        "agent主线": {"300308"},
        "K线过闸": {"600995"},
    }
    r = _br(raw, code="300308")
    assert set(r.fields["命中来源"]) == {"多策略并集", "板块roster", "agent主线"}
    assert "300308" in r.浓缩块


def test_来源标签_未命中():
    raw = {"K线过闸": {"600995"}, "多策略并集": None, "council": None,
           "板块roster": None, "agent主线": None}
    r = _br(raw, code="999999")
    assert r.fields["命中来源"] == []
    assert "命中来源=无" in r.浓缩块


# ── 代码抽取锁 ──
def test_extract_codes():
    obj = {"selections": [{"code": "300308"}, {"code": 600995}],
           "nested": {"picks": [{"代码": "000001"}]}, "list": ["002415"]}
    codes = _extract_codes(obj)
    assert {"300308", "600995", "000001", "002415"} <= codes


# ── 真数据冒烟（跑真实全A扫描，~15s；无数据则跳过）──
def test_shared_pool_真数据():
    if not os.path.isdir(os.path.join(ROOT, "data", "master", "kline")):
        pytest.skip("无 K 线数据")
    res = get("shared_pool").run(AS_OF, "300308", root=ROOT)
    assert res.防未来 is True
    assert res.fields["池规模"] >= 0
    assert isinstance(res.fields["各来源命中数"], dict)
