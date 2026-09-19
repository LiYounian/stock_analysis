"""valuation 语义锁。锁死估值档位(PE/PB/PEG) + PEG现算口径 + 缺数据NA不编（Wave2 基本面·估值）。"""
import os
import json
import pytest

from tools.pyramid.registry import get
from tools.pyramid.tools.valuation_tool import (
    _PE参考档, _PB档, _PEG档, _pick_growth, ValuationTool,
)
from tools.pyramid._common import 格档, 字段
import tools.pyramid.tools  # noqa: F401  触发 register

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 主仓数据根（worktree 里 per-stock json 可能不全；照 redflag/unlock 先例用 env 指回主仓）。
DATA_ROOT = os.environ.get("VALUATION_TEST_DATA_ROOT") or os.environ.get("REDFLAG_TEST_DATA_ROOT") or ROOT
AS_OF = "2026-09-18"
CODE = "000026"


# ── PE 参考档边界锁（亏损/低≤15/中≤30/偏高≤60/高）──
def test_PE档位边界():
    assert 格档(-5, _PE参考档)[0] == "亏损"
    assert 格档(0, _PE参考档)[0] == "亏损"
    assert 格档(15, _PE参考档)[0] == "低"
    assert 格档(15.01, _PE参考档)[0] == "中"
    assert 格档(30, _PE参考档)[0] == "中"
    assert 格档(60, _PE参考档)[0] == "偏高"
    assert 格档(60.01, _PE参考档)[0] == "高"
    assert 格档(300, _PE参考档)[0] == "高"


# ── PB 档边界锁（破净≤1/低≤2/中≤4/偏高≤8/高）──
def test_PB档位边界():
    assert 格档(0.9, _PB档)[0] == "破净"
    assert 格档(1.0, _PB档)[0] == "破净"
    assert 格档(2.0, _PB档)[0] == "低"
    assert 格档(4.0, _PB档)[0] == "中"
    assert 格档(8.0, _PB档)[0] == "偏高"
    assert 格档(8.01, _PB档)[0] == "高"


# ── PEG 档边界锁（Lynch 基准1：低估≤0.8/合理≤1.2/偏高≤2/高估）──
def test_PEG档位边界():
    assert 格档(0.8, _PEG档)[0] == "低估"
    assert 格档(0.81, _PEG档)[0] == "合理"
    assert 格档(1.2, _PEG档)[0] == "合理"
    assert 格档(2.0, _PEG档)[0] == "偏高"
    assert 格档(2.01, _PEG档)[0] == "高估"


# ── PEG 增速源优先级：financial.利润表摘要.归母净利增速 优先，缺则 fundamental.净利增速 ──
def test_pick_growth_优先financial摘要():
    g, src = _pick_growth({"净利增速": 5.0},
                          {"利润表摘要": {"归母净利增速": 21.9}, "报告期": "2026-06-30"})
    assert g == 21.9 and "归母" in src and "2026-06-30" in src


def test_pick_growth_fallback_fundamental():
    g, src = _pick_growth({"净利增速": 5.0}, {"利润表摘要": {}})
    assert g == 5.0 and "净利" in src


def test_pick_growth_两缺():
    g, src = _pick_growth({}, {})
    assert g is None and src == "增速缺失"


# ── 脱敏假 json fixture：造 data/analysis/<as_of>/<code>.json 走各分支 ──
def _write_fake(tmp_path, code, valuation, fundamental=None, financial=None):
    d = os.path.join(str(tmp_path), "data", "analysis", AS_OF)
    os.makedirs(d, exist_ok=True)
    doc = {"valuation": valuation}
    if fundamental is not None:
        doc["fundamental"] = fundamental
    if financial is not None:
        doc["financial"] = financial
    with open(os.path.join(d, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
    return str(tmp_path)


# ── PEG 现算口径：增速≤0 时 PEG 失效不套档（不给数值档，标"失效"）──
def test_PEG_增速为负失效(tmp_path):
    root = _write_fake(
        tmp_path, "600000",
        valuation={"pe_ttm": 30.0, "pb": 2.0, "mktcap_yi": 100.0,
                   "pe_valid": True, "mode": "PE适用", "basis": "b", "口径提示": "t"},
        fundamental={"净利增速": -12.0},
        financial={"利润表摘要": {"归母净利增速": -12.0}, "报告期": "2026-06-30"},
    )
    r = ValuationTool().run(AS_OF, "600000", root=root)
    peg = next(it for it in r.字段解读 if it["名"] == "PEG(现算)")
    assert peg["值"] == "不适用"
    assert "增速≤0" in peg["口径"] and "PEG失效" in peg["口径"]
    # 不含任何 PEG 数值档（低估/合理/偏高/高估）
    assert not any(x in peg["口径"] for x in ("低估", "合理", "偏高", "高估"))


# ── PE 口径不适用（pe_valid=False / mode≠PE适用）→ 不套档，原样传 basis，PEG 连带不适用 ──
def test_PE不适用_不套档_传basis(tmp_path):
    root = _write_fake(
        tmp_path, "600001",
        valuation={"pe_ttm": -8.0, "pb": 1.5, "mktcap_yi": 50.0,
                   "pe_valid": False, "mode": "亏损_PB口径",
                   "basis": "亏损·改用PB", "口径提示": "t"},
        fundamental={"净利增速": 20.0},
    )
    r = ValuationTool().run(AS_OF, "600001", root=root)
    pe = next(it for it in r.字段解读 if it["名"] == "PE(TTM)")
    peg = next(it for it in r.字段解读 if it["名"] == "PEG(现算)")
    assert "不适用" in pe["口径"] and "亏损·改用PB" in pe["意味"]
    assert "不适用" in peg["口径"]  # PE 不适用 → PEG 连带不适用


# ── 正常 PEG 现算数值锁：pe30÷增速20% = 1.5 → 偏高 ──
def test_PEG正常现算(tmp_path):
    root = _write_fake(
        tmp_path, "600002",
        valuation={"pe_ttm": 30.0, "pb": 3.0, "mktcap_yi": 80.0,
                   "pe_valid": True, "mode": "PE适用", "basis": "b", "口径提示": "t"},
        financial={"利润表摘要": {"归母净利增速": 20.0}, "报告期": "2026-06-30"},
    )
    r = ValuationTool().run(AS_OF, "600002", root=root)
    peg = next(it for it in r.字段解读 if it["名"] == "PEG(现算)")
    assert peg["值"] == "1.50" and "偏高" in peg["口径"]


# ── 缺数据：不存在的 code → missing 不编 ──
def test_缺valuation_missing():
    t = ValuationTool()
    r = t.run(AS_OF, "999999", root=DATA_ROOT)
    assert r.freshness == "missing"
    assert "数据缺失" in r.浓缩块
    assert r.面 == "基本面"


# ── 字段口径三段不空编（值 None→NA 合法，口径/意味 空则 raise）──
def test_字段拒空编():
    with pytest.raises(ValueError):
        字段("X", 1.0, "", "意味")  # 口径空
    with pytest.raises(ValueError):
        字段("X", 1.0, "口径", "")  # 意味空
    ok = 字段("X", None, "口径", "意味")  # 值 None→NA 合法
    assert ok["值"] == "NA"


# ── 真实票集成：000026 关键档锁（PE 高/PEG 高估/PE历史分位待补/面=基本面）──
@pytest.mark.skipif(
    not os.path.exists(os.path.join(DATA_ROOT, "data", "analysis", AS_OF, f"{CODE}.json")),
    reason="需主仓 per-stock json（设 VALUATION_TEST_DATA_ROOT 指回主仓）",
)
def test_run_真实票():
    r = get("valuation").run(AS_OF, CODE, root=DATA_ROOT)
    assert r.面 == "基本面" and r.freshness == "fresh"
    txt = r.to_prompt()
    assert "PE(TTM)" in txt and "PEG(现算)" in txt
    assert "待补落盘" in txt  # PE历史分位缺口显式标注
    # 字段解读结构：每条四段齐全（契约 __post_init__ 已校验，这里再确认非空）
    assert len(r.字段解读) == 5
    for it in r.字段解读:
        assert it["名"] and it["口径"] and it["意味"] and it["值"]
