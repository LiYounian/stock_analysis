"""insider_reduction 语义锁 + d2_compose 接入 + 防未来 + 中性化 + 真数据用。

锁死：减持嫌疑档档位表（无/低/中/高 × 关键词判据）、高→veto「清仓式减持」、
排雷子分降分（中-25/低-12）、无 reduce 不改旧行为、缺 events→missing、
防未来（date>as_of 不计）、协议转让/未减持/增持类中性化不误杀、高危否决不进排序不双算。

真数据：events 里减持低频；用主仓真样本（2026-08-11/603087 有减持计划公告）做覆盖对；
缺数据则 skip 不失败。测试数据指主仓用 REDUCE_TEST_DATA_ROOT（照 unlock 写法）。
"""
import json
import os
import pytest

from tools.pyramid.registry import get
from tools.pyramid.tools.insider_reduction_tool import (
    _severity, _verdict, _candidates, RECENT_DAYS,
)
from tools.pyramid import d2_compose as C
import tools.pyramid.tools  # noqa: F401  触发注册

# data/(analysis) 常在主仓(worktree gitignored)；用 env 指过去，缺省=本仓根
ROOT = os.environ.get("REDUCE_TEST_DATA_ROOT") or \
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _write_pstock(tmp_path, as_of, code, events):
    """构造 data/analysis/<as_of>/<code>.json（events 列表；None=无 events 键），返回 root。"""
    d = tmp_path / "data" / "analysis" / as_of
    d.mkdir(parents=True, exist_ok=True)
    payload = {"events": events} if events is not None else {"snapshot": {}}
    (d / f"{code}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(tmp_path)


def _ev(date, title, type="减持", impact="利空"):
    return {"date": date, "type": type, "impact": impact, "title": title, "summary": ""}


# ── 单条严重度判据锁（纯函数·核心语义锁）──────────────────
def test_severity判据():
    assert _severity(_ev("2026-09-01", "控股股东清仓式减持股份计划")) == "high"
    assert _severity(_ev("2026-09-01", "控股股东大额减持股份计划")) == "high"
    assert _severity(_ev("2026-09-01", "股东大额减持计划公告")) == "mid"
    assert _severity(_ev("2026-09-01", "关于预披露减持计划的公告")) == "mid"
    assert _severity(_ev("2026-09-01", "董事、高级管理人员减持股份计划公告")) == "normal"
    # 中性化：协议转让 / 增持 / 未减持 / impact=利好
    assert _severity(_ev("2026-09-01", "股东协议转让引入战略投资者")) == "neutral"
    assert _severity(_ev("2026-09-01", "关于减持计划期限届满暨未减持股份的公告")) == "neutral"
    assert _severity(_ev("2026-09-01", "一致行动人增持计划", type="权益变动", impact="利好")) == "neutral"
    # 控股股东但非大额/非清仓 → 常规（v1 不自动升中，v2 才量化）
    assert _severity(_ev("2026-09-01", "控股股东减持股份计划公告")) == "normal"


# ── 档位表锁（严重度列表 → 档）────────────────────────────
def test_verdict档位():
    assert _verdict([]) == ("无嫌疑", 0)
    assert _verdict(["neutral", "neutral"]) == ("无嫌疑", 0)   # 全中性化
    assert _verdict(["normal"]) == ("低", 1)                   # 单条常规
    assert _verdict(["normal", "normal"]) == ("中", 2)         # ≥2 条
    assert _verdict(["mid"]) == ("中", 1)                      # 单条大额/预披露
    assert _verdict(["high", "normal"]) == ("高", 2)           # 含高 → 高
    assert _verdict(["neutral", "normal"]) == ("低", 1)        # 中性不计入命中条数


# ── 防未来：候选窗口 date≤as_of 且窗内 ──────────────────────
def test_候选防未来():
    as_of = "2026-09-17"
    evs = [
        _ev("2026-09-20", "股东减持计划公告"),   # 未来 → 剔除
        _ev("2026-09-10", "股东减持计划公告"),   # 窗内 → 计入
        _ev("2026-07-01", "股东减持计划公告"),   # 超 30 日窗 → 剔除
        _ev("2026-09-05", "利好公告", type="业绩预增", impact="利好"),  # 非候选类型
    ]
    cand = _candidates(evs, as_of)
    assert len(cand) == 1 and cand[0]["date"] == "2026-09-10"


# ── d2_compose 接入：高 → veto「清仓式减持」──────────────
def test_减持高_veto():
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None, None,
                         {"减持嫌疑档": "高"}) == "清仓式减持"
    # 无 reduce 不影响旧行为（回归锁）
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None, None, None) is None
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}) is None
    # 假利好/解禁 veto 仍优先命中（顺序未被破坏）
    assert C.veto_reason({"嫌疑档": "高"}, {}, None, None,
                         {"减持嫌疑档": "高"}) == "假利好高嫌疑"
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {}, None,
                         {"解禁嫌疑档": "高危"}, {"减持嫌疑档": "高"}) == "大额解禁临近"


# ── 排雷子分降分锁（中-25 / 低-12）────────────────────────
def test_排雷子分_减持降分():
    base = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None, None)
    中 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None, {"减持嫌疑档": "中"})
    低 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None, {"减持嫌疑档": "低"})
    无 = C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None, {"减持嫌疑档": "无嫌疑"})
    assert base == 100 and 中 == 75 and 低 == 88 and 无 == 100  # 中-25 低-12
    # missing 不扣分
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None,
                     {"减持嫌疑档": "missing"}) == 100
    # 解禁 + 减持叠加（解禁中-25 叠 减持低-12 = 63）
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None,
                     {"解禁嫌疑档": "中"}, {"减持嫌疑档": "低"}) == 63


def test_排雷子分_多负向源叠加_clip100兜底不为负():
    """5 个负向源同时重罚（含多个中/高档 → 总扣分远超 100）时，_clip100 必须兜底归 0，
    绝不为负。防以后重写把 clip 去掉或写错方向（统筹回归锁要求）。"""
    s = C.子分_排雷(
        {"嫌疑档": "中"},                                  # 假利好中 → 从 100 起扣到 40
        {"命中规则": [{"状态": "现行", "环节": "排雷"},     # 经验硬排雷 2 条 → -30
                     {"状态": "现行", "环节": "排雷"}]},
        {"嫌疑档": "中"},                                   # 财报中 → -30
        {"解禁嫌疑档": "中"},                                # 解禁中 → -25
        {"减持嫌疑档": "中"},                                # 减持中 → -25
    )
    assert s == 0.0        # 累计远超 100，_clip100 兜底归 0
    assert s >= 0.0        # 永不为负（方向锁）
    # 全高危同时命中也归 0（高危虽走 veto，但子分本身仍须 clip 不为负）
    s2 = C.子分_排雷({"嫌疑档": "高"}, {"命中规则": []},
                   {"嫌疑档": "高危"}, {"解禁嫌疑档": "高危"}, {"减持嫌疑档": "高"})
    assert s2 == 0.0


# ── 高危否决不进排序·不双算 ───────────────────────────────
def test_减持高_否决不双算():
    干净 = {"嫌疑档": "无嫌疑"}
    row = C.compose_one("999999", ["council"], pv={}, gate={}, sec={},
                        fake=干净, exp={"命中规则": []}, fin=None, unlock=None,
                        reduce={"减持嫌疑档": "高", "命中条数": 2})
    assert row.否决 == "清仓式减持"        # → build_skeleton 归入排雷否决，不进排序
    assert row.fields快照["减持"] == "高"
    # 无 reduce 的同票不被否决（回归·高 100 不会误伤干净票）
    ok = C.compose_one("999998", ["council"], pv={}, gate={}, sec={},
                       fake=干净, exp={"命中规则": []}, fin=None, unlock=None, reduce=None)
    assert ok.否决 is None


# ── 端到端（tmp fixture）：档位 + fields ──────────────────
def test_e2e_高_清仓式(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999001", [
        _ev("2026-09-10", "控股股东清仓式减持股份计划公告"),
    ])
    res = get("insider_reduction").run(as_of, "999001", root=root)
    assert res.fields["减持嫌疑档"] == "高"
    assert res.fields["命中条数"] == 1
    assert res.fields["方式标签"] == "清仓式减持"
    assert res.fields["主体标签"] == "控股股东"
    assert res.freshness == "fresh" and res.防未来 is True


def test_e2e_中_多条(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999002", [
        _ev("2026-09-05", "股东减持股份计划公告"),
        _ev("2026-09-12", "董事减持股份计划公告"),
    ])
    res = get("insider_reduction").run(as_of, "999002", root=root)
    assert res.fields["减持嫌疑档"] == "中" and res.fields["命中条数"] == 2
    assert res.fields["最近减持日"] == "2026-09-12"


def test_e2e_低_单条常规(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999003", [
        _ev("2026-09-10", "董事、高级管理人员减持股份计划公告"),
    ])
    res = get("insider_reduction").run(as_of, "999003", root=root)
    assert res.fields["减持嫌疑档"] == "低" and res.fields["命中条数"] == 1
    assert res.fields["主体标签"] == "董监高"


def test_e2e_协议转让中性化不误杀(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999004", [
        _ev("2026-09-10", "股东协议转让引入战略投资者暨权益变动", type="权益变动"),
        _ev("2026-09-11", "关于减持计划期限届满暨未减持股份的公告"),
    ])
    res = get("insider_reduction").run(as_of, "999004", root=root)
    assert res.fields["减持嫌疑档"] == "无嫌疑" and res.fields["命中条数"] == 0
    assert res.fields["窗内候选数"] == 2   # 候选存在但全中性化
    # 无嫌疑不扣分（回归）
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {"命中规则": []}, None, None, res.fields) == 100


def test_e2e_窗内无减持_无嫌疑(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999005", [
        _ev("2026-09-10", "业绩预增公告", type="业绩", impact="利好"),
    ])
    res = get("insider_reduction").run(as_of, "999005", root=root)
    assert res.fields["减持嫌疑档"] == "无嫌疑" and res.fields["窗内候选数"] == 0


def test_e2e_防未来_未来公告不计(tmp_path):
    as_of = "2026-09-17"
    root = _write_pstock(tmp_path, as_of, "999006", [
        _ev("2026-09-25", "控股股东清仓式减持股份计划公告"),  # 未来 → 不可见
    ])
    res = get("insider_reduction").run(as_of, "999006", root=root)
    assert res.fields["减持嫌疑档"] == "无嫌疑" and res.fields["窗内候选数"] == 0


# ── 缺 events → missing（不编造）──────────────────────────
def test_缺events_missing(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999007", None)  # 无 events 键
    res = get("insider_reduction").run("2026-09-17", "999007", root=root)
    assert res.fields["减持嫌疑档"] == "missing" and res.freshness == "missing"


def test_缺文件_missing(tmp_path):
    res = get("insider_reduction").run("2026-09-17", "000000", root=str(tmp_path))
    assert res.fields["减持嫌疑档"] == "missing" and res.freshness == "missing"


# ── 浓缩块 ≤8 行硬约束（G3）──────────────────────────────
def test_浓缩块行数上限(tmp_path):
    root = _write_pstock(tmp_path, "2026-09-17", "999008", [
        _ev("2026-09-10", "控股股东大额减持股份计划公告"),
        _ev("2026-09-12", "董事减持股份计划公告"),
    ])
    res = get("insider_reduction").run("2026-09-17", "999008", root=root)
    assert len([l for l in res.浓缩块.splitlines() if l.strip()]) <= 8


# ── 真数据覆盖对验证（主仓 2026-08-11/603087 有减持计划公告）──
def test_真数据_有减持公告():
    as_of = "2026-08-11"
    p = os.path.join(ROOT, "data", "analysis", as_of, "603087.json")
    if not os.path.exists(p):
        pytest.skip("无 2026-08-11/603087 主仓数据（worktree gitignored·设 REDUCE_TEST_DATA_ROOT）")
    res = get("insider_reduction").run(as_of, "603087", root=ROOT)
    # 该票 2026-08-07 "董事、高级管理人员减持股份计划公告" → 单条常规 → 低
    assert res.fields["减持嫌疑档"] == "低"
    assert res.fields["命中条数"] == 1
    assert res.fields["主体标签"] == "董监高"
    assert res.freshness == "fresh" and res.防未来 is True
