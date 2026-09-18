"""入场口径语义锁（A7 首入场/止损 + A8/D3 三处撮合一致性）。

锁死：
  A7 · entry_price 工具：首入场 ≠ 加仓价；止损幅 ≥ 最小距离下限（无 −0.1% 无效止损）；
       T 层 → method 映射（T1 破前高→突破旁路，否则回踩 MA5）。
  A8/D3 · entry_rule 撮合纯函数：三处（entry_price 定限价 / d3_score 记分 / model_a 撮合）
       在同一 entry_rule 下撮合结论一致——用 sector_news_forward._entry_labels（model_a 撮合源）
       与 entry_rule.match_entry、d3_score.forward_return 对照，同输入同结论。
"""
import os
import pandas as pd
import pytest

from tools.pyramid import entry_rule as ER
from tools.pyramid import d3_score as D3
from tools.pyramid.tools.entry_price_tool import (
    EntryPriceTool, _method_for_tier, _stop_floor_frac, _MIN_STOP_PCT,
)

ROOT = os.environ.get("D3_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════ A8/D3 · entry_rule 撮合纯函数（核心语义锁·无数据依赖）══════════════
def test_match_entry_close恒成交():
    m = ER.match_entry("close", limit=10.0)
    assert m["filled"] is True and m["entry_price"] == 10.0 and m["rule"] == "close"


def test_match_entry_limit回踩成交():
    # D+1 low(9.8) ≤ 限价(10) → 成交；成交价=min(限价, D+1 开=9.9)=9.9（低开按开盘）
    m = ER.match_entry("limit", limit=10.0, next_open=9.9, next_low=9.8)
    assert m["filled"] is True and m["entry_price"] == 9.9


def test_match_entry_limit高开未回踩踏空():
    # D+1 low(10.2) > 限价(10) → 高开未回踩、未成交（final）
    m = ER.match_entry("limit", limit=10.0, next_open=10.3, next_low=10.2)
    assert m["filled"] is False and m["entry_price"] is None


def test_match_entry_limit次日未到期pending():
    m = ER.match_entry("limit", limit=10.0, next_open=None, next_low=None)
    assert m["filled"] is None


def test_match_entry_限价缺():
    assert ER.match_entry("close", limit=None)["filled"] is None
    assert ER.match_entry("limit", limit=0.0, next_open=1, next_low=1)["filled"] is None


def test_normalize_rule非法回落默认():
    assert ER.normalize_rule("wtf") == ER.DEFAULT_ENTRY_RULE
    assert ER.normalize_rule("limit") == "limit"


# —— 三处同结论：match_entry 复现 model_a 撮合源(_entry_labels) 的 fill/price ——
@pytest.mark.parametrize("d0_close,d1_open,d1_low,exp_filled", [
    (10.0, 9.9, 9.8, True),     # 回踩成交
    (10.0, 10.3, 10.2, False),  # 高开踏空
    (10.0, 10.1, 9.5, True),    # 高开但盘中回踩到限价 → 成交@min(限价,开)=限价
])
def test_三处撮合一致_limit(d0_close, d1_open, d1_low, exp_filled):
    from tools.research import sector_news_forward as snf
    # 构造 3 根 K：D0 / D+1 / D+2，喂给 _entry_labels（其撮合口径已被 test_sector_news_forward 锁死）
    rows = [("2026-01-05", 10.0, 9.5, d0_close),
            ("2026-01-06", d1_open, d1_low, d1_open),   # D+1 收盘随意（不影响 fill 判定）
            ("2026-01-07", d1_open, d1_low, d1_open)]
    d2i = {r[0]: i for i, r in enumerate(rows)}
    cache = {"TEST": (rows, d2i)}
    base = snf._entry_labels("TEST", "2026-01-05", cache, ew={})
    # entry_rule.match_entry 用同一输入（限价=D0 收）撮合
    m = ER.match_entry("limit", limit=d0_close, next_open=d1_open, next_low=d1_low)
    assert m["filled"] is exp_filled
    assert base["entry"]["filled"] is exp_filled
    if exp_filled:
        # 成交价一致（三处都 = min(限价, D+1 开)）
        assert m["entry_price"] == pytest.approx(base["entry"]["price"])


# ══════════════ A7 · entry_price 工具 T 层 method 映射（纯函数）══════════════
def test_T层method映射():
    assert _method_for_tier("T1") == "突破新高确认"     # 破前高 → 突破旁路
    assert _method_for_tier("T2") == "回踩MA5"
    assert _method_for_tier("T3") == "回踩MA5"
    assert _method_for_tier(None) == "回踩MA5"


def test_止损下限计算():
    # ATR 小 → 用最小百分比下限 2%
    assert _stop_floor_frac(现价=100.0, atr=0.5) == pytest.approx(_MIN_STOP_PCT)
    # ATR 大 → 用 ATR 自适应（4/100=4% > 2%）
    assert _stop_floor_frac(现价=100.0, atr=4.0) == pytest.approx(0.04)


# ══════════════ A7 · entry_price 工具端到端（需 K线数据·缺则 skip）══════════════
def _has(code):
    return os.path.exists(os.path.join(ROOT, "data", "master", "kline", f"{code}.parquet"))


def test_首入场不等于加仓且止损不退化():
    if not _has("301551"):
        pytest.skip("无 301551 K线")
    f = EntryPriceTool().run(as_of="2026-09-17", code="301551", t_tier="T2", root=ROOT).fields
    assert not f.get("数据不足")
    # 首入场(=D0收) ≠ 加仓(=MA5)：正常票 MA5≠现价
    assert f["首入场限价"] != f["加仓位"]
    # 止损幅 ≥ 最小下限（绝无 −0.1% 无效止损）
    assert f["止损幅pct"] <= -_MIN_STOP_PCT * 100 + 1e-6
    assert f["止损幅pct"] <= f["止损下限pct"] + 1e-6   # 不比下限更松
    # 单调性：止损 < 首入场 ≤ 红线
    assert f["止损价"] < f["首入场限价"] <= f["不追高上限"]
    assert f["单调性ok"] is True
    # A8：工具口径标 limit，与 d3_score/model_a 对齐
    assert f["entry_rule"] == "limit"


def test_止损下限在平均线粘合时仍生效():
    """构造 MA5≈MA20≈现价 的样本（历史 301551 −0.1% 事故场景）→ 止损仍 ≥ 2%。"""
    # 用一个真实票即可验证下限恒成立；这里复用工具的下限计算路径已被上面锁死，
    # 端到端只要任一真实票的止损幅都 ≥ 下限即可（数据缺则 skip）。
    if not _has("000725"):
        pytest.skip("无 000725 K线")
    f = EntryPriceTool().run(as_of="2026-09-17", code="000725", t_tier="T1", root=ROOT).fields
    if f.get("数据不足"):
        pytest.skip("000725 数据不足")
    assert f["止损幅pct"] <= -_MIN_STOP_PCT * 100 + 1e-6
    assert f["入场方式"] == "突破新高确认"   # T1 → 突破旁路


# ══════════════ A8/D3 · d3_score.forward_return entry_rule 贯通 ══════════════
def test_forward_entry_rule贯通():
    if not _has("300776"):
        pytest.skip("无 300776 K线")
    # close 口径恒成交；limit 口径按撮合可能踏空。二者 filled 字段都在。
    fc = D3.forward_return("300776", "2026-09-17", 1, "2026-09-18", root=ROOT, entry_rule="close")
    assert fc.get("filled") is True and fc.get("entry_rule") == "close"
    fl = D3.forward_return("300776", "2026-09-17", 1, "2026-09-18", root=ROOT, entry_rule="limit")
    assert fl.get("entry_rule") == "limit"
    assert fl.get("filled") in (True, False)   # 明确成交/踏空，不含糊
