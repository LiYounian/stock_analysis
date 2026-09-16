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


def test_write_load_roundtrip(tmp_path, _patch_sources):
    _patch_sources(table={"电子": {"roles": {"龙头": [{"code": "A", "name": "甲"}]}}},
                   news={"A": [{"title": "t", "content": "c", "time": "2026-09-15"}]})
    raw = BC.collect_board_raw("2026-09-16", "电子", [{"code": "A", "name": "甲", "role": "龙头"}])
    p = BC.write_board_raw("2026-09-16", "电子", raw, out_root=str(tmp_path))
    back = BC.load_board_raw("2026-09-16", "电子", out_root=str(tmp_path))
    assert back is not None and back["板块"] == "电子"
    assert back["items"][0]["title"] == "t"
