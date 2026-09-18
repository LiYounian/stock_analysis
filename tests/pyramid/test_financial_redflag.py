"""financial_redflag 语义锁 + 集成。锁死财报排雷档位与 d2_compose 接入（P2 缺口修复）。"""
import os
import pytest

from tools.pyramid.registry import get
from tools.pyramid.tools.financial_redflag_tool import _redflag_verdict, _QUALITY档
from tools.pyramid._common import 格档
from tools.pyramid import d2_compose as C
import tools.pyramid.tools  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


# ── quality 档位锁（D1 §4：80/65/50/35）──
def test_quality档位():
    assert 格档(85, _QUALITY档)[0] == "优"
    assert 格档(70, _QUALITY档)[0] == "良"
    assert 格档(55, _QUALITY档)[0] == "中"
    assert 格档(45, _QUALITY档)[0] == "弱"
    assert 格档(30, _QUALITY档)[0] == "差"


# ── verdict 分档锁 ──
def test_verdict_高危():
    lvl, r = _redflag_verdict("差", 45.0, ["现金含量不足"],
                              [{"code": "现金含量不足", "严重度": "中"}], {"扣非净利增速": -5})
    assert lvl == "高危"  # 评级差 + quality<50 + 扣非负


def test_verdict_无嫌疑():
    lvl, r = _redflag_verdict("优", 85.0, [], [], {"扣非净利增速": 30})
    assert lvl == "无嫌疑" and r == []


def test_verdict_中_扣非负():
    lvl, r = _redflag_verdict("良", 70.0, [], [], {"扣非净利增速": -3})
    assert lvl == "中" and any("扣非" in x for x in r)


# ── d2_compose 接入：财报高危 → veto ──
def test_财报高危_veto():
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, {"嫌疑档": "高危"}) == "财报高危红旗"
    # 无 fin 时不影响旧行为
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None) is None


def test_排雷子分_财报降分():
    base = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None)
    中 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, {"嫌疑档": "中"})
    assert base == 100 and 中 == 70  # 中危 -30


# ── 真数据：002025 应被判财报高危（P2 缺口实证）──
def test_002025_财报高危():
    if not os.path.exists(os.path.join(ROOT, "data", "analysis", AS_OF, "002025.json")):
        pytest.skip("无 002025 数据")
    res = get("financial_redflag").run(AS_OF, "002025", root=ROOT)
    if res.fields.get("数据缺"):
        pytest.skip("无 financial 字段")
    assert res.fields["评级"] == "差"
    assert res.fields["嫌疑档"] == "高危"  # 骨架此前漏的雷，现在抓到
