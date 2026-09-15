"""锁：复盘 md「次日实盘绝对收益三列」解析（2026-09-15 整改设计 §4，阶段2A）。

为什么改（守则#6）：
  · 复盘记分从"α 相对记分"扩到"按入场价算**绝对收益 + 是否收盘为正**"，列名冻结；
  · **向后兼容硬要求**：旧复盘（无这三列）→ 返回 None（缺则 None、不炸），绝不因新列报错；
  · "未触发"票的入场价/是否为正 → None（诚实，不拿没买的票充数）。
"""
from __future__ import annotations

from tools import selection_summary as ss

# 新格式复盘：逐票收盘记分表带 入场价 / 绝对收益% / 是否收盘为正 三新列。
_NEW_MD = """# 2026-09-15 盘尾复盘

## 逐票收盘记分（次日实盘口径）

| 票/代码/名称 | 入场点 | 入场价 | D+1 收盘 | 入场→收盘 绝对收益% | 是否收盘为正 | α vs 全A等权 | β归因 | D+2 卖出线达成 |
|---|---|---|---|---|---|---|---|---|
| 中信金属 601061 | 回踩12.00挂限价 | 12.00 | 12.30 | +2.50% | 是 | +1.20pp | 有色β次要 | 未跟踪 |
| 恒丰纸业 600356 | 昨收挂限价 | 8.30 | 8.14 | −1.93% | 否 | −0.80pp | 大盘拖累为主 | 未达成 |
| 某科技 300999 | 触及29.5才买 | 未触发 | — | 未触发 | 未触发 | — | — | — |
"""

# 旧格式复盘：只有 α 列，无三新列。
_OLD_MD = """# 2026-09-01 盘尾复盘

## 逐票收盘记分

| 票 | 昨收→今收 | α vs 全A等权 |
|---|---|---|
| 中信金属 601061 | +2.50% | +1.20pp |
"""


def test_new_format_positive_row():
    r = ss.parse_review_absolute_scorecard(_NEW_MD, "601061")
    assert r == {
        ss.COL_ENTRY_PRICE: 12.00,
        ss.COL_ABS_RETURN: 2.50,
        ss.COL_CLOSE_POSITIVE: True,
    }


def test_new_format_negative_row():
    r = ss.parse_review_absolute_scorecard(_NEW_MD, "600356")
    assert r[ss.COL_ENTRY_PRICE] == 8.30
    assert r[ss.COL_ABS_RETURN] == -1.93          # 兼容 Unicode 负号 −
    assert r[ss.COL_CLOSE_POSITIVE] is False


def test_untriggered_row_all_none_but_dict():
    """未触发票：入场价/绝对收益/是否为正 全 None（不拿没买的票充数），但行存在→返回 dict。"""
    r = ss.parse_review_absolute_scorecard(_NEW_MD, "300999")
    assert r == {
        ss.COL_ENTRY_PRICE: None,
        ss.COL_ABS_RETURN: None,
        ss.COL_CLOSE_POSITIVE: None,
    }


def test_old_format_returns_none():
    """旧复盘无三新列 → None（向后兼容硬要求，不炸）。"""
    assert ss.parse_review_absolute_scorecard(_OLD_MD, "601061") is None


def test_code_not_found_returns_none():
    assert ss.parse_review_absolute_scorecard(_NEW_MD, "999999") is None


def test_empty_or_bad_input_returns_none():
    assert ss.parse_review_absolute_scorecard("", "601061") is None
    assert ss.parse_review_absolute_scorecard(_NEW_MD, "") is None
    assert ss.parse_review_absolute_scorecard("不是表格的乱文本", "601061") is None


def test_existing_close_pct_parser_unaffected():
    """回归：新增函数未改动旧 parse_review_close_pct——旧格式复盘仍取到 D 日涨跌%。"""
    assert ss.parse_review_close_pct(_OLD_MD, "601061") == 2.50
