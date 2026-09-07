"""候选池消息面富集·独立确认层单测。

锁语义(为什么这么写,防未来重写误删规则):
  · 候选池 = 合议 Top-N ∪ 各策略视图各前 K ∪ 自选池,去重保序(合议在前作骨架)。
  · 缺某策略/合议 view → 该来源贡献 0,不崩(优雅降级)。
  · 独立确认层只召 情绪三层/事件驱动/资金流;候选有 sentiment/fundflow 数据 → 消息面专家**发声**
    (不再弃权);候选无消息面数据 → 三位**全弃权**。这正是本节点存在的意义。
  · 节点**不改写 record['council'] 全A主排序**——只产附加 view「候选池消息面确认」。
  · 富集**只对候选池**(有界),节点把 pool 原样传给富集/组装步,不扩到全A。
"""
import pytest

from tools.pipeline import candidate_message as cm


# ————————————————————————————————————————————————
# ① 候选池抽取
# ————————————————————————————————————————————————
def _fake_views():
    """构造一批假 view:合议 top 8 只、两个策略各若干、与合议部分重叠。"""
    council = {"top": [{"code": f"C{i:03d}"} for i in range(8)]}      # C000..C007
    v_mom = {"入选清单": [{"code": "C005"}, {"code": "M001"}, {"code": "M002"}, {"code": "M003"}]}
    v_vol = {"top": [{"code": "V001"}, {"code": "V002"}]}
    v_rank = {"排行": {"1日": [{"code": "R001"}, {"code": "R002"}]}}  # 排行型
    return {"策略0合议": council, "动量组合": v_mom, "量价放量": v_vol,
            "指标条件化状态排序": v_rank}


@pytest.fixture
def _patch_views(monkeypatch):
    views = _fake_views()

    def _get_view(name, date="latest"):
        if name in views:
            return views[name]
        raise FileNotFoundError(name)   # 其余策略 view 缺失 → 降级贡献 0

    monkeypatch.setattr(cm.store, "get_view", _get_view)
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: ["W001", "C000"])  # 自选含与合议重叠票
    return views


def test_build_pool_union_topn_topk_dedup(_patch_views):
    built = cm.build_candidate_pool("2026-09-08", council_top_n=5, per_view_top_k=2)
    pool = built["pool"]
    # 合议前 5:C000..C004;动量前 2:C005,M001(C005 不在合议前5,故新增);量价前 2:V001,V002;
    # 排行前 2:R001,R002;自选:W001,C000(去重)。
    assert pool[:5] == ["C000", "C001", "C002", "C003", "C004"]     # 合议在前作骨架
    for c in ["C005", "M001", "V001", "V002", "R001", "R002", "W001"]:
        assert c in pool
    assert pool.count("C000") == 1                                  # 去重(合议∩自选)
    assert len(pool) == len(set(pool))                              # 全去重
    # provenance 记多来源
    assert "策略0合议" in built["provenance"]["C000"]
    assert "自选池" in built["provenance"]["C000"]
    assert built["counts"]["策略0合议"] == 5
    assert built["counts"]["动量组合"] == 2


def test_build_pool_missing_views_degrade(monkeypatch):
    """全部 view 缺失 → 池仅剩自选(不崩)。"""
    monkeypatch.setattr(cm.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError(name)))
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: ["W001", "W002"])
    built = cm.build_candidate_pool("2026-09-08")
    assert built["pool"] == ["W001", "W002"]
    assert built["counts"]["策略0合议"] == 0


# ————————————————————————————————————————————————
# ③ 独立确认层:有消息面数据 → 专家发声;无 → 全弃权
# ————————————————————————————————————————————————
def _rec_with_news(code, net):
    return {"meta": {"code": code, "name": f"名{code}", "as_of": "2026-09-08"},
            "sentiment": {"净情绪分": net, "样本数": 20},
            "fundflow": {"今日主力净流入": 1e8 if net > 0 else -1e8,
                         "主力连续净流入天数": 3 if net > 0 else 0}}


def _rec_no_news(code):
    return {"meta": {"code": code, "name": f"名{code}", "as_of": "2026-09-08"}}


def test_confirm_experts_speak_when_news_present(hermetic_experts):
    """候选有 sentiment/fundflow → 情绪三层 + 资金流 发声(不弃权),消息面方向随情绪。"""
    recs = {"C001": _rec_with_news("C001", 0.6), "C002": _rec_with_news("C002", -0.6)}
    out = cm.confirm_pool(["C001", "C002"], provenance={"C001": ["策略0合议"]},
                          load_record=lambda c: recs[c])
    by = {x["code"]: x for x in out}
    assert by["C001"]["消息面方向"] == "看多"
    assert by["C002"]["消息面方向"] == "看空"
    assert not by["C001"]["全弃权"]
    assert "情绪三层" in by["C001"]["发声专家"]
    assert "资金流" in by["C001"]["发声专家"]
    assert "事件驱动" in by["C001"]["弃权专家"]      # hermetic 下事件汇总恒空 → 弃权
    assert by["C001"]["依据"]                        # 有理由
    assert by["C001"]["候选来源"] == ["策略0合议"]


def test_confirm_all_abstain_without_news(hermetic_experts):
    """候选无任何消息面数据 → 三位消息面专家全弃权(本节点存在意义的反面锁)。"""
    out = cm.confirm_pool(["C009"], provenance={},
                          load_record=lambda c: _rec_no_news(c))
    assert out[0]["全弃权"] is True
    assert out[0]["发声专家"] == []
    assert set(out[0]["弃权专家"]) == set(cm.MSG_EXPERTS)


def test_confirm_skips_missing_record(hermetic_experts):
    def _load(code):
        if code == "MISS":
            raise FileNotFoundError(code)
        return _rec_with_news(code, 0.6)
    out = cm.confirm_pool(["C001", "MISS"], provenance={}, load_record=_load)
    assert [x["code"] for x in out] == ["C001"]     # 缺 record 的票跳过,不崩


# ————————————————————————————————————————————————
# 节点编排:有界(只对候选池)+ 落 view + 不改全A主排序
# ————————————————————————————————————————————————
def test_node_bounded_writes_view_and_no_council_mutation(monkeypatch, hermetic_experts,
                                                          analysis_tmpdir):
    # 池:合议 top 3(analysis_tmpdir 下先写入假 view)
    cm.store.set_active_date("2026-09-08")
    cm.store.put_view("策略0合议", {"top": [{"code": "C001"}, {"code": "C002"}, {"code": "C003"}]},
                      date="2026-09-08")
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: [])

    calls = {}

    def _enrich(pool, as_of, no_llm=False):
        calls["enrich_pool"] = list(pool)
        calls["enrich_no_llm"] = no_llm
        return {"stub": True}

    def _serialize(pool, as_of):
        calls["serialize_pool"] = list(pool)

    recs = {"C001": _rec_with_news("C001", 0.6), "C002": _rec_with_news("C002", -0.6),
            "C003": _rec_no_news("C003")}
    view = cm.run_candidate_message_enrich("2026-09-08", council_top_n=30, per_view_top_k=5,
                                           enrich_fn=_enrich, serialize_fn=_serialize,
                                           load_record=lambda c: recs[c])
    # 有界:富集/组装只拿到候选池这 3 只(不扩全A)
    assert calls["enrich_pool"] == ["C001", "C002", "C003"]
    assert calls["serialize_pool"] == ["C001", "C002", "C003"]
    # 落了 view，含 3 条确认
    assert view["候选池规模"] == 3
    assert {x["code"] for x in view["确认"]} == {"C001", "C002", "C003"}
    assert view["确认层专家"] == cm.MSG_EXPERTS
    # 统计:C001看多 / C002看空 / C003全弃权(无数据)
    assert view["统计"]["看多"] >= 1 and view["统计"]["看空"] >= 1 and view["统计"]["全弃权"] == 1
    # 不改全A主排序:注入的 load_record 返回的 record 未被写回 council(节点从不落 record['council'])
    assert all("council" not in recs[c] for c in recs)
    # view 可回读(真落盘到 tmp)
    back = cm.store.get_view("候选池消息面确认", date="2026-09-08")
    assert back["候选池规模"] == 3


def test_node_no_llm_flag_propagates(monkeypatch, hermetic_experts, analysis_tmpdir):
    cm.store.set_active_date("2026-09-08")
    cm.store.put_view("策略0合议", {"top": [{"code": "C001"}]}, date="2026-09-08")
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: [])
    seen = {}

    def _enrich(pool, as_of, no_llm=False):
        seen["no_llm"] = no_llm
        return {}

    cm.run_candidate_message_enrich("2026-09-08", enrich_fn=_enrich,
                                    serialize_fn=lambda p, a: None,
                                    load_record=lambda c: _rec_with_news(c, 0.6), no_llm=True)
    assert seen["no_llm"] is True
