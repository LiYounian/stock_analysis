"""情绪打分失败三态(B 线)语义锁 —— 守则6 的护栏,最重要的红线。

锁住的唯一不变量:**ok 的真·中性 0.0 ≠ unknown 的 null**。
打分失败(unknown)/无输入(missing)绝不塌缩成与真中性不可区分的 0.0;它们只出 null,
且拉低覆盖率/触发顶层质量降级、令情绪专家干净弃权。未来任何重写不得把二者再合并。

设计权威:docs/计划/2026-09-04_情绪打分失败三态_设计.md §二(三态契约)。
"""
import pytest

from tools.analysis import event as ev
from tools.analysis import council
from tools.analysis import predict as pr
from tools.analysis import serialize
from tools.contracts import record as rc


# ————————————————————————————————————————————————
# 核心红线:_weighted_net —— unknown 的 0.0 不得作为真中性进入加权
# ————————————————————————————————————————————————
def test_weighted_net_unknown_layer_is_null_not_neutral():
    """舆情打分失败(status=unknown)、其 0.0 不可信 → 净情绪 None、质量 unknown、覆盖率 0。"""
    net, cover, quality = ev._weighted_net({"舆情": (0.0, 5, "unknown")})
    assert net is None                 # 绝不是 0.0
    assert quality == "unknown"
    assert cover == 0.0


def test_weighted_net_true_neutral_ok_is_zero_not_null():
    """舆情**真中性**(status=ok, net 0.0)→ 净情绪 0.0(可信)、质量 ok。与 unknown 判然不同。"""
    net, cover, quality = ev._weighted_net({"舆情": (0.0, 5, "ok")})
    assert net == 0.0                  # 真中性就是 0.0,不塌缩成 None
    assert quality == "ok"
    assert cover == 1.0


def test_weighted_net_ok_and_unknown_are_distinguishable():
    """同一 0.0 输入,status 不同 → 结果不同。这是本次重构的唯一红线,冻结之。"""
    ok = ev._weighted_net({"舆情": (0.0, 5, "ok")})
    unknown = ev._weighted_net({"舆情": (0.0, 5, "unknown")})
    assert ok[0] == 0.0 and unknown[0] is None
    assert ok[2] == "ok" and unknown[2] == "unknown"


def test_weighted_net_missing_layer_renormalizes():
    """舆情/政策 missing(无输入)→ 退出加权,新闻独占重归一;质量 ok、覆盖率 1.0。"""
    net, cover, quality = ev._weighted_net(
        {"新闻": (0.6, 3, "ok"), "舆情": (0.0, 0, "missing"), "政策": (0.0, 0, "missing")})
    assert net == 0.6                  # 舆情/政策未把净情绪往 0 拉
    assert quality == "ok"
    assert cover == 1.0


def test_weighted_net_unknown_ugc_does_not_pollute_but_marks_partial():
    """新闻 ok + 舆情 unknown:失败层 0.0 不混入加权(净情绪仍=新闻),但顶层质量降为 partial。"""
    net, cover, quality = ev._weighted_net(
        {"新闻": (0.6, 3, "ok"), "舆情": (0.0, 5, "unknown"), "政策": (0.0, 0, "missing")})
    assert net == 0.6                  # 若把 unknown 的 0.0 混入会被拉低,断言未被污染
    assert quality == "partial"
    assert cover == pytest.approx(0.714, abs=1e-3)   # 0.5/(0.5+0.2)


def test_weighted_net_all_missing_is_missing_null():
    """三层全无输入 → 质量 missing、净情绪 None(不再是旧的 0.0)。"""
    net, cover, quality = ev._weighted_net(
        {"新闻": (0.0, 0, "missing"), "舆情": (0.0, 0, "missing"), "政策": (0.0, 0, "missing")})
    assert net is None and quality == "missing"


def test_weighted_net_coverage_threshold_configurable():
    """覆盖率阈值可配:提高阈值可把「新闻ok+舆情unknown」从 partial 推成 unknown(净情绪 null)。"""
    layers = {"新闻": (0.6, 3, "ok"), "舆情": (0.0, 5, "unknown")}
    # 覆盖率 = 0.5/0.7 ≈ 0.714;阈值设 0.8 → 不达标 → unknown
    net, cover, quality = ev._weighted_net(layers, min_coverage=0.8)
    assert net is None and quality == "unknown"


# ————————————————————————————————————————————————
# aggregate_sentiment：失败数 / status / 只用成功条目
# ————————————————————————————————————————————————
def test_aggregate_partial_failure_counts_and_status():
    """部分条目打分失败(error / scored:false)→ 只用成功条目、失败数正确、status=partial。"""
    events = [
        {"影响方向": "利好", "影响强度": 5, "与本股关系": "直接", "scored": True},   # 成功 +1.0
        {"error": "conn"},                                                          # 失败
        {"scored": False, "影响方向": "利好", "影响强度": 5, "与本股关系": "直接"},  # 显式失败(即便有方向)
    ]
    agg = ev.aggregate_sentiment(events)
    assert agg["失败数"] == 2
    assert agg["样本数"] == 1
    assert agg["status"] == "partial"
    assert agg["净情绪分"] == 1.0            # 只吃那 1 条成功条目


def test_aggregate_all_failed_is_unknown():
    events = [{"error": "x"}, {"scored": False}]
    agg = ev.aggregate_sentiment(events)
    assert agg["status"] == "unknown" and agg["样本数"] == 0 and agg["失败数"] == 2


def test_aggregate_empty_is_missing():
    assert ev.aggregate_sentiment([])["status"] == "missing"


def test_aggregate_scored_but_irrelevant_is_not_failure():
    """成功打分但与本股无关(rel=0)→ 剔除出净情绪,但**不计失败**、status 仍 ok。"""
    events = [{"影响方向": "利好", "影响强度": 5, "与本股关系": "无关", "scored": True}]
    agg = ev.aggregate_sentiment(events)
    assert agg["失败数"] == 0 and agg["status"] == "ok" and agg["样本数"] == 0


# ————————————————————————————————————————————————
# 表态门控：unknown 情绪不投票、不污染综合分;council 与 predict 同款
# ————————————————————————————————————————————————
_TECH_BUY = {"ob_os": {"结论": "超卖"}}      # 超卖 +2 → 单独即偏买入


def test_unknown_sentiment_abstains_in_council_and_predict():
    """质量=unknown 的票(净情绪值本会触发情绪偏多)→ 情绪专家干净弃权,综合分不被拉偏。"""
    sent_unknown = {"净情绪分": 0.5, "样本数": 8, "质量": "unknown"}
    r_c = council.bias_council(_TECH_BUY, None, sent_unknown)
    r_p = pr.bias_recommendation(_TECH_BUY, None, sent_unknown)
    assert r_c == r_p                                  # council/predict 逐字一致
    assert "情绪偏多+2" not in r_c["依据"]             # 情绪未投票
    assert r_c["得分"] == 2                            # 仅超卖,情绪 0 贡献


def test_ok_sentiment_votes_in_council_and_predict():
    """质量=ok 的同值票 → 情绪正常投票(与 unknown 形成对照,锁住两者行为不同)。"""
    sent_ok = {"净情绪分": 0.5, "样本数": 8, "质量": "ok"}
    r_c = council.bias_council(_TECH_BUY, None, sent_ok)
    r_p = pr.bias_recommendation(_TECH_BUY, None, sent_ok)
    assert r_c == r_p
    assert "情绪偏多+2" in r_c["依据"]
    assert r_c["得分"] == 4                            # 超卖2 + 情绪2


def test_true_neutral_ok_sentiment_rates_normally():
    """真中性(ok, net 0.0)→ 情绪按中性正常处理(不加分、也不作为失败弃权),评级照常。"""
    sent_neutral = {"净情绪分": 0.0, "样本数": 8, "质量": "ok"}
    r = council.bias_council(_TECH_BUY, None, sent_neutral)
    assert r["得分"] == 2 and r["结论"] == "偏买入"     # 情绪中性不改判,超卖照常成买入


def test_missing_sentiment_abstains():
    sent_missing = {"净情绪分": None, "样本数": 0, "质量": "missing"}
    r = council.bias_council(_TECH_BUY, None, sent_missing)
    assert "情绪" not in "".join(r["依据"]) and r["得分"] == 2


def test_legacy_sentiment_without_quality_still_votes():
    """向后兼容:旧记录无「质量」字段 → 回退原 net/样本数 判据,情绪照常投票。"""
    legacy = {"净情绪分": 0.5, "样本数": 8}            # 无 质量 键
    r = council.bias_council(_TECH_BUY, None, legacy)
    assert "情绪偏多+2" in r["依据"] and r["得分"] == 4


# ————————————————————————————————————————————————
# provenance / 契约三态
# ————————————————————————————————————————————————
def test_provenance_sentiment_quality_mapping():
    assert serialize._sentiment_quality({"质量": "unknown", "净情绪分": None}) == "unknown"
    assert serialize._sentiment_quality({"质量": "partial", "净情绪分": 0.3}) == "partial"
    assert serialize._sentiment_quality({"质量": "ok", "净情绪分": 0.0}) == "ok"
    assert serialize._sentiment_quality(None) == "missing"


def _minimal_rec(sentiment: dict, prov_sent="unknown") -> dict:
    return {"schema_version": 1,
            "meta": {"code": "000001", "name": "x", "as_of": "2026-09-04"},
            "events": [], "timeseries_refs": {},
            "provenance": {"sentiment": prov_sent},
            "sentiment": sentiment}


def test_contract_unknown_must_be_null_not_zero():
    """契约红线:质量=unknown 却给净情绪分(如 0.0)→ 校验报错(锁死不得冒充中性)。"""
    errs = rc.validate_record(_minimal_rec({"质量": "unknown", "净情绪分": 0.0}))
    assert any("质量=" in e and "净情绪分" in e for e in errs)


def test_contract_unknown_null_is_valid():
    errs = rc.validate_record(_minimal_rec({"质量": "unknown", "净情绪分": None}))
    assert not any("净情绪分" in e and "质量" in e for e in errs)


def test_contract_provenance_sentiment_enum():
    good = rc.validate_record(_minimal_rec({"质量": "ok", "净情绪分": 0.0}, prov_sent="ok"))
    assert not any("provenance.sentiment" in e for e in good)
    bad = rc.validate_record(_minimal_rec({"质量": "ok", "净情绪分": 0.0}, prov_sent="bogus"))
    assert any("provenance.sentiment 非法" in e for e in bad)


def test_contract_registers_quality_enum():
    assert rc.ENUMS["情绪质量"] == ("ok", "partial", "unknown", "missing")
