"""S3 定向采集层 测试——锁"采集与研判解耦·按表采集·逐条可溯"的语义:

1. role_picks 读 S2 关系表(全板块含主力),跨角色去重(一票多角色留首个)。
2. collect_board_raw 只抓不判:新闻按日期窗口过滤(≤date 且 ≥cutoff)、逐条带 role/code/source/url。
3. 板块政策 + 国际对标(region=国外→国际形势)按 industry_map 命中并入。
4. LHB 资金流快照按 code 附带(PIT·窗口内上榜)。
5. 无 LLM:采集全程不触发任何 LLM 客户端。
6. 读写往返:write_board_raw → load_board_raw 一致。
"""
import json

import pytest

from tools.analysis.sector_forecast import board_news_collect as BC


@pytest.fixture
def _patch_sources(monkeypatch):
    """装配 S2 表 / 新闻 / 政策 / LHB 的 mock,断网。"""
    def _apply(*, table=None, news=None, policy=None, lhb_data=None):
        monkeypatch.setattr(BC, "_read_role_table", lambda date, sw: (table or {}).get(sw))

        import tools.collectors.news as news_mod
        monkeypatch.setattr(news_mod, "fetch_news",
                            lambda codes, days=None, workers=1: {c: (news or {}).get(c, []) for c in codes})

        import tools.analysis.sector_forecast.market_step as ms
        if policy is not None:
            import tempfile
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
            json.dump(policy, f, ensure_ascii=False)
            f.close()
            from pathlib import Path
            monkeypatch.setattr(ms, "resolve_analysis_file",
                                lambda date, name: Path(f.name) if name == "sentiment_policy.json" else None)
        else:
            monkeypatch.setattr(ms, "resolve_analysis_file", lambda date, name: None)

        import tools.analysis.industry_map as im
        monkeypatch.setattr(im, "to_sw", lambda x: x)   # 行业名直通(测试里 industries 已是申万名)

        import tools.collectors.lhb as lhb_mod

        def _asof(code, date):
            if lhb_data and code in lhb_data:
                return lhb_data[code]
            raise FileNotFoundError(code)
        monkeypatch.setattr(lhb_mod, "lhb_asof", _asof)
    return _apply


def test_role_picks_dedup_across_roles(_patch_sources):
    _patch_sources(table={"电子": {"roles": {
        "龙头": [{"code": "A", "name": "甲"}],
        "中军": [{"code": "B", "name": "乙"}],
        "主力": [{"code": "A", "name": "甲"}, {"code": "C", "name": "丙"}],  # A 与龙头重
    }}})
    picks = BC.role_picks("2026-09-16", ["电子"])
    codes = [p["code"] for p in picks["电子"]]
    assert codes == ["A", "B", "C"]                      # A 去重,留首个角色
    assert picks["电子"][0]["role"] == "龙头"            # A 首个角色=龙头(按 roles 顺序)


def test_collect_filters_news_window(_patch_sources):
    _patch_sources(
        table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
        news={"A": [
            {"title": "利好新闻", "content": "x", "time": "2026-09-15", "source": "东财", "url": "u1"},
            {"title": "太旧", "content": "y", "time": "2026-08-01", "source": "新浪", "url": "u2"},  # 超窗
            {"title": "未来", "content": "z", "time": "2026-09-20", "source": "东财", "url": "u3"},  # date之后
        ]},
    )
    picks = BC.role_picks("2026-09-16", ["电子"])
    raw = BC.collect_board_raw("2026-09-16", "电子", picks["电子"])
    titles = [it["title"] for it in raw["items"]]
    assert "利好新闻" in titles and "太旧" not in titles and "未来" not in titles
    it = next(i for i in raw["items"] if i["title"] == "利好新闻")
    assert it["role"] == "龙头" and it["code"] == "A" and it["source"] == "东财" and it["url"] == "u1"


def test_allow_future_toggle(_patch_sources):
    """allow_future 开关(防未来硬红线):默认严格剔除 t>date;=True(live)保留未来新闻,下限仍生效。"""
    _patch_sources(
        table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
        news={"A": [
            {"title": "当日", "content": "x", "time": "2026-09-15", "source": "东财", "url": "u1"},
            {"title": "未来", "content": "z", "time": "2026-09-20", "source": "东财", "url": "u3"},
            {"title": "太旧", "content": "y", "time": "2026-06-01", "source": "新浪", "url": "u2"},
        ]},
    )
    picks = [{"code": "A", "name": "甲", "role": "龙头"}]
    # 默认严格:未来新闻被剔
    strict = BC.collect_board_raw("2026-09-16", "电子", picks)
    tt = [i["title"] for i in strict["items"]]
    assert "未来" not in tt and strict["allow_future"] is False
    # allow_future=True(live):未来新闻保留,但下限窗口(太旧)仍剔
    live = BC.collect_board_raw("2026-09-16", "电子", picks, allow_future=True)
    tl = [i["title"] for i in live["items"]]
    assert "未来" in tl and "当日" in tl and "太旧" not in tl
    assert live["allow_future"] is True and "非防未来" in live["新闻口径"]


def test_policy_and_international(_patch_sources):
    _patch_sources(
        table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
        news={"A": []},
        policy=[
            {"title": "国产替代政策", "summary": "s", "industries": ["电子"], "date": "2026-09-14"},
            {"title": "美国出口管制", "summary": "s2", "industries": ["电子"], "region": "国外", "date": "2026-09-13"},
            {"title": "无关行业", "summary": "s3", "industries": ["银行"], "date": "2026-09-14"},
        ],
    )
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    kinds = {it["title"]: it["kind"] for it in raw["items"]}
    assert kinds.get("国产替代政策") == "板块政策"
    assert kinds.get("美国出口管制") == "国际对标"
    assert "无关行业" not in kinds                        # 非本板块政策不并入


def test_lhb_fundflow_attached(_patch_sources):
    _patch_sources(
        table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
        news={"A": []},
        lhb_data={"A": [{"list_date": "2026-09-10", "net_buy": 2e8}]},
    )
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    assert len(raw["资金流"]) == 1
    assert raw["资金流"][0]["code"] == "A" and raw["资金流"][0]["方向"] == "净买入"
    assert raw["统计"]["LHB覆盖票数"] == 1


def test_collect_no_llm(monkeypatch, _patch_sources):
    """采集全程不得触发 LLM 客户端(采集与研判解耦硬约束)。"""
    import tools.llm.client as lc
    monkeypatch.setattr(lc, "get_client", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("S3 采集不应调用 LLM")))
    _patch_sources(table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
                   news={"A": [{"title": "t", "content": "c", "time": "2026-09-15"}]})
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    assert raw["统计"]["个股新闻"] == 1                   # 采到了,且没碰 LLM


def test_p1_noise_filter_precision():
    """P1:真实榜单/数据表噪音标题被剔;真催化标题不误杀(精度硬约束·防踏空)。"""
    # —— 应判噪音(市场级榜单/数据表·来自真实对比样本)——
    noise = [
        "数据丨最新，8股股东户数下降超一成！下周4股解禁比例超50%（附股）",
        "下周A股解禁市值超840亿元 4股解禁比例超50%",
        "8月A股回购图谱：277家公司斥资超130亿元实施回购",
        "A股9月7日16家公司限售股解禁 合计市值277.85亿元",
        "跨境支付(CIPS)概念下跌1.05%，8股主力资金净流出超亿元",
        "计算机行业9月7日资金流向日报",
        "59只A股筹码大换手（9月7日）",
        "杠杆资金连续三日减仓创业板股",
        "139股每笔成交量增长超50%",
        "1377只股短线走稳 站上五日均线",
        "72只股上午收盘涨停(附股)",
    ]
    for t in noise:
        assert BC._is_noise_title(t), f"应判噪音却漏了:{t}"
    # —— 必须保留(真催化/公告·个股事件)——
    signal = [
        "长飞光纤（601869）：中标中国移动通信集团内蒙古有限公司采购项目，中标金额为157.04万元",
        "70.998亿元、涨价1.15倍、18家中标：中国移动普通光缆集采顶格落槌",
        "生益科技H1净利暴增130%、电投能源连续5年增长，红利质量ETF招商连续5个季度净流入",
        "PCB行业进入结构性上行阶段：量价齐升由AI算力需求主导",
        "源杰科技(688498)：业绩持续亮眼 CPO/NPO等领域新品进展顺利",
        "华为重磅技术落地！韬定律更新改写国产芯片堆叠发展格局",
        "有研半导体硅材料股份公司 关于筹划发行股份及支付现金购买资产的公告",
        "致尚科技终止收购恒扬数据改道增资 为五矿证券项目",
    ]
    for t in signal:
        assert not BC._is_noise_title(t), f"误杀了真催化:{t}"


def test_p2_event_dedup_merges_restatements():
    """P2:多家媒体近乎逐字转载同一条 → 去重为一,留正文最长(信息量最高)一条;不同事件保留。"""
    items = [
        {"code": "A", "title": "301486，终止收购！", "text": "短"},
        {"code": "A", "title": "301486，终止收购!", "text": "中等长度正文内容"},          # 近逐字(标点差)
        {"code": "A", "title": "【中国基金报】301486，终止收购", "text": "最长最详细的正文内容说明"},  # 加来源前缀
        {"code": "B", "title": "长飞光纤中标中国移动光缆集采70.998亿", "text": "不同事件"},
    ]
    kept, merged = BC._dedup_events(items)
    titles = [k["title"] for k in kept]
    # 终止收购三条近重复聚成一条 + 长飞一条 = 2 条
    assert len(kept) == 2 and merged == 2
    assert any("长飞" in t for t in titles)                 # 不同事件保留
    # 终止收购簇保留了正文最长的一条
    zishang = [k for k in kept if "长飞" not in k["title"]][0]
    assert zishang["text"].startswith("最长最详细")


def test_p2_keeps_distinct_events():
    """P2 保守:不同事件(相似度不够)不误合。"""
    items = [
        {"code": "A", "title": "甲公司中标5亿元光伏订单", "text": "x"},
        {"code": "A", "title": "甲公司拟收购乙公司51%股权", "text": "y"},   # 不同事件
    ]
    kept, merged = BC._dedup_events(items)
    assert len(kept) == 2 and merged == 0


def test_p1_p2_wired_into_collect(_patch_sources):
    """P1+P2 接进 collect_board_raw:噪音剔 + 重复合,统计如实记 P1过滤/P2去重。"""
    _patch_sources(
        table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
        news={"A": [
            {"title": "甲公司中标5亿元大单", "content": "真催化正文", "time": "2026-09-15"},
            {"title": "59只A股筹码大换手（9月15日）", "content": "噪音表", "time": "2026-09-15"},   # P1 噪音
            {"title": "【快讯】甲公司中标5亿元大单", "content": "同一条近逐字转载更长正文", "time": "2026-09-15"},  # P2 与上近重复
        ]},
    )
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    st = raw["统计"]
    assert st["P1过滤噪音"] == 1                       # 榜单噪音剔 1
    assert st["P2去重合并"] == 1                       # 中标/大单同事件合 1
    assert st["个股新闻"] == 1                         # 最终只剩 1 条干净催化
    titles = [it["title"] for it in raw["items"]]
    assert not any("筹码大换手" in t for t in titles)   # 噪音不在


def test_write_load_roundtrip(tmp_path, _patch_sources):
    _patch_sources(table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
                   news={"A": [{"title": "t", "content": "c", "time": "2026-09-15"}]})
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    p = BC.write_board_raw("2026-09-16", "电子", raw, out_root=str(tmp_path))
    back = BC.load_board_raw("2026-09-16", "电子", out_root=str(tmp_path))
    assert back is not None and back["板块"] == "电子"
    assert back["items"][0]["title"] == "t"
