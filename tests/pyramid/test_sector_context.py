"""sector_context 档位语义锁 + 集成测试（P1b 窗2）。

纯逻辑档位锁（守则6）恒跑：锁死 _FS档/_RS档/_净催化档/_拥挤含义/_冷热含义/_grade_净催化，
防未来重写无意删规则。集成测试在有主档数据的 root 上跑（bare CI 无数据则跳过）。
"""
import os
import pytest

from tools.pyramid.registry import get, all_names
from tools.pyramid._common import 格档
import tools.pyramid.tools  # noqa: F401  触发注册
from tools.pyramid.tools import sector_context_tool as sc

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


def _data_root():
    """定位含 sector 数据的 root：worktree 自身 → 主仓（worktree 场景）。无则 None。"""
    cands = [ROOT]
    if "/.claude/worktrees/" in ROOT:
        cands.append(ROOT.split("/.claude/worktrees/")[0])
    if "/worktrees/" in ROOT:
        cands.append(ROOT.split("/worktrees/")[0] + "/stock_analysis")
    for c in cands:
        if os.path.exists(os.path.join(c, "data", "analysis", AS_OF, "sector_focus.json")):
            return c
    return None


# ── 注册 ──
def test_已注册():
    assert "sector_context" in all_names()
    assert get("sector_context").塔层 == "④宏观"


# ── 档位语义锁（纯逻辑·恒跑）──
def test_focus_score档():
    assert 格档(0.40, sc._FS档)[0] == "低"
    assert 格档(0.60, sc._FS档)[0] == "中"
    assert 格档(0.88, sc._FS档)[0] == "高"


def test_RS_pos60档():
    assert 格档(0.10, sc._RS档)[0] == "弱"
    assert 格档(0.30, sc._RS档)[0] == "偏弱"
    assert 格档(0.50, sc._RS档)[0] == "中"
    assert 格档(0.70, sc._RS档)[0] == "偏强"
    assert 格档(0.90, sc._RS档)[0] == "强"


def test_净催化档_数值边界():
    assert 格档(-60.0, sc._净催化档)[0] == "强负"
    assert 格档(-10.0, sc._净催化档)[0] == "负"
    assert 格档(0.0, sc._净催化档)[0] == "中性"
    assert 格档(14.0, sc._净催化档)[0] == "正"
    assert 格档(96.0, sc._净催化档)[0] == "强正"


def test_grade_净催化_旁路与优先级():
    # 规避池优先压负
    assert sc._grade_净催化(96.0, True, True, None)[0] == "负"
    # 有净催化数值走数值档
    assert sc._grade_净催化(96.0, True, False, None)[0] == "强正"
    # 无数值→消息驱动旁路
    assert sc._grade_净催化(None, False, False, ("利好", "强"))[0] == "强正"
    assert sc._grade_净催化(None, False, False, ("利空", "中"))[0] == "负"
    # 都无→中性不编
    assert sc._grade_净催化(None, False, False, None)[0] == "中性"


def test_拥挤冷热含义锁():
    assert "拥挤高位" in sc._拥挤含义["A"]
    assert "不拥挤" in sc._拥挤含义["B"]
    for k in ("过冷", "正常活跃", "拐点", "过热"):
        assert k in sc._冷热含义


# ── 不编造：无法解析板块 → missing ──
def test_未知code_不编():
    res = get("sector_context").run(AS_OF, "999999", root=ROOT)
    assert res.freshness == "missing"
    assert res.fields.get("板块") is None
    assert "missing" in res.浓缩块
    assert res.防未来 is True


def test_缺code_报错():
    with pytest.raises(ValueError):
        get("sector_context").run(AS_OF, None, root=ROOT)


# ── 集成（有数据才跑）──
def test_集成_三票语义锁():
    root = _data_root()
    if root is None:
        pytest.skip("无 sector 数据（bare CI）")
    tool = get("sector_context")

    r1 = tool.run(AS_OF, "300308", root=root)
    assert r1.fields["板块"] == "电子"
    assert r1.fields["重点标"] == "重点池"
    assert r1.fields["focus_score档"] == "高"
    assert r1.fields["净催化档"] == "强正"      # net96 利好强
    assert r1.fields["RS档"] is not None
    assert r1.freshness == "fresh"
    assert r1.防未来 is True
    # 浓缩块恰 6 行（6 字段），且不含裸 json
    lines = [ln for ln in r1.浓缩块.splitlines() if ln.strip()]
    assert len(lines) == 6 and "{" not in r1.浓缩块

    r2 = tool.run(AS_OF, "600995", root=root)
    assert r2.fields["板块"] == "公用事业"
    assert r2.fields["净催化档"] == "正"        # net14
    assert r2.fields["拥挤档"] == "A" and r2.fields["冷热标签"] == "拐点"

    r3 = tool.run(AS_OF, "002142", root=root)
    assert r3.fields["板块"] == "银行"
    assert r3.fields["重点标"] == "非重点池"
    assert r3.fields["净催化档"] == "中性"       # 非重点池·无消息驱动
    assert r3.fields["RS档"] == "强"            # pos60≈0.81


def test_集成_防未来不取未来板块环境():
    """as_of 早于任一 sector 文件日 → 环境 missing，绝不下探未来日期。"""
    root = _data_root()
    if root is None:
        pytest.skip("无 sector 数据（bare CI）")
    res = get("sector_context").run("2026-01-01", "300308", root=root)
    assert res.freshness == "missing"
    assert res.fields.get("环境命中日") is None
    assert res.防未来 is True
