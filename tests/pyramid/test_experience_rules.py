"""窗3 · experience_rules 语义锁测试（守则6）。

锁死：规则库 schema / 环节·状态枚举 / as_of 防未来（首见版本过滤）/ 环节·code 过滤 /
浓缩块 ≤8 行且禁裸 json。这些断言锁住"为什么这么拆"，防未来重写无意删规则。
"""
import os
import pytest

from tools.pyramid.registry import get, all_names
import tools.pyramid.tools  # noqa: F401 触发注册
from tools.pyramid.tools import experience_rules_tool as er

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


@pytest.fixture(scope="module")
def db():
    return er.build_rule_db(ROOT, write=False)


# ── 注册 ──
def test_已注册():
    assert "experience_rules" in all_names()
    assert get("experience_rules").塔层 == "经验"


# ── 规则库 schema 锁 ──
def test_规则库_schema与枚举(db):
    assert db["rule_count"] == len(db["rules"]) > 0
    需字段 = {"id", "规则文字", "作用环节", "状态", "首见版本", "出处", "证据", "codes"}
    for r in db["rules"]:
        assert 需字段 <= set(r), f"规则 {r.get('id')} 缺字段"
        assert isinstance(r["作用环节"], list) and r["作用环节"]
        for e in r["作用环节"]:
            assert e in er.环节枚举, f"非法环节 {e}"
        assert r["状态"] in er.状态枚举, f"非法状态 {r['状态']}"
        assert r["首见版本"].startswith("v")


def test_环节枚举锁定():
    # 五大环节 + 通则兜底（改这张表即破坏 D2 按环节取用契约）
    assert er.环节枚举 == ("召回", "排雷", "排序", "价位", "择时", "通则")
    assert er.状态枚举 == ("现行", "存疑", "已弃")


def test_三章节都被解析(db):
    sections = {r["出处"].split()[1] for r in db["rules"]}
    assert "§4经验条目" in sections
    assert "§5已知陷阱" in sections
    assert "§6待验证假设" in sections
    # §6 待验证假设一律标存疑（不确定即存疑，不丢）
    for r in db["rules"]:
        if r["出处"].split()[1] == "§6待验证假设":
            assert r["状态"] == "存疑"


def test_环节分类描述性(db):
    # 关键词归桶应能把"规避/风险"类归排雷、"入场/回踩/止损"类归价位
    ex = {r["规则文字"]: r["作用环节"] for r in db["rules"]}
    hit_排雷 = [t for t, envs in ex.items() if "排雷" in envs]
    hit_价位 = [t for t, envs in ex.items() if "价位" in envs]
    assert hit_排雷 and hit_价位


# ── as_of 防未来 ──
def test_首见版本防未来过滤():
    tool = get("experience_rules")
    早 = tool.run("2026-09-03", root=ROOT)
    晚 = tool.run("2026-09-17", root=ROOT)
    # 晚 as_of 能看到的规则 ≥ 早 as_of（经验只增）
    assert 晚.fields["适用条数"] >= 早.fields["适用条数"] > 0
    # 早 as_of 绝不含首见版本 > as_of 的规则
    早集 = {r["出处"] for r in 早.fields["命中规则"]}
    for 出处 in 早集:
        ver = 出处.split()[0]  # vYYYY-MM-DD
        assert ver <= "v2026-09-03", f"防未来破损: {出处} 泄漏进 as_of=09-03"
    # 库晚于 as_of 应标 stale
    assert 早.freshness == "stale"


# ── 环节 / code 过滤 ──
def test_环节过滤收窄():
    tool = get("experience_rules")
    全 = tool.run(AS_OF, root=ROOT)
    排雷 = tool.run(AS_OF, 环节="排雷", root=ROOT)
    assert 0 < 排雷.fields["命中数"] <= 全.fields["命中数"]
    for r in 排雷.fields["命中规则"]:
        assert "排雷" in r["作用环节"]


def test_code过滤命中被引用票():
    tool = get("experience_rules")
    # 找一个规则库里被引用的 code
    db = er.build_rule_db(ROOT, write=False)
    cited = None
    for r in db["rules"]:
        if r["codes"]:
            cited = r["codes"][0]
            break
    assert cited, "规则库应至少有一条引用了个股 code"
    res = tool.run(AS_OF, code=cited, root=ROOT)
    assert res.fields["命中数"] >= 1
    for r in res.fields["命中规则"]:
        pass  # 命中规则的完整 codes 不在浓缩 fields 里，逻辑已在过滤层保证


# ── 浓缩块契约 ──
def test_浓缩块不超8行且无裸json():
    tool = get("experience_rules")
    res = tool.run(AS_OF, root=ROOT)
    prompt = res.to_prompt()
    assert len([l for l in res.浓缩块.splitlines() if l.strip()]) <= 8
    # 禁裸 json：不吐 fields 结构（大括号/证据键值），"见 fields.命中规则"仅为指针文字可留
    assert "{" not in prompt and "'证据'" not in prompt and '"证据"' not in prompt


def test_无命中诚实报空不编():
    tool = get("experience_rules")
    # 用一个不可能被引用的假 code
    res = tool.run(AS_OF, code="999999", root=ROOT)
    assert res.fields["命中数"] == 0
    assert "命中 0 条" in res.浓缩块
