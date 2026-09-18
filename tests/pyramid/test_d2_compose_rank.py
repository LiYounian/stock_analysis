"""A10 排序稳定 tie-break + topN 分桶、A11 计分票数口径自洽 的语义锁。

数据无关：A10 用合成骨架票 + select_top；A11 用 monkeypatch 假工具跑 build_skeleton
的计数分支，锁死"参与=计分+数据缺、计分=排序+否决"的自洽口径。
"""
import random

import pytest

from tools.pyramid import d2_compose as C


def _row(code, score, 量价=50.0):
    return C.骨架票(code=code, 骨架分=score, 子分={"量价自证": 量价}, 来源标签=[])


# ── A10 稳定 tie-break ────────────────────────────────
def test_排序键_同分按量价子分再代码():
    rows = [_row("000003", 62.70, 量价=40.0),
            _row("000001", 62.70, 量价=80.0),
            _row("000002", 62.70, 量价=80.0),
            _row("000009", 70.00, 量价=10.0)]
    rows.sort(key=C._排序键)
    # 70 分在前；同 62.70 里量价高者(80)在前，同 80 再按代码升序
    assert [r.code for r in rows] == ["000009", "000001", "000002", "000003"]


def test_排序键_可复现_打乱后同序():
    base = [_row(f"{i:06d}", random.choice([62.70, 55.0, 70.0]),
                 量价=random.choice([40.0, 80.0])) for i in range(30)]
    a = sorted(base, key=C._排序键)
    shuffled = base[:]
    random.shuffle(shuffled)
    b = sorted(shuffled, key=C._排序键)
    assert [r.code for r in a] == [r.code for r in b]  # 排序完全确定


# ── A10 topN 分桶（同分不硬切）──────────────────────
def test_select_top_边界同分整桶纳入():
    ranked = sorted([_row("000001", 90.0), _row("000002", 80.0),
                     _row("000003", 70.0), _row("000004", 70.0),
                     _row("000005", 70.0)], key=C._排序键)
    sel = C.select_top(ranked, 3)  # 第3名=70，尚有两票也是70
    assert len(sel) == 5  # 同分整桶纳入，不在 70 分处劈开
    assert {r.code for r in sel} == {f"00000{i}" for i in range(1, 6)}


def test_select_top_清晰切点只取topN():
    ranked = sorted([_row("000001", 90.0), _row("000002", 80.0),
                     _row("000003", 70.0), _row("000004", 60.0)], key=C._排序键)
    sel = C.select_top(ranked, 3)
    assert [r.code for r in sel] == ["000001", "000002", "000003"]


def test_select_top_无同分跨界_随机压力():
    for _ in range(200):
        n = random.randint(1, 40)
        ranked = sorted([_row(f"{i:06d}", float(random.randint(50, 65)))
                         for i in range(n)], key=C._排序键)
        top_n = random.randint(1, n)
        sel = C.select_top(ranked, top_n)
        sel_codes = {r.code for r in sel}
        cut = sel[-1].骨架分  # 边界分
        # 不变式：边界分那一桶要么全进要么全不进——池里没有"同为边界分却被排除"的票
        for r in ranked:
            if r.骨架分 == cut:
                assert r.code in sel_codes
        assert len(sel) >= top_n


# ── A11 计分票数口径自洽（monkeypatch 假工具）──────────
class _FakeResult:
    def __init__(self, fields):
        self.fields = fields


class _FakeTool:
    def __init__(self, name, fmap, raise_codes):
        self.name = name
        self.fmap = fmap
        self.raise_codes = raise_codes

    def run(self, as_of, code, root=None):
        if code in self.raise_codes:
            raise RuntimeError("模拟取数失败")
        return _FakeResult(self.fmap.get(code, {}))


def test_build_skeleton_计数三口径自洽(monkeypatch):
    from tools.pyramid.tools import shared_pool_tool
    from tools.pyramid import registry

    # 5 票：3 排序 + 1 否决(涨停) + 1 数据缺失(取数抛错)
    members = {"000001": ["a"], "000002": ["b"], "000003": ["c"],
               "000004": ["d"], "000005": []}
    monkeypatch.setattr(shared_pool_tool, "pool_with_labels",
                        lambda as_of, root=None, scan_kline=True: members)

    pv = {c: {"量比档": "放量", "pos60档": "低"} for c in members}
    gate = {c: {"层级": "T2", "涨停": False, "极高位": False} for c in members}
    gate["000004"]["涨停"] = True  # 000004 → 否决
    sec = {c: {"角色": "中军", "RS档": "中", "拥挤档": "B", "净催化档": "中性",
               "板块": "电子"} for c in members}
    fake = {c: {"嫌疑档": "无嫌疑"} for c in members}
    exp = {c: {"命中规则": []} for c in members}
    empty = {c: {} for c in members}

    tools = {
        "price_volume": _FakeTool("price_volume", pv, {"000005"}),  # 000005 缺失
        "gate": _FakeTool("gate", gate, set()),
        "sector_context": _FakeTool("sector_context", sec, set()),
        "fake_good_news": _FakeTool("fake_good_news", fake, set()),
        "experience_rules": _FakeTool("experience_rules", exp, set()),
        "financial_redflag": _FakeTool("financial_redflag", empty, set()),
        "unlock_risk": _FakeTool("unlock_risk", empty, set()),
        "insider_reduction": _FakeTool("insider_reduction", empty, set()),
    }
    monkeypatch.setattr(registry, "get", lambda name: tools[name])

    out = C.build_skeleton("2026-09-17", root=None, scan_kline=False)
    assert out["池规模"] == 5
    assert out["参与票数"] == 5
    assert out["数据缺失票数"] == 1          # 000005
    assert out["否决票数"] == 1              # 000004 涨停
    assert out["排序票数"] == 3              # 000001/2/3
    # 三口径自洽：
    assert out["排序票数"] + out["否决票数"] == out["计分票数"]
    assert out["计分票数"] + out["数据缺失票数"] == out["参与票数"]
    assert out["参与票数"] == out["池规模"]  # 未 limit
    # 排序降序（tie-break 后仍降序）
    scores = [r.骨架分 for r in out["排序"]]
    assert scores == sorted(scores, reverse=True)
