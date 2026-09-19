"""Wave2 · stock_sentiment 语义锁测试（守则6：锁住"为什么改"防未来重写删规则）。

锁死：三张档位表边界 / 写死常量(近窗60·专用类型剔除) / 口径三段拒空编 /
events 空与 consensus None 容错 / 净情绪分 None 降级 stale / 三层原样解读 /
公告 tally 剔除{减持,权益变动,解禁}·增持保留 / events 防未来+近窗 / ugc 源未接入标 NA /
无 json 标 missing 不编 / 浓缩块 ≤8 行。
"""
import json
import os

import pytest

from tools.pyramid.registry import get, all_names
from tools.pyramid._common import 字段, 格档
import tools.pyramid.tools  # noqa: F401 触发注册
from tools.pyramid.tools import stock_sentiment_tool as ss

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-18"


def test_已注册():
    assert "stock_sentiment" in all_names()
    t = get("stock_sentiment")
    assert t.塔层 == "②消息"
    assert t.面 == "消息情绪面"


# ── 写死常量语义锁 ──
def test_写死常量锁定():
    assert ss.RECENT_EVENT_DAYS == 60
    assert ss.专用事件类型 == ("减持", "权益变动", "解禁")


# ── 三张档位表边界锁（改这些即改变"档位怎么分"）──
def test_净情绪档_边界锁():
    assert 格档(-0.5, ss.净情绪档)[0] == "强负"
    assert 格档(-0.30, ss.净情绪档)[0] == "强负"   # ≤上界命中
    assert 格档(-0.29, ss.净情绪档)[0] == "负"
    assert 格档(-0.05, ss.净情绪档)[0] == "负"
    assert 格档(-0.04, ss.净情绪档)[0] == "中性"
    assert 格档(0.05, ss.净情绪档)[0] == "中性"
    assert 格档(0.06, ss.净情绪档)[0] == "正"
    assert 格档(0.30, ss.净情绪档)[0] == "正"
    assert 格档(0.31, ss.净情绪档)[0] == "强正"
    assert 格档(0.9, ss.净情绪档)[0] == "强正"
    assert 格档(None, ss.净情绪档)[0] == "无档"


def test_覆盖率档_边界锁():
    assert 格档(0.0, ss.覆盖率档)[0] == "低覆盖"
    assert 格档(0.5, ss.覆盖率档)[0] == "低覆盖"
    assert 格档(0.51, ss.覆盖率档)[0] == "部分覆盖"
    assert 格档(0.9, ss.覆盖率档)[0] == "部分覆盖"
    assert 格档(0.91, ss.覆盖率档)[0] == "全覆盖"
    assert 格档(1.0, ss.覆盖率档)[0] == "全覆盖"


def test_研报覆盖档_边界锁():
    assert 格档(0, ss.研报覆盖档)[0] == "无覆盖"
    assert 格档(1, ss.研报覆盖档)[0] == "冷门"
    assert 格档(2, ss.研报覆盖档)[0] == "冷门"
    assert 格档(3, ss.研报覆盖档)[0] == "一般"
    assert 格档(9, ss.研报覆盖档)[0] == "一般"
    assert 格档(10, ss.研报覆盖档)[0] == "高关注"
    assert 格档(35, ss.研报覆盖档)[0] == "高关注"


# ── 口径三段拒空编（契约底线）──
def test_字段_拒空编():
    with pytest.raises(ValueError):
        字段("x", 1.0, "", "意味")          # 口径空
    with pytest.raises(ValueError):
        字段("x", 1.0, "口径", "")          # 意味空
    # 值 None → NA 合法（缺数据标记）
    d = 字段("x", None, "口径", "意味")
    assert d["值"] == "NA"


# ── run() 全路径：合成 data-root ──
def _write(tmp_path, code, payload):
    adir = tmp_path / "data" / "analysis" / AS_OF
    adir.mkdir(parents=True, exist_ok=True)
    with open(adir / f"{code}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _run(tmp_path, code):
    return get("stock_sentiment").run(AS_OF, code, root=str(tmp_path))


def test_run_rich_全字段(tmp_path):
    _write(tmp_path, "300001", {
        "sentiment": {
            "净情绪分": 0.20, "利好数": 3, "利空数": 1, "样本数": 12,
            "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜",
            "口径": "三层加权 新闻0.5/政策0.3/舆情0.2,缺层重归一",
            "三层": {"新闻": {"净情绪": 0.25}, "政策": {"净情绪": 0.0},
                     "舆情": {"净情绪": 0.1, "多空": "偏多"}},
        },
        "events": [{"date": "2026-09-10", "type": "回购", "impact": "待判", "title": "回购"}],
        "consensus": {"覆盖机构数": 15.0, "预期EPS当年": 0.8, "预期EPS次年": 1.0, "预期增速": 0.25, "新鲜度": "新鲜"},
        "ugc": None,
    })
    r = _run(tmp_path, "300001")
    assert r.面 == "消息情绪面" and r.freshness == "fresh" and r.防未来 is True
    assert r.fields["净情绪档"] == "正"
    assert r.fields["覆盖率档"] == "全覆盖"
    assert r.fields["研报覆盖档"] == "高关注"
    assert r.fields["公告事件数"] == 1 and "回购×1" in r.fields["公告roster"]
    assert r.fields["舆情多空"] == "偏多"
    # 净情绪分口径用 sentiment 原口径原样
    assert "三层加权 新闻0.5/政策0.3/舆情0.2" in r.浓缩块
    # 口径三段四段全非空（契约）
    for it in r.字段解读:
        assert all(str(it.get(k, "")).strip() for k in ("名", "口径", "意味"))
        assert "值" in it
    assert len([l for l in r.浓缩块.splitlines() if l.strip()]) <= 8


def test_run_events空_无新公告(tmp_path):
    _write(tmp_path, "300002", {
        "sentiment": {"净情绪分": 0.0, "利好数": 0, "利空数": 0, "样本数": 5,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权",
                      "三层": {"舆情": {"净情绪": 0.0, "多空": "中性"}}},
        "events": [],
        "consensus": {"覆盖机构数": 1.0, "预期EPS当年": 0.5, "预期EPS次年": 0.6, "预期增速": 0.2},
    })
    r = _run(tmp_path, "300002")
    assert r.fields["公告事件数"] == 0
    assert "无新公告事件" in r.浓缩块
    assert r.fields["研报覆盖档"] == "冷门"


def test_run_净情绪分None_降级stale(tmp_path):
    _write(tmp_path, "300003", {
        "sentiment": {"净情绪分": None, "利好数": 0, "利空数": 0, "样本数": 0,
                      "覆盖率": 0.0, "质量": "missing", "新鲜度": "新鲜", "口径": "三层加权",
                      "三层": {}},
        "events": [{"date": "2026-09-12", "type": "监管/调研", "impact": "待判", "title": "调研"}],
        "consensus": {},
    })
    r = _run(tmp_path, "300003")
    assert r.freshness == "stale"                       # 净情绪分缺 → stale
    assert r.fields["净情绪档"] == "无有效情绪"
    assert "无有效情绪·待补" in r.浓缩块
    assert r.fields["覆盖率档"] == "打分未覆盖"
    assert r.fields["研报覆盖档"] == "无覆盖"            # consensus 空
    assert "无覆盖·待补" in r.浓缩块


def test_run_consensus_None增速_待补(tmp_path):
    _write(tmp_path, "300004", {
        "sentiment": {"净情绪分": -0.4, "利好数": 1, "利空数": 8, "样本数": 20,
                      "覆盖率": 0.95, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权",
                      "三层": {"舆情": {"净情绪": -0.5, "多空": "偏空"}}},
        "events": [],
        "consensus": {"覆盖机构数": 4.0, "预期EPS当年": -0.51, "预期EPS次年": 0.09, "预期增速": None},
    })
    r = _run(tmp_path, "300004")
    assert r.fields["净情绪档"] == "强负"
    assert r.fields["研报覆盖档"] == "一般"
    assert "增速无·待补" in r.浓缩块                      # 预期增速 None → 待补
    assert "预期EPS当年-0.51" in r.浓缩块


def test_run_公告tally剔除专用类型_增持保留(tmp_path):
    """锁：减持/权益变动/解禁 已被 insider_reduction/unlock_risk 占用，tally 剔除避免双计；增持无专用工具、保留。"""
    _write(tmp_path, "300005", {
        "sentiment": {"净情绪分": 0.1, "利好数": 1, "利空数": 0, "样本数": 3,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [
            {"date": "2026-09-10", "type": "回购", "impact": "待判", "title": "回购"},
            {"date": "2026-09-11", "type": "减持", "impact": "利空", "title": "减持"},
            {"date": "2026-09-11", "type": "权益变动", "impact": "待判", "title": "权益变动"},
            {"date": "2026-09-11", "type": "解禁", "impact": "待判", "title": "解禁"},
            {"date": "2026-09-12", "type": "增持", "impact": "利好", "title": "增持"},
        ],
        "consensus": {"覆盖机构数": 6.0, "预期EPS当年": 0.3, "预期EPS次年": 0.4, "预期增速": 0.3},
    })
    r = _run(tmp_path, "300005")
    assert r.fields["公告事件数"] == 2                    # 回购 + 增持
    roster = r.fields["公告roster"]
    assert "回购×1" in roster and "增持×1" in roster
    assert "减持" not in roster and "权益变动" not in roster and "解禁" not in roster
    assert "已剔除减持/权益变动/解禁" in r.浓缩块


def test_run_events防未来与近窗(tmp_path):
    """锁：date>as_of 丢弃（防未来）；龄>60日 丢弃（近窗）。"""
    _write(tmp_path, "300006", {
        "sentiment": {"净情绪分": 0.0, "利好数": 0, "利空数": 0, "样本数": 1,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [
            {"date": "2026-09-20", "type": "重大合同", "impact": "利好", "title": "未来事件"},   # >as_of 丢
            {"date": "2026-06-01", "type": "股权激励", "impact": "待判", "title": "陈年事件"},   # 109日 丢
            {"date": "2026-09-15", "type": "合同订单", "impact": "利好", "title": "近窗事件"},   # 3日 留
        ],
        "consensus": {"覆盖机构数": 3.0, "预期EPS当年": 0.2, "预期EPS次年": 0.3, "预期增速": 0.5},
    })
    r = _run(tmp_path, "300006")
    assert r.fields["公告事件数"] == 1
    assert "合同订单×1" in r.fields["公告roster"]
    assert "重大合同" not in r.浓缩块 and "股权激励" not in r.浓缩块
    assert r.防未来 is True


def test_run_ugc有值原样(tmp_path):
    _write(tmp_path, "300007", {
        "sentiment": {"净情绪分": 0.0, "利好数": 0, "利空数": 0, "样本数": 1,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [],
        "consensus": {"覆盖机构数": 2.0, "预期EPS当年": 0.1, "预期EPS次年": 0.2, "预期增速": 1.0},
        "ugc": {"热度": 88},
    })
    r = _run(tmp_path, "300007")
    assert r.fields["社媒热度"] is not None
    assert "社媒热度" in r.浓缩块


def test_run_无json_missing不编(tmp_path):
    r = _run(tmp_path, "999999")   # 未写任何文件
    assert r.freshness == "missing"
    assert r.fields.get("数据不足") is True
    assert "人工确认" in r.浓缩块
    assert r.面 == "消息情绪面"


def test_run_三层原样解读(tmp_path):
    _write(tmp_path, "300008", {
        "sentiment": {"净情绪分": -0.1, "利好数": 2, "利空数": 5, "样本数": 15,
                      "覆盖率": 0.8, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权",
                      "三层": {"新闻": {"净情绪": -0.107}, "政策": {"净情绪": 0.0},
                               "舆情": {"净情绪": -0.4, "多空": "偏空"}}},
        "events": [],
        "consensus": {"覆盖机构数": 12.0, "预期EPS当年": 1.0, "预期EPS次年": 1.2, "预期增速": 0.2},
    })
    r = _run(tmp_path, "300008")
    assert r.fields["净情绪档"] == "负"
    assert r.fields["覆盖率档"] == "部分覆盖"            # 0.8 ∈ (0.5,0.9]
    assert r.fields["舆情多空"] == "偏空"
    assert "舆情偏空" in r.浓缩块 and "新闻-0.11" in r.浓缩块


# ── v2 新语义锁（守则6：锁住 v2 改的"为什么"，防未来重写删掉）──
def test_v2_样本太少阈值锁定():
    assert ss.样本太少阈值 == 5


def test_v2_消息覆盖与可信度_改名讲人话(tmp_path):
    """v2 §4：'情绪样本质量'→'消息覆盖与可信度'，口径讲'抓N条·约X%成功打分'。"""
    _write(tmp_path, "310001", {
        "sentiment": {"净情绪分": 0.2, "利好数": 3, "利空数": 1, "样本数": 12,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [], "consensus": {},
    })
    r = _run(tmp_path, "310001")
    cov = next(it for it in r.字段解读 if it["名"] == "消息覆盖与可信度")
    assert "情绪样本质量" not in r.浓缩块          # 旧名已弃
    assert "成功打分" in cov["口径"] and "条" in cov["口径"]
    assert cov["意味"].startswith("影响：")


def test_v2_净情绪三态_样本太少非真中性(tmp_path):
    """v2 §4：净情绪≈中性 + 样本≤阈值 → 讲'消息太少非缺数据'，不误报真中性。"""
    _write(tmp_path, "310002", {
        "sentiment": {"净情绪分": 0.0, "利好数": 0, "利空数": 0, "样本数": 2,
                      "覆盖率": 0.7, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [], "consensus": {},
    })
    r = _run(tmp_path, "310002")
    净 = next(it for it in r.字段解读 if it["名"] == "个股净情绪")
    assert "消息太少" in 净["意味"] and "仅2条" in 净["意味"]
    assert "不是缺数据" in 净["意味"]


def test_v2_净情绪三态_样本足真中性(tmp_path):
    """v2 §4：净情绪≈中性 + 样本>阈值 → 判'真中性'（区别于样本太少）。"""
    _write(tmp_path, "310003", {
        "sentiment": {"净情绪分": 0.0, "利好数": 4, "利空数": 4, "样本数": 30,
                      "覆盖率": 1.0, "质量": "ok", "新鲜度": "新鲜", "口径": "三层加权", "三层": {}},
        "events": [], "consensus": {},
    })
    r = _run(tmp_path, "310003")
    净 = next(it for it in r.字段解读 if it["名"] == "个股净情绪")
    assert "真中性" in 净["意味"]
    assert "消息太少" not in 净["意味"]


# ── 真实数据集成冒烟（无数据则跳过）──
def test_真实数据冒烟():
    r = get("stock_sentiment").run("2026-09-18", "003006", root=ROOT)
    if r.fields.get("数据不足"):
        pytest.skip("无 003006 per-stock json（worktree 需 root 指主仓）")
    assert r.面 == "消息情绪面"
    assert r.fields["净情绪档"] in ("强负", "负", "中性", "正", "强正", "无有效情绪")
    assert len([l for l in r.浓缩块.splitlines() if l.strip()]) <= 8
    assert r.防未来 is True
