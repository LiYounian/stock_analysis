"""S4 结构化研判 测试——锁"读S3 raw·解耦研判·并行分批·失败隔离"的语义:

1. 研判吃 S3 raw 的 items(采集与研判解耦):verdict 收到的 items 来自 raw、非实时采集。
2. 组装输出:龙头/中军/主力/资金流 从 raw 角色/资金流带出;消息标签取 LLM 消息面。
3. 全板块并行:多板块都被研判(顺序无关);单板块抛错被隔离、不拖垮整批。
4. write_catalyst:格式含 rubric/schema/model,利好板块正确抽取。
5. 沿用 board_verdict_instruction+BOARD_VERDICT_SCHEMA(不重造评分口径)。
"""
import json

import pytest

from tools.analysis.sector_forecast import board_verdict_s4 as S4


def _raw(sw, items, roles, flows=None):
    return {"板块": sw, "as_of": "2026-09-16", "items": items, "角色": roles,
            "资金流": flows or []}


@pytest.fixture
def _patch(monkeypatch):
    """mock LLM 研判 + S3 raw 加载;记录 verdict 收到的 items 以验解耦。"""
    seen = {}

    def _apply(*, raws, verdict=None, fail_boards=()):
        import tools.analysis.sector_forecast.board_news_collect as BC
        monkeypatch.setattr(BC, "load_board_raw",
                            lambda date, sw, out_root=None: raws.get(sw))
        monkeypatch.setattr(S4, "_boards_with_raw",
                            lambda date, out_root=None: list(raws.keys()))

        import tools.analysis.sector_forecast.news_catalyst as NC

        def _v(date, sw, leads, *, client=None, items=None):
            seen[sw] = items                       # 记录:研判拿到的 items
            if sw in fail_boards:
                raise RuntimeError("boom")
            return (verdict or {}).get(sw, {"消息面": "中性", "强弱": "弱", "关键事件": [],
                                            "持续性": "", "时效": "", "可靠性综述": "",
                                            "理由": "", "n条": len(items or []), "新增": 0})
        monkeypatch.setattr(NC, "board_news_verdict", _v)

        import tools.llm.client as lc
        monkeypatch.setattr(lc, "get_client", lambda *a, **k: object())
        return seen
    return _apply


def test_verdict_consumes_raw_items(_patch):
    raws = {"电子": _raw("电子", [{"date": "2026-09-15", "who": "龙头·甲", "title": "t", "text": "x"}],
                        [{"code": "A", "name": "甲", "role": "龙头"}])}
    seen = _patch(raws=raws)
    cat = S4.board_catalyst_from_raw("2026-09-16", workers=2)
    # 研判拿到的 items 正是 S3 raw 的 items(解耦:没有实时采集)
    assert seen["电子"][0]["title"] == "t"
    assert "电子" in cat


def test_assembles_roles_and_flows(_patch):
    raws = {"电子": _raw("电子", [],
                        [{"code": "A", "name": "甲", "role": "龙头"},
                         {"code": "B", "name": "乙", "role": "中军"},
                         {"code": "C", "name": "丙", "role": "主力"}],
                        flows=[{"code": "C", "方向": "净买入", "累计净买亿": 2.0}])}
    _patch(raws=raws, verdict={"电子": {"消息面": "利好", "强弱": "强", "关键事件": [],
                                       "持续性": "", "时效": "", "可靠性综述": "", "理由": "r"}})
    cat = S4.board_catalyst_from_raw("2026-09-16")
    c = cat["电子"]
    assert c["消息标签"] == "利好" and c["强弱"] == "强"
    assert [x["code"] for x in c["龙头"]] == ["A"]
    assert [x["code"] for x in c["中军"]] == ["B"]
    assert [x["code"] for x in c["主力"]] == ["C"]
    assert c["资金流"][0]["方向"] == "净买入"


def test_parallel_all_boards_and_failure_isolated(_patch):
    raws = {sw: _raw(sw, [], [{"code": "A", "name": "甲", "role": "龙头"}])
            for sw in ["电子", "计算机", "银行", "军工"]}
    _patch(raws=raws, fail_boards={"银行"})
    cat = S4.board_catalyst_from_raw("2026-09-16", workers=3)
    assert set(cat.keys()) == {"电子", "计算机", "军工"}   # 银行失败被隔离,其余都出
    assert "银行" not in cat


def test_write_catalyst_format(_patch, tmp_path):
    raws = {"电子": _raw("电子", [], [{"code": "A", "name": "甲", "role": "龙头"}])}
    _patch(raws=raws, verdict={"电子": {"消息面": "利好", "强弱": "强", "关键事件": [],
                                       "持续性": "", "时效": "", "可靠性综述": "", "理由": "r"}})
    cat = S4.board_catalyst_from_raw("2026-09-16")
    p = S4.write_catalyst("2026-09-16", cat, out_root=str(tmp_path))
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["利好板块"] == ["电子"]
    assert "rubric" in d and "board_verdict_schema" in d
    assert "deepseek" in d["model"].lower()
    assert d["板块研判"]["电子"]["消息标签"] == "利好"


def test_no_raw_returns_empty(_patch):
    _patch(raws={})
    cat = S4.board_catalyst_from_raw("2026-09-16")
    assert cat == {}


def test_board_desc_condenses_with_rubric():
    """金字塔④ 描述凝练:board_desc 把研判凝成塔尖可读一段·用 rubric_map 标准强弱口径·带催化/资金流。"""
    from tools.llm import rubric_map as rm
    entry = {
        "消息标签": "利好", "强弱": "强", "理由": "光缆集采涨价+AI算力景气共振",
        "持续性": "近两周多条同向", "时效": "本周新",
        "可靠性综述": "一手为主",
        "关键事件": [
            {"时间": "2026-09-07", "事件": "中国移动光缆集采70.998亿涨价1.15倍", "方向": "利好",
             "影响程度": "大", "来源": "一手"},
            {"时间": "2026-09-07", "事件": "某研报观点", "方向": "利好", "影响程度": "小", "来源": "二手"},
        ],
        "资金流": [{"code": "601869", "name": "长飞光纤", "方向": "净买入", "累计净买亿": 1.41}],
    }
    desc = S4.board_desc(entry, "电子")
    assert "消息面：利好·强" in desc                        # 用 rubric_map 档标签(标准定义塔尖glossary共享一次)
    assert rm.strength_desc("强") not in desc              # 防过载:不内联长定义(§4.1 凝练)
    assert "光缆集采70.998亿" in desc                      # 影响大的催化优先入
    assert "某研报观点" not in desc                        # 影响小的被凝练掉(防过载)
    assert "长飞光纤" in desc and "1.41" in desc           # 主力净买领先入描述
    assert "持续性：近两周多条同向" in desc


def test_verdict_includes_desc(_patch):
    """_one_verdict 输出带 additive「描述」字段(供塔尖读)。"""
    raws = {"电子": _raw("电子", [], [{"code": "A", "name": "甲", "role": "龙头"}])}
    _patch(raws=raws, verdict={"电子": {"消息面": "利好", "强弱": "强", "关键事件": [],
                                       "持续性": "", "时效": "", "可靠性综述": "", "理由": "r"}})
    cat = S4.board_catalyst_from_raw("2026-09-16")
    assert "描述" in cat["电子"] and "消息面：利好·强" in cat["电子"]["描述"]
