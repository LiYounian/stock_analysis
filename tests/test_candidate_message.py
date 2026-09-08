"""候选池消息面三段式·回灌打分单测。

锁语义(为什么这么写,防未来重写误删规则):
  ① 阶段1 候选口径:各策略(含合议策略0)各取 top-K(默认8,clamp 到 [5,10])∪ 自选池,去重保序;
     **硬上限 ≤ 策略数×10**(控 LLM 预算,超出按序截断);缺某 view → 该来源贡献 0(优雅降级)。
  ② 阶段3 回灌重排**确实用了消息面分**:候选有 sentiment/fundflow → 消息面专家发声 + 消息面分非0
     → 完整分抬升、排到无消息面数据的候选之前(有数据的专家发声 → 候选集内排序变化)。
  ③ **不改全A主排序**:节点只重排候选集、只落候选 view,**从不写 record['council']**——全A 5000 主排序
     不受影响(本设计红线,测试锁死)。
  ④ 映射函数方向正确:看多/看涨 → 加分、看空 → 减分、中性 → 0;kill-switch(启用=False)→ 恒0。
  ⑤ 留存视图「消息面评分」产出且结构正确(方向/强度/分/理由/完整分),供复用/展示。
"""
import copy

import pytest

from tools.config.strategy import THRESHOLDS
from tools.pipeline import candidate_message as cm


# ————————————————————————————————————————————————
# ① 候选池抽取(各策略 top-K ∪,硬上限 ≤ 策略数×10,去重降级)
# ————————————————————————————————————————————————
def _fake_views():
    """构造一批假 view:合议 top 8 只、几个策略各若干、与合议部分重叠。"""
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


def test_build_pool_topk_union_dedup(_patch_views):
    built = cm.build_candidate_pool("2026-09-08", top_k=5)   # 5 在合法区间 [5,10]
    pool = built["pool"]
    # 合议前5:C000..C004(合议是策略0、放最前作骨架);动量前5:C005,M001,M002,M003(仅4只);
    # 量价:V001,V002;排行:R001,R002;自选:W001,C000(去重)。
    assert pool[:5] == ["C000", "C001", "C002", "C003", "C004"]
    for c in ["C005", "M001", "M002", "M003", "V001", "V002", "R001", "R002", "W001"]:
        assert c in pool
    assert pool.count("C000") == 1                                  # 去重(合议∩自选)
    assert len(pool) == len(set(pool))                              # 全去重
    # provenance 记多来源
    assert "策略0合议" in built["provenance"]["C000"]
    assert "自选池" in built["provenance"]["C000"]
    assert built["counts"]["策略0合议"] == 5
    assert built["counts"]["动量组合"] == 4
    assert built["top_k"] == 5


def test_build_pool_topk_clamped_to_range():
    """top-K 越界自动 clamp 到 config「top_k范围」(默认 [5,10])。"""
    assert cm._resolve_top_k(100) == 10       # 上越界 → 10
    assert cm._resolve_top_k(1) == 5          # 下越界 → 5
    assert cm._resolve_top_k(7) == 7          # 区间内原样
    assert cm._resolve_top_k(None) == int(THRESHOLDS["消息面回灌"]["top_k"])  # None → config 默认


def test_build_pool_hard_cap_truncates(monkeypatch):
    """硬上限 ≤ 策略数×10:超限按并入序截断(合议/靠前策略优先保留),被砍票 provenance 清掉。"""
    big = {"top": [{"code": f"C{i:03d}"} for i in range(10)]}       # 合议 10 只

    def _get_view(name, date="latest"):
        if name == "策略0合议":
            return big
        raise FileNotFoundError(name)

    monkeypatch.setattr(cm.store, "get_view", _get_view)
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: [])
    # 只留合议一路策略、硬上限=3 → 候选截到前 3;超出票不进池、provenance 不留脏来源。
    built = cm.build_candidate_pool("2026-09-08", top_k=10,
                                    strategy_views=[], hard_cap=3)
    assert built["pool"] == ["C000", "C001", "C002"]
    assert built["上限命中"] is True
    assert "C003" not in built["provenance"]
    # 默认硬上限 = 策略数×10(策略数 = 1合议 + len(STRATEGY_VIEWS))
    d = cm.build_candidate_pool("2026-09-08", strategy_views=None)
    assert d["硬上限"] == (1 + len(cm.STRATEGY_VIEWS)) * 10


def test_build_pool_missing_views_degrade(monkeypatch):
    """全部 view 缺失 → 池仅剩自选(不崩)。"""
    monkeypatch.setattr(cm.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError(name)))
    monkeypatch.setattr(cm.stock_pool, "get_codes", lambda: ["W001", "W002"])
    built = cm.build_candidate_pool("2026-09-08")
    assert built["pool"] == ["W001", "W002"]
    assert built["counts"]["策略0合议"] == 0


# ————————————————————————————————————————————————
# ④ 评价 → 分数:可解释线性映射方向正确 + kill-switch
# ————————————————————————————————————————————————
def test_msg_score_mapping_direction():
    cfg = {"启用": True, "方向强度斜率": 1.0, "分数上限": 1.0}
    assert cm.msg_score_from_evaluation("看多", 0.6, cfg=cfg) == pytest.approx(0.6)   # 看多 → 加分
    assert cm.msg_score_from_evaluation("看涨", 0.6, cfg=cfg) == pytest.approx(0.6)   # 看涨同义 → 加分
    assert cm.msg_score_from_evaluation("看空", 0.6, cfg=cfg) == pytest.approx(-0.6)  # 看空 → 减分
    assert cm.msg_score_from_evaluation("中性", 0.9, cfg=cfg) == 0.0                  # 中性 → 0
    # 强度 clamp [0,1] + 分数上限 clamp
    assert cm.msg_score_from_evaluation("看多", 5.0, cfg=cfg) == pytest.approx(1.0)
    cap = {"启用": True, "方向强度斜率": 3.0, "分数上限": 0.5}
    assert cm.msg_score_from_evaluation("看多", 1.0, cfg=cap) == pytest.approx(0.5)   # 斜率放大后被上限压住
    # kill-switch:启用=False → 恒0(回灌 no-op)
    assert cm.msg_score_from_evaluation("看多", 0.9, cfg={"启用": False}) == 0.0


# ————————————————————————————————————————————————
# ② 回灌重排确实用了消息面分 + ③ 不写 record['council']
# ————————————————————————————————————————————————
def _rec_with_news(code, net):
    return {"meta": {"code": code, "name": f"名{code}", "as_of": "2026-09-08"},
            "sentiment": {"净情绪分": net, "样本数": 20},
            "fundflow": {"今日主力净流入": 1e8 if net > 0 else -1e8,
                         "主力连续净流入天数": 3 if net > 0 else 0}}


def _rec_no_news(code):
    return {"meta": {"code": code, "name": f"名{code}", "as_of": "2026-09-08"}}


def test_rescore_uses_message_and_reranks(hermetic_experts):
    """有消息面数据 → 消息面专家发声、消息面分非0、完整分抬升 → 排到无数据候选之前(候选集重排)。"""
    recs = {"P": _rec_with_news("P", 0.6),      # 正面消息 → 看多
            "N": _rec_with_news("N", -0.6),     # 负面消息 → 看空
            "Z": _rec_no_news("Z")}             # 无消息面数据 → 专家全弃权、分0
    out = cm.rescore_pool(["P", "N", "Z"], provenance={"P": ["策略0合议"]},
                          load_record=lambda c: recs[c])
    by = {x["code"]: x for x in out}
    # 消息面方向随情绪、消息面分方向正确
    assert by["P"]["消息面方向"] == "看多" and by["P"]["消息面分"] > 0
    assert by["N"]["消息面方向"] == "看空" and by["N"]["消息面分"] < 0
    assert by["Z"]["消息面分"] == 0.0 and by["Z"]["全弃权"] is True
    # 完整分 = 数据面综合分 + 回灌权重×消息面分:看多候选完整分 > 数据面综合分(回灌 term 真加了分)
    w = float(THRESHOLDS["消息面回灌"]["回灌权重"])
    assert by["P"]["完整分"] == pytest.approx(by["P"]["数据面综合分"] + w * by["P"]["消息面分"])
    assert by["P"]["完整分"] > by["P"]["数据面综合分"]
    # 候选集内重排:看多(P)排在无数据(Z)之前、看空(N)之后 → 排序确实被消息面分驱动
    ranks = {x["code"]: x["候选排名"] for x in out}
    assert ranks["P"] < ranks["Z"]
    assert ranks["P"] < ranks["N"]
    # 发声名单:情绪三层发声;资金流已移出消息面消费者专家 → 不进消息面合议、不在发声名单
    assert "情绪三层" in by["P"]["发声专家"]
    assert "资金流" not in by["P"]["发声专家"]
    assert by["P"]["理由"] and by["P"]["候选来源"] == ["策略0合议"]
    # ③ 不写 record['council']:注入 record 从未被塞进 council(全A主排序不受影响)
    assert all("council" not in recs[c] for c in recs)


def test_rescore_killswitch_off_reflow_noop(hermetic_experts):
    """kill-switch 关(启用=False):消息面分恒0、完整分退回纯数据基线(回灌不改候选排序)。"""
    recs = {"P": _rec_with_news("P", 0.6)}
    cfg_off = {**THRESHOLDS["消息面回灌"], "启用": False}
    out = cm.rescore_pool(["P"], provenance={}, load_record=lambda c: recs[c], cfg=cfg_off)
    x = out[0]
    assert x["消息面分"] == 0.0
    assert x["完整分"] == pytest.approx(x["数据面综合分"])   # 回灌 no-op → 完整分=纯数据基线


def test_rescore_skips_missing_record(hermetic_experts):
    def _load(code):
        if code == "MISS":
            raise FileNotFoundError(code)
        return _rec_with_news(code, 0.6)
    out = cm.rescore_pool(["P", "MISS"], provenance={}, load_record=_load)
    assert [x["code"] for x in out] == ["P"]         # 缺 record 的票跳过,不崩


# ————————————————————————————————————————————————
# 节点编排:有界(只对候选池)+ 落两份 view + 不改全A主排序 + ⑤ 留存结构
# ————————————————————————————————————————————————
def test_node_bounded_writes_views_and_no_council_mutation(monkeypatch, hermetic_experts,
                                                           analysis_tmpdir):
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
    orig = copy.deepcopy(recs)
    view = cm.run_candidate_message_enrich("2026-09-08", top_k=8,
                                           enrich_fn=_enrich, serialize_fn=_serialize,
                                           load_record=lambda c: recs[c])
    # 有界:富集/组装只拿到候选池这 3 只(不扩全A)
    assert calls["enrich_pool"] == ["C001", "C002", "C003"]
    assert calls["serialize_pool"] == ["C001", "C002", "C003"]
    # 落了「候选池消息面确认」view,含 3 条重排
    assert view["候选池规模"] == 3
    assert {x["code"] for x in view["重排"]} == {"C001", "C002", "C003"}
    assert view["消息面消费者专家"] == THRESHOLDS["消息面回灌"]["消费者专家"]
    # 统计:C001看多 / C002看空 / C003全弃权
    assert view["统计"]["看多"] >= 1 and view["统计"]["看空"] >= 1 and view["统计"]["全弃权"] == 1
    # ③ 不写 record['council']:节点从不落 record['council'](注入 record 保持原样)
    assert all("council" not in recs[c] for c in recs)
    assert recs == orig
    # view 可回读
    back = cm.store.get_view("候选池消息面确认", date="2026-09-08")
    assert back["候选池规模"] == 3
    # ⑤ 留存视图「消息面评分」产出 + 结构正确
    scoring = cm.store.get_view("消息面评分", date="2026-09-08")
    assert scoring["视图"] == "消息面评分"
    assert len(scoring["评分"]) == 3
    row = scoring["评分"][0]
    for key in ("code", "消息面方向", "消息面强度", "消息面分", "数据面综合分", "完整分",
                "理由", "候选排名"):
        assert key in row
    # 评分按完整分降序(候选排名 1 分最高)
    assert scoring["评分"][0]["完整分"] >= scoring["评分"][-1]["完整分"]


# ————————————————————————————————————————————————
# 消息面回灌校准锁(2026-09-08 续6:w 0.5→0.3 + 资金流移出消息面专家)
# 前向评测证实旧配置 double-count:默认专家组已含 情绪三层/事件驱动/资金流,
# 消息面分又单独合议这三位并以 w=0.5 回灌 → 同批专家算两次、放大噪声。
# 校准=①权重降到 0.3(强协同项非主导)②资金流(数据面因子)移出消息面消费者专家、
# 但仍留默认专家组走 base 通道(不误删数据面资金流因子)。
# ————————————————————————————————————————————————
def test_reflow_weight_is_calibrated_to_0_3():
    """① 回灌权重 == 0.3(前向评测校准:旧 0.5 双重计数偏激进)。"""
    assert THRESHOLDS["消息面回灌"]["回灌权重"] == 0.3


def test_msg_experts_exclude_fundflow():
    """② 消息面消费者专家 == [情绪三层, 事件驱动](资金流已移出,不再走回灌重复通道);
    config 与模块常量 MSG_EXPERTS 保持一致(单一真源)。"""
    assert THRESHOLDS["消息面回灌"]["消费者专家"] == ["情绪三层", "事件驱动"]
    assert cm.MSG_EXPERTS == ["情绪三层", "事件驱动"]
    assert "资金流" not in THRESHOLDS["消息面回灌"]["消费者专家"]
    assert "资金流" not in cm.MSG_EXPERTS


def test_default_experts_still_contain_fundflow():
    """③ 资金流仅移出消息面通道,**仍在数据面默认专家组**(它是数据面因子,该在 base)。
    锁死此点防未来把资金流从 base 一并误删。"""
    # 默认专家组是 rescore_pool 的 consumer_experts 真源(数据面综合分用它重算合议),
    # 故资金流仍作为数据面因子参与 base 打分,只是不再走消息面回灌重复通道。
    assert "资金流" in THRESHOLDS["合议"]["默认专家组"]


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
