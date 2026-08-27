"""Stock.added_at 向后兼容 + add_stock 写时间戳单测(方案2 删除裁决前提)。

锁住:①旧 JSON 无 added_at 字段可正常加载(默认"");②add_stock 写入 now(ISO);
③读接口签名/返回结构不变(上层零影响);④asdict 持久化自动带上 added_at。
全程 monkeypatch _STORE 指向临时文件,绝不碰真实 config/stock_pool.json。
"""
import json

import pytest

from tools.config import stock_pool as sp


@pytest.fixture
def temp_store(tmp_path, monkeypatch):
    store = tmp_path / "stock_pool.json"
    monkeypatch.setattr(sp, "_STORE", store)
    yield store


def test_old_json_without_added_at_loads(temp_store):
    """旧格式(无 added_at)条目能加载,added_at 默认空串。"""
    temp_store.write_text(json.dumps([
        {"code": "600000", "name": "浦发银行", "industry": "银行", "sector": "银行", "market": "A"},
    ], ensure_ascii=False), encoding="utf-8")
    sp.reload()
    s = sp.get("600000")
    assert s is not None
    assert s.added_at == ""                       # 向后兼容:缺字段 → 默认""


def test_new_json_with_added_at_roundtrips(temp_store):
    """新格式带 added_at 能原样读回。"""
    temp_store.write_text(json.dumps([
        {"code": "600000", "name": "浦发", "industry": "银行", "sector": "银行",
         "market": "A", "added_at": "2026-08-20T10:00:00+08:00"},
    ], ensure_ascii=False), encoding="utf-8")
    sp.reload()
    assert sp.get("600000").added_at == "2026-08-20T10:00:00+08:00"


def test_add_stock_stamps_now(temp_store):
    temp_store.write_text("[]", encoding="utf-8")
    sp.reload()
    s = sp.add_stock("000001", "平安银行", "银行", "银行")
    assert s.added_at != ""                        # 写了时间戳
    assert "T" in s.added_at                        # ISO 形态
    # 持久化后重载仍在
    sp.reload()
    assert sp.get("000001").added_at == s.added_at


def test_asdict_persists_added_at(temp_store):
    temp_store.write_text("[]", encoding="utf-8")
    sp.reload()
    sp.add_stock("000001", "平安银行", "银行", "银行")
    raw = json.loads(temp_store.read_text(encoding="utf-8"))
    assert "added_at" in raw[0] and raw[0]["added_at"]   # 落盘带上新字段


def test_read_interfaces_unchanged(temp_store):
    """读接口返回结构不变:get_codes/by_sector/get_pool 照常工作。"""
    temp_store.write_text("[]", encoding="utf-8")
    sp.reload()
    sp.add_stock("000001", "平安银行", "银行", "银行")
    sp.add_stock("300308", "中际旭创", "光模块", "光模块")
    assert set(sp.get_codes()) == {"000001", "300308"}
    assert set(sp.by_sector().keys()) == {"银行", "光模块"}
    assert len(sp.get_pool()) == 2
