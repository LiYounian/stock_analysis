"""经验沉淀文档 doc-lint(锁"为什么改"的语义在真实经验库里不被后续改写悄悄抹掉)。

锁三件(2026-09-14 回测落地):
  · #31:超跌反抽首入场闸门=放量收阳(H2 已验证),且标 状态：已验证;
  · #10:capitulation β 例外档存在,**且带"不给个股加超跌α"的边界守卫**
        (最重要的诚实边界:② 是 β 择时,绝不冒充个股 α),标 状态：已验证;
  · 弱市不硬凑(3-5 目标不降标准硬塞)条款存在。

用真实最新版经验库(experience_recall 的解析路径),防未来 eod-review 并版时改丢。
"""
from __future__ import annotations

import re

import pytest

from tools.analysis import experience_recall as er


@pytest.fixture(scope="module")
def entries_by_id():
    entries, ver = er.load_entries("2026-09-14")
    assert ver is not None, "找不到 ≤2026-09-14 的经验版本"
    return {e.id: e for e in entries}


def test_entry31_trigger_is_volup_close_and_verified(entries_by_id):
    e = entries_by_id.get(31)
    assert e is not None, "经验 #31 缺失"
    assert "放量收阳" in e.body, "#31 未定稿'放量收阳'首入场闸门"
    # MA5 明确降为非首入场闸门(加仓/趋势确认档)
    assert re.search(r"MA5.{0,20}(加仓|趋势确认|不作首入场|二次确认)", e.body), \
        "#31 未把 MA5 降为加仓/趋势确认档"
    assert e.status == "已验证", "#31 触发下移已由 H2 回测坐实,应标'已验证'"


def test_entry10_has_capitulation_exception_without_stock_alpha(entries_by_id):
    e = entries_by_id.get(10)
    assert e is not None, "经验 #10 缺失"
    assert "capitulation" in e.body, "#10 未加 capitulation 例外档"
    # 最重要的边界:β 择时不冒充个股 α —— 必须有显式否定守卫
    assert re.search(r"(不.{0,12}个股.{0,6}α|不.{0,10}超跌选股\s*α|不.{0,12}超跌.{0,6}α)", e.body), \
        "#10 capitulation 例外缺'不给个股加超跌α'的边界守卫(② 不得越界成个股 α)"
    # H1-α 证不了 的诚实标注在 #10 里体现
    assert "证不了" in e.body or "H1-α" in e.body, "#10 未标注 H1-α 证不了的边界"
    assert e.status == "已验证"


def test_weak_market_no_forced_fill(entries_by_id):
    # 3-5 目标弱市不硬凑(诚实说 N 只)——落在 #10 ⑥ 或全库任一条目
    blob = "\n".join(e.body for e in entries_by_id.values())
    assert re.search(r"(不硬凑|只有\s*N\s*只|够格|不.{0,6}降标准)", blob), \
        "弱市诚实条款(不硬凑/只有N只够格)缺失"
