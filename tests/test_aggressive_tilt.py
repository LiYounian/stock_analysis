"""第二步·激进版·后验倾斜 单测(守则6:锁死安全阀语义)。

锁死:
- 安全阀①kill-switch:k=0 或 signal=None → 方向_修正逐字段==方向、上涨概率%_修正==上涨概率%、是否倾斜=False。
- 只倾斜方向、只对 1/5日(10日透传);退回样本不倾斜;clip 到[0,100]。
- 根源结构性信号口径:只用 政策+公司公告根源,舆情/顶层不进;强度门槛过滤弱事件;无根源→None;陈旧×0.5;无数据不计。
"""
from tools.analysis import conditional_predict as cp


# ———————— direction_view 倾斜 ————————
def _cond(p1=55.0, p5=55.0, p10=55.0, lvl="精确", fb=False):
    mk = lambda p: {"上涨概率%": p, "放宽层级": lvl, "是否退回": fb}
    return {"1日": mk(p1), "5日": mk(p5), "10日": mk(p10)}


def test_killswitch_k0_or_no_signal_equiv_pure_tech():
    """k=0 或 signal=None:逐字段等价纯技术(方向_修正==方向、概率不变、未倾斜)。"""
    for kwargs in ({"signal": 1.0, "k": 0.0}, {"signal": None, "k": 12.0}):
        dv = cp.direction_view(_cond(60.0, 40.0, 50.0), **kwargs)
        for key, v in dv.items():
            assert v["方向_修正"] == v["方向"]
            assert v["上涨概率%_修正"] == v["上涨概率%"]
            assert v["是否倾斜"] is False


def test_tilt_only_1_and_5_day_not_10():
    """k>0、signal>0:1/5日按 p+k·signal 重判,10日透传不倾斜;基线方向不动。"""
    dv = cp.direction_view(_cond(55.0, 55.0, 55.0), signal=1.0, k=8.0, tilt_horizons=("1日", "5日"))
    assert dv["1日"]["上涨概率%_修正"] == 63.0 and dv["1日"]["方向_修正"] == "看涨"   # 55→63 过58
    assert dv["1日"]["方向"] == "中性"                                             # 基线不动
    assert dv["1日"]["是否倾斜"] is True
    assert dv["5日"]["方向_修正"] == "看涨" and dv["5日"]["是否倾斜"] is True
    assert dv["10日"]["方向_修正"] == "中性" and dv["10日"]["是否倾斜"] is False    # 10日透传


def test_tilt_negative_signal_pushes_bearish():
    """signal<0(利空根源):下压概率、可翻成看跌。"""
    dv = cp.direction_view(_cond(45.0, 45.0), signal=-1.0, k=8.0)
    assert dv["1日"]["上涨概率%_修正"] == 37.0 and dv["1日"]["方向_修正"] == "看跌"   # 45→37 过42


def test_tilt_clip_and_no_tilt_on_fallback():
    """clip 到[0,100];退回样本不倾斜。"""
    cond = {"1日": {"上涨概率%": 96.0, "放宽层级": "精确", "是否退回": False},
            "5日": {"上涨概率%": 55.0, "放宽层级": "退回", "是否退回": True}}
    dv = cp.direction_view(cond, signal=1.0, k=8.0)
    assert dv["1日"]["上涨概率%_修正"] == 100.0            # 96+8→clip100
    assert dv["5日"]["是否倾斜"] is False                  # 退回不倾斜
    assert dv["5日"]["方向_修正"] == dv["5日"]["方向"]


def test_direction_view_backward_compat_keys():
    """基线键(方向/置信度/上涨概率%/放宽层级)仍在(不破坏旧消费)。"""
    dv = cp.direction_view(_cond(60.0))
    v = dv["1日"]
    for kk in ("方向", "置信度", "上涨概率%", "放宽层级"):
        assert kk in v


# ———————— root_structural_signal 口径 ————————
def _sent(policy=None, events=None, news_fresh="新鲜"):
    three = {"新闻": {"新鲜度": news_fresh}}
    if policy is not None:
        three["政策"] = policy
    return {"三层": three, "events": events or []}


def test_signal_strong_positive_300209_like():
    """政策+0.47/40 + 公司公告利好·4·直接 → 强正(≈+0.67)。"""
    s = _sent(policy={"净情绪": 0.47, "样本数": 40, "新鲜度": "新鲜"},
              events=[{"层": "公司行为", "影响方向": "利好", "影响强度": 4, "与本股关系": "直接"}])
    r = cp.root_structural_signal(s)
    assert r is not None and 0.5 < r <= 1.0


def test_signal_weak_negative_indirect_300502_like():
    """无政策 + 公司公告利空·3·间接 → 负(≈-0.6)。"""
    s = _sent(policy={"净情绪": 0.0, "样本数": 0},
              events=[{"层": "公司行为", "影响方向": "利空", "影响强度": 3, "与本股关系": "间接"}])
    r = cp.root_structural_signal(s)
    assert r is not None and -1.0 <= r < 0


def test_signal_no_valid_root_601838_like_returns_none():
    """无政策 + 仅中性/弱(强度<门槛)公司事件 → None(不倾斜)。"""
    s = _sent(policy={"净情绪": 0.0, "样本数": 0},
              events=[{"层": "公司行为", "影响方向": "中性", "影响强度": 1, "与本股关系": "直接"}])
    assert cp.root_structural_signal(s) is None


def test_signal_none_and_missing():
    assert cp.root_structural_signal(None) is None
    assert cp.root_structural_signal({}) is None


def test_signal_ignores_ugc_and_top_net():
    """舆情层/顶层净情绪分不参与:只有舆情有值时 → None。"""
    s = {"净情绪分": 0.9, "三层": {"舆情": {"净情绪": 0.9, "样本数": 20}, "新闻": {"新鲜度": "新鲜"}}, "events": []}
    assert cp.root_structural_signal(s) is None


def test_signal_strength_threshold_filters_weak_company_event():
    """公司公告强度<门槛(默认3)不计;只有弱事件+无政策 → None。"""
    s = _sent(events=[{"层": "公司行为", "影响方向": "利好", "影响强度": 2, "与本股关系": "直接"}])
    assert cp.root_structural_signal(s) is None


def test_signal_stale_halves_policy():
    """政策层新鲜度=陈旧 → 该分量×0.5。"""
    s = _sent(policy={"净情绪": 0.6, "样本数": 5, "新鲜度": "陈旧"})
    assert abs(cp.root_structural_signal(s) - 0.3) < 1e-9   # 只有政策,root==pol_sig×0.5


def test_signal_policy_no_data_excluded():
    """政策层新鲜度=无数据 → 不计;无其它根源 → None。"""
    s = _sent(policy={"净情绪": 0.6, "样本数": 5, "新鲜度": "无数据"})
    assert cp.root_structural_signal(s) is None


def test_signal_strength_may_be_string():
    """影响强度为字符串也能 coerce(真实数据里可能是 str)。"""
    s = _sent(events=[{"层": "公司行为", "影响方向": "利好", "影响强度": "4", "与本股关系": "直接"}])
    r = cp.root_structural_signal(s)
    assert r is not None and r > 0


# ———————— 真结构性门(持续性接入 record 后)————————
def _comp(direction="利好", strength=4, rel="直接", persist=None, pdir=None):
    e = {"层": "公司行为", "影响方向": direction, "影响强度": strength, "与本股关系": rel}
    if persist is not None:
        e["持续性"] = persist
    if pdir is not None:
        e["持续性方向"] = pdir
    return _sent(events=[e])


def test_structural_event_counts():
    """持续性=结构性持续 → 计入(方向取持续性方向)。"""
    r = cp.root_structural_signal(_comp(persist="结构性持续", pdir="利好", strength=4))
    assert r is not None and r > 0


def test_transient_or_neutral_persist_excluded():
    """持续性=短暂事件/中性 → 见光死,不进倾斜(即便强度5);无其它根源 → None。"""
    assert cp.root_structural_signal(_comp(persist="短暂事件", pdir="利好", strength=5)) is None
    assert cp.root_structural_signal(_comp(persist="中性", strength=5)) is None


def test_structural_dir_falls_back_to_impact_dir():
    """持续性方向=中性 时退回原 影响方向。"""
    r = cp.root_structural_signal(_comp(direction="利空", persist="结构性持续", pdir="中性", strength=3))
    assert r is not None and r < 0


def test_unclassified_event_uses_strength_proxy():
    """无持续性字段(未分类)→ 退回旧强度近似(≥门槛计入,<门槛不计)。"""
    assert cp.root_structural_signal(_comp(persist=None, strength=4)) is not None
    assert cp.root_structural_signal(_comp(persist=None, strength=2)) is None


def test_switch_off_ignores_persistence(monkeypatch):
    """SENTIMENT_PERSISTENCE_ON=False → 忽略持续性,退回强度近似(短暂事件也按强度计)。"""
    monkeypatch.setattr(cp.settings, "SENTIMENT_PERSISTENCE_ON", False)
    assert cp.root_structural_signal(_comp(persist="短暂事件", direction="利好", strength=4)) is not None
