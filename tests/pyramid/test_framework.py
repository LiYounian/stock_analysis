"""P1a 框架契约测试。锁死：ToolResult 契约 / 浓缩块≤8行 / 防未来 / entry_price 单调性与语义。

档位语义锁（守则6）：这些断言锁住"为什么改"，防未来重写无意删规则。
"""
import os
import pandas as pd
import pytest

from tools.pyramid.registry import ToolResult, register, get, all_names, list_tools
from tools.pyramid import _common
import tools.pyramid.tools  # noqa: F401  触发注册

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


# ── 契约 ──
def test_toolresult_塔层校验():
    with pytest.raises(ValueError):
        ToolResult(name="x", 塔层="不存在", as_of=AS_OF, 浓缩块="a")


def test_toolresult_freshness校验():
    with pytest.raises(ValueError):
        ToolResult(name="x", 塔层="①塔基", as_of=AS_OF, 浓缩块="a", freshness="bad")


def test_浓缩块_超8行即违规():
    with pytest.raises(ValueError):
        ToolResult(name="x", 塔层="①塔基", as_of=AS_OF, 浓缩块="\n".join(str(i) for i in range(9)))


def test_to_prompt_不吐裸json():
    r = ToolResult(name="t", 塔层="①塔基", as_of=AS_OF, 浓缩块="现价 10 元 [中]", fields={"x": 1})
    s = r.to_prompt()
    assert "现价 10 元" in s and "{" not in s and "x" not in s.split("\n", 1)[-1]


def test_to_prompt_stale带旗标():
    r = ToolResult(name="t", 塔层="②消息", as_of=AS_OF, 浓缩块="a", freshness="stale")
    assert "[stale]" in r.to_prompt()


# ── 防未来 ──
def test_assert_no_future_截断():
    df = pd.DataFrame(
        {"date": pd.to_datetime(["2026-09-16", "2026-09-17", "2026-09-18"]), "close": [1, 2, 3]}
    )
    out = _common.assert_no_future(df, "2026-09-17")
    assert out["date"].max() == pd.Timestamp("2026-09-17")
    assert 3 not in out["close"].values  # 09-18 的未来行被剔


def test_load_kline_防未来(tmp_path=None):
    df = _common.load_kline("300308", AS_OF, root=ROOT, min_bars=20)
    if df is not None:  # 数据在则校验，不在则跳过（CI 无数据）
        assert pd.to_datetime(df["date"]).max() <= pd.Timestamp(AS_OF)


# ── 档位渲染语义锁 ──
def test_格档_命中与缺省():
    表 = [(0.3, "低", "低位"), (0.7, "中", "中位"), (1.01, "高", "高位")]
    assert _common.格档(0.2, 表) == ("低", "低位")
    assert _common.格档(0.5, 表) == ("中", "中位")
    assert _common.格档(0.9, 表) == ("高", "高位")
    assert _common.格档(None, 表)[0] == "无档"


# ── 注册表 ──
def test_entry_price_已注册():
    assert "entry_price" in all_names()
    assert get("entry_price").塔层 == "①塔基"


def test_list_tools_读yaml列全7():
    names = {t.get("name") for t in list_tools()}
    for n in ["entry_price", "shared_pool", "gate", "price_volume",
              "sector_context", "experience_rules", "fake_good_news"]:
        assert n in names, f"yaml 缺 {n}"


# ── entry_price 样板：单调性 + 语义 ──
def test_entry_price_单调性铁律():
    df = _common.load_kline("300308", AS_OF, root=ROOT, min_bars=20)
    if df is None:
        pytest.skip("无 300308 数据")
    res = get("entry_price").run(AS_OF, "300308", root=ROOT)
    f = res.fields
    if f.get("数据不足"):
        pytest.skip("数据不足")
    entry, stop, cap = f["挂单价"], f["止损价"], f["不追高上限"]
    # G5 铁律：止损 < 挂单 ≤ 红线；回踩限价不追高开
    assert stop < entry <= cap
    assert entry <= f["现价"]  # 回踩MA5 默认 → 不挂到现价之上
    assert f["单调性ok"] is True
    assert res.防未来 is True


def test_entry_price_数据不足不编造():
    res = get("entry_price").run(AS_OF, "000000", root=ROOT)  # 不存在的票
    assert res.freshness == "missing"
    assert res.fields.get("数据不足") is True
    assert "人工确认" in res.浓缩块
