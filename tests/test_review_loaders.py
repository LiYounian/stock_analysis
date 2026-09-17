"""tools/review/loaders：读产物→Pick。锚点解析鲁棒 + 依据 code join + 缺文件有声降级。

全程 tmp_path 合成产物，**不碰生产 data**。
"""
from __future__ import annotations

import json

from tools.review.loaders import (
    analysis_dir,
    load_curated_picks,
    load_picks,
    load_today_picks,
    parse_curated_codes,
)

DATE = "2026-09-16"


def _write_today(root, date=DATE):
    d = root / "analysis" / date
    d.mkdir(parents=True)
    doc = {
        "date": date,
        "板块": [
            {
                "board": "传媒",
                "板块消息面": {"tag": "利好", "强弱": "强",
                             "关键事件": [{"事件": "AI长剧", "方向": "利好", "可信度": "可信",
                                        "影响程度": "大", "执行度": "高", "来源": "一手"}]},
                "个股": [
                    {"code": "603000", "name": "人民网", "档": "推荐", "建议分": 7.2,
                     "来源": "板块催化", "角色": "龙头", "策略命中": [],
                     "board": "传媒", "council": {"综合方向": "看多", "财报红旗数": 0},
                     "形态": {"现价": 20.0, "ma5": 19.5, "当日涨跌": 0.05, "位置pos60": 0.6},
                     "入场": "回踩MA5(19.5)限价", "止损": "跌破MA20"},
                ],
            },
            {
                "board": "半导体",
                "板块消息面": {"tag": "利好", "强弱": "中", "关键事件": []},
                "个股": [
                    {"code": "688110", "name": "东芯股份", "档": "推荐", "建议分": 8.0,
                     "来源": "策略直选", "角色": "策略", "策略命中": ["council合议"],
                     "board": None, "council": {"综合方向": "看多", "财报红旗数": 0},
                     "形态": {"现价": 115.8, "ma5": 111.9, "当日涨跌": 0.048}},
                ],
            },
        ],
    }
    (d / f"今日选股_{date}.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return d


def test_load_today_picks_fields(tmp_path):
    _write_today(tmp_path)
    picks = load_today_picks(DATE, data_root=str(tmp_path))
    assert len(picks) == 2
    by = {p.code: p for p in picks}
    p = by["603000"]
    assert p.来源 == "板块催化" and p.角色 == "龙头" and p.version_tag == "今日选股"
    assert p.council["综合方向"] == "看多"
    assert p.形态["ma5"] == 19.5
    # board 级板块消息面挂到逐票（催化证据）
    assert p.板块消息面["强弱"] == "强"
    assert p.板块消息面["关键事件"][0]["来源"] == "一手"
    assert p.入场_text == "回踩MA5(19.5)限价"
    # 策略直选票 board 为 None（今日选股 rec.board=None）
    assert by["688110"].来源 == "策略直选"


def test_parse_curated_codes_robust():
    md = (
        "# 统筹精选 v2\n\n## 一、大盘（塔尖）\n- 塔牌(水泥0.64) 不是代码\n\n"
        "## 三、逐票\n\n"
        "### 1. 复旦微电 688385（首选·最强）\n- 内容\n\n"
        "### 2. 中芯国际 688981（龙头）\n\n"
        "### 3. 沪电股份 002463（PCB）\n\n"
        "### 3. 沪电股份 002463（重复不该再计）\n"
    )
    got = parse_curated_codes(md)
    assert got == [("复旦微电", "688385"), ("中芯国际", "688981"), ("沪电股份", "002463")]
    # 正文里的 "水泥0.64" 不是 6 位、不误抓
    assert all(len(c) == 6 for _, c in got)


def test_curated_inherits_or_minimal(tmp_path):
    d = _write_today(tmp_path)
    # v3 精选：一只在今日选股(688110·继承依据)、一只不在(999999·最小 Pick)
    (d / f"统筹精选_v3信息先行_{DATE}.md").write_text(
        "# v3\n## 逐票\n### 1. 东芯股份 688110（首选）\n### 2. 幽灵股 999999（不在双路）\n",
        encoding="utf-8")
    from tools.review.loaders import _build_today_index
    idx = _build_today_index(DATE, str(tmp_path))
    cur = load_curated_picks(DATE, str(tmp_path), idx)
    by = {p.code: p for p in cur}
    assert by["688110"].来源 == "策略直选"          # 继承今日选股依据
    assert by["688110"].version_tag == "统筹精选v3"
    assert by["999999"].来源 is None                # 不在双路→最小 Pick，依据留空（有声缺失）
    assert by["999999"].name == "幽灵股"


def test_missing_file_graceful(tmp_path):
    # 无今日选股文件 → 空 picks，不炸
    assert load_today_picks(DATE, data_root=str(tmp_path)) == []
    assert load_picks(DATE, data_root=str(tmp_path)) == []


def test_malformed_json_graceful(tmp_path):
    d = tmp_path / "analysis" / DATE
    d.mkdir(parents=True)
    (d / f"今日选股_{DATE}.json").write_text("{bad json", encoding="utf-8")
    assert load_today_picks(DATE, data_root=str(tmp_path)) == []  # 有声降级、不抛
