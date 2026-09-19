"""unlock_risk 语义锁 + d2_compose 接入 + 防未来 + 真数据用。

锁死：解禁嫌疑档档位表（占流通3%/10% × 剩余30/90日）、高危→veto「大额解禁临近」、
排雷子分降分（中-25/低-10）、无 unlock 不改旧行为、缺 financing→missing、
防未来（下一次未披露/已过 as_of 不可见）、剩余天数自算。

真 9-17 样本近期无临近解禁属正常 → 用 tmp fixture 构造临近解禁锁档位；
另用主仓真数据（2026-09-04 有临近解禁的票）做覆盖对验证（缺数据则 skip 不失败）。
"""
import json
import os
import pytest

from tools.pyramid.registry import get
from tools.pyramid.tools.unlock_risk_tool import (
    _unlock_verdict, _占比_低线, _占比_高线, _临近_天,
)
from tools.pyramid import d2_compose as C
import tools.pyramid.tools  # noqa: F401  触发注册

# data/(analysis) 常在主仓(worktree gitignored)；用 env 指过去，缺省=本仓根
ROOT = os.environ.get("UNLOCK_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_pstock(tmp_path, as_of, code, 解禁块):
    """构造 data/analysis/<as_of>/<code>.json（仅 financing.解禁），返回 root。"""
    d = tmp_path / "data" / "analysis" / as_of
    d.mkdir(parents=True, exist_ok=True)
    payload = {"financing": {"解禁": 解禁块}} if 解禁块 is not None else {"snapshot": {}}
    (d / f"{code}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


# ── 档位表锁（纯函数·核心语义锁）──────────────────────
def test_verdict档位边界():
    # 占比 < 3% → 低（不论剩余）
    assert _unlock_verdict(2.99, 5, False) == "低"
    assert _unlock_verdict(2.99, 200, False) == "低"
    # 3% ≤ 占比 < 10%：临近(≤30)=中，尚远(>30)=低
    assert _unlock_verdict(3.0, _临近_天, False) == "中"
    assert _unlock_verdict(9.99, 30, False) == "中"
    assert _unlock_verdict(5.0, 31, False) == "低"
    # 占比 ≥ 10%：临近=高危(veto候选)，尚远=中
    assert _unlock_verdict(10.0, 30, False) == "高危"
    assert _unlock_verdict(50.0, 1, False) == "高危"
    assert _unlock_verdict(10.0, 31, False) == "中"


def test_verdict占比缺保守低():
    assert _unlock_verdict(None, 5, True) == "低"      # ratio_missing
    assert _unlock_verdict(None, 5, False) == "低"     # 占比 None 也保守
    # 阈值常量未被悄悄改动
    assert (_占比_低线, _占比_高线, _临近_天) == (3.0, 10.0, 30)


# ── d2_compose 接入：高危 → veto ──────────────────────
def test_解禁高危_veto():
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None,
                         {"解禁嫌疑档": "高危"}) == "大额解禁临近"
    # 无 unlock 不影响旧行为（回归锁）
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None, None) is None
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}) is None
    # 假利好/财报 veto 仍优先命中（顺序未被破坏）
    assert C.veto_reason({"嫌疑档": "高"}, {}, None,
                         {"解禁嫌疑档": "高危"}) == "假利好高嫌疑"


def test_排雷子分_解禁降分():
    base = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None)
    中 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, {"解禁嫌疑档": "中"})
    低 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, {"解禁嫌疑档": "低"})
    无 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, {"解禁嫌疑档": "无嫌疑"})
    assert base == 100 and 中 == 75 and 低 == 90 and 无 == 100  # 中-25 低-10
    # missing 中性扣分（#6：未深采≠查证干净，解禁 missing -5）
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None,
                     {"解禁嫌疑档": "missing"}) == 95


def test_排雷子分_财报解禁叠加():
    # 财报中(-30) 叠 解禁低(-10) = 60
    s = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []},
                  {"嫌疑档": "中"}, {"解禁嫌疑档": "低"})
    assert s == 60


def test_高危_否决不进排序_不双算():
    """高危既走 veto 又有 _解禁档_扣[高危]=100——锁定：veto 命中的票被踢出排序、
    子分那 100 不叠到最终排序分（compose_one 标 否决，build_skeleton 归 排雷否决）。"""
    干净 = {"嫌疑档": "无嫌疑"}
    row = C.compose_one(
        "999999", ["council"], pv={}, gate={}, sec={},
        fake=干净, exp={"命中规则": []}, fin=None,
        unlock={"解禁嫌疑档": "高危", "剩余天数": 5},
    )
    assert row.否决 == "大额解禁临近"   # → build_skeleton 归入 排雷否决，不进 排序
    assert row.fields快照["解禁"] == "高危"
    # 无 unlock 的同票不被否决（回归·高危 100 不会误伤干净票）
    ok = C.compose_one("999998", ["council"], pv={}, gate={}, sec={},
                       fake=干净, exp={"命中规则": []}, fin=None, unlock=None)
    assert ok.否决 is None


# ── 端到端（tmp fixture）：档位 + 剩余天数自算 ──────────
def test_e2e_高危_临近大额(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999001", {
        "未来次数": 3, "未来90日次数": 1, "未来90日占流通_pct": 15.5,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-09-30", "披露日": "2023-01-01",
                 "占流通市值_pct": 15.5, "占总股本_pct": 20.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run(as_of, "999001", root=root)
    assert res.fields["解禁嫌疑档"] == "高危"
    assert res.fields["剩余天数"] == 13   # 9-30 − 9-17 自算
    assert res.fields["占流通pct"] == 15.5
    assert res.freshness == "fresh" and res.防未来 is True


def test_e2e_中_临近中额(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999002", {
        "未来次数": 2, "未来90日次数": 1, "未来90日占流通_pct": 6.0,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-10-05", "披露日": "2025-01-01",
                 "占流通市值_pct": 6.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999002", root=root)
    assert res.fields["解禁嫌疑档"] == "中" and res.fields["剩余天数"] == 18


def test_e2e_占比缺保守低(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999003", {
        "未来次数": 1, "未来90日次数": 1, "未来90日占流通_pct": None,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-10-01", "披露日": "2025-01-01",
                 "占流通市值_pct": None, "占总股本_pct": None, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999003", root=root)
    assert res.fields["解禁嫌疑档"] == "低"


# ── 无临近解禁 → 无嫌疑（不扣分）──────────────────────
def test_e2e_无临近_未来90次数0(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999004", {
        "未来次数": 2, "未来90日次数": 0, "未来90日占流通_pct": None,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-12-15", "披露日": "2025-12-17",
                 "占流通市值_pct": 30.0, "自洽性": "可采信"},  # 超 90 日窗
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999004", root=root)
    assert res.fields["解禁嫌疑档"] == "无嫌疑"
    # 无嫌疑不扣分（回归）
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None,
                     res.fields) == 100


def test_e2e_下一次null_无嫌疑(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999005", {
        "未来次数": 0, "未来90日次数": 0, "未来90日占流通_pct": None,
        "不可采信次数": 0, "下一次": None, "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999005", root=root)
    assert res.fields["解禁嫌疑档"] == "无嫌疑"


# ── 防未来 ────────────────────────────────────────────
def test_防未来_披露晚于asof(tmp_path):
    # 下一次在 as_of 时尚未披露 → 不可见 → 无嫌疑
    root = _write_pstock(tmp_path, "2026-09-17", "999006", {
        "未来次数": 1, "未来90日次数": 1, "未来90日占流通_pct": 20.0,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-10-01", "披露日": "2026-09-20",  # 披露日 > as_of
                 "占流通市值_pct": 20.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999006", root=root)
    assert res.fields["解禁嫌疑档"] == "无嫌疑"


def test_解禁日缺或异常_容错无临近(tmp_path):
    # 解禁日缺失 / 格式异常 → 无法确认时点 → 当无临近，不抛错
    root = _write_pstock(tmp_path, "2026-09-17", "999011", {
        "未来次数": 1, "未来90日次数": 1, "未来90日占流通_pct": 20.0,
        "不可采信次数": 0,
        "下一次": {"解禁日": "N/A", "披露日": "2025-01-01",
                 "占流通市值_pct": 20.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999011", root=root)
    assert res.fields["解禁嫌疑档"] == "无嫌疑"
    root2 = _write_pstock(tmp_path, "2026-09-17", "999012", {
        "未来次数": 1, "未来90日次数": 1, "未来90日占流通_pct": 20.0,
        "不可采信次数": 0,
        "下一次": {"披露日": "2025-01-01", "占流通市值_pct": 20.0},  # 无解禁日键
        "明细": [],
    })
    res2 = get("unlock_risk").run("2026-09-17", "999012", root=root2)
    assert res2.fields["解禁嫌疑档"] == "无嫌疑"


def test_防未来_解禁日已过(tmp_path):
    # 下一次解禁日 ≤ as_of（相对 as_of 已解禁）→ 无前视临近
    root = _write_pstock(tmp_path, "2026-09-17", "999007", {
        "未来次数": 1, "未来90日次数": 1, "未来90日占流通_pct": 20.0,
        "不可采信次数": 0,
        "下一次": {"解禁日": "2026-09-10", "披露日": "2025-01-01",
                 "占流通市值_pct": 20.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999007", root=root)
    assert res.fields["解禁嫌疑档"] == "无嫌疑"


# ── 缺 financing → missing（不编造）────────────────────
def test_缺financing_missing(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999008", None)  # 无 financing
    res = get("unlock_risk").run("2026-09-17", "999008", root=root)
    assert res.fields["解禁嫌疑档"] == "missing" and res.freshness == "missing"


def test_缺文件_missing(tmp_path):
    res = get("unlock_risk").run("2026-09-17", "000000", root=str(tmp_path))
    assert res.fields["解禁嫌疑档"] == "missing" and res.freshness == "missing"


# ── 不可采信批次浓缩块标注 ────────────────────────────
def test_不可采信标注(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999009", {
        "未来次数": 5, "未来90日次数": 1, "未来90日占流通_pct": 2.0,
        "不可采信次数": 1,
        "下一次": {"解禁日": "2026-10-01", "披露日": "2025-01-01",
                 "占流通市值_pct": 2.0, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999009", root=root)
    assert "不可采信" in res.浓缩块 and res.fields["不可采信"] == 1


# ── 浓缩块 ≤8 行硬约束（G3·ToolResult 会抛错，这里显式再锁）──
def test_浓缩块行数上限(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999010", {
        "未来次数": 3, "未来90日次数": 1, "未来90日占流通_pct": 15.5,
        "不可采信次数": 2,
        "下一次": {"解禁日": "2026-09-30", "披露日": "2023-01-01",
                 "占流通市值_pct": 15.5, "自洽性": "可采信"},
        "明细": [],
    })
    res = get("unlock_risk").run("2026-09-17", "999010", root=root)
    assert len([l for l in res.浓缩块.splitlines() if l.strip()]) <= 8


# ── 真数据覆盖对验证（主仓 2026-09-04 有临近解禁票）──────
def test_真数据_有临近解禁():
    as_of = "2026-09-04"
    p = os.path.join(ROOT, "data", "analysis", as_of, "603162.json")
    if not os.path.exists(p):
        pytest.skip("无 2026-09-04/603162 主仓数据（worktree gitignored·设 UNLOCK_TEST_DATA_ROOT）")
    res = get("unlock_risk").run(as_of, "603162", root=ROOT)
    # 该票 as_of=9-4 时未来90日占流通≈2.22% (<3%) → 低；下一次 9-21 剩余 17 日
    assert res.fields["解禁嫌疑档"] == "低"
    assert res.fields["剩余天数"] == 17   # 9-21 − 9-4
    assert res.freshness == "fresh" and res.防未来 is True
