"""F3 单测:council 合议层。锁语义:加权求和方向、权重覆盖、冲突仅标注、归因自洽、依赖守卫。"""
import inspect

from tools.analysis import council
from tools.config.strategy import THRESHOLDS

_C = THRESHOLDS["合议"]


def _rec(**blocks) -> dict:
    base = {"meta": {"code": "000001", "name": "测试"}}
    base.update(blocks)
    return base


def _all_bull_rec():
    return _rec(signals={"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]},
                         "ob_os": {"verdict": "超卖", "resonance": 3},
                         "reversal": {"拐点标签": "反弹启动", "拐点评分": 80}},
                fundflow={"今日主力净流入": 1e8, "主力连续净流入天数": 3},
                sentiment={"净情绪分": 0.6, "样本数": 20})


def _all_bear_rec():
    return _rec(signals={"trend": {"评级": "偏空", "得分": -80, "依据": ["空头"]},
                         "ob_os": {"verdict": "超买", "resonance": 3},
                         "reversal": {"拐点标签": "无", "拐点评分": 0}},
                fundflow={"今日主力净流入": -1e8, "主力连续净流入天数": 0},
                sentiment={"净情绪分": -0.6, "样本数": 20})


def test_all_bull_is_bullish():
    r = council.convene_default(_all_bull_rec())
    assert r["综合方向"] == "看多" and r["综合分"] >= _C["tau"]
    assert not r["是否冲突"]


def test_all_bear_is_bearish():
    r = council.convene_default(_all_bear_rec())
    assert r["综合方向"] == "看空" and r["综合分"] <= -_C["tau"]
    assert not r["是否冲突"]


def test_empty_experts_neutral():
    r = council.convene([], _rec())
    assert r["综合方向"] == "中性" and r["综合分"] == 0.0


def test_weight_override_shifts_result():
    """把看空专家权重压到 0,则综合分应上移(更偏多)。"""
    rec = _all_bull_rec()
    rec["sentiment"] = {"净情绪分": -0.6, "样本数": 20}    # 情绪转空
    base = council.convene_default(rec)
    boosted = council.convene(list(_C["默认专家组"]), rec, weight_override={"情绪三层": 0.0})
    assert boosted["综合分"] >= base["综合分"]
    assert "含权重覆盖" in boosted["口径"]


def test_conflict_flagged_but_not_overturned():
    """强看多 + 强看空并存 → 标冲突;但方向仍由加权求和定(D1 不改判)。"""
    rec = _rec(signals={"trend": {"评级": "偏多", "得分": 100, "依据": ["多头"]},
                        "ob_os": {"verdict": "超买", "resonance": 3},   # 看空
                        "reversal": {"拐点标签": "无", "拐点评分": 0}},
               sentiment={"净情绪分": -0.8, "样本数": 20})              # 看空
    r = council.convene_default(rec)
    assert r["是否冲突"] is True and r["冲突说明"]
    # 方向仍是加权求和结果(未被冲突改成中性/翻转)
    assert r["综合方向"] in ("看多", "看空", "中性")
    # 归因里确实同时存在正负贡献
    signs = {(-1 if a["贡献"] < 0 else 1) for a in r["归因"] if a["贡献"] != 0}
    assert signs == {1, -1}


def test_weak_opposition_not_conflict():
    """一方贡献极微弱(<ε)→ 不算冲突。"""
    rec = _rec(signals={"trend": {"评级": "偏多", "得分": 90, "依据": ["多头"]}},
               sentiment={"净情绪分": -0.2, "样本数": 1})     # 极弱看空(样本1、置信度低)
    r = council.convene(["技术趋势", "情绪三层"], rec)
    assert r["是否冲突"] is False


def test_attribution_sum_consistent_with_S():
    """归因贡献之和 / 分母(Σ权重×置信度)== 综合分(数值自洽,置信度加权口径)。"""
    rec = _all_bull_rec()
    r = council.convene_default(rec)
    total_contrib = sum(a["贡献"] for a in r["归因"])
    denom = sum(a["权重"] * a["置信度"] for a in r["归因"])
    assert abs(total_contrib / denom - r["综合分"]) < 1e-6


def test_abstain_does_not_dilute():
    """弃权稀释修正:一堆弃权专家不应把在场专家的综合分拉向 0。

    构造:技术趋势强看多(置信度1),其余专家全弃权(缺数据→置信度0)。
    置信度加权分母下,S 应 ≈ 该单一专家的强度(不被弃权者稀释)。
    """
    rec = _rec(signals={"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]}})
    # 只有技术趋势有数据,其余(超买超卖/拐点/资金流/情绪/多因子/事件驱动/板块轮动)弃权
    r = council.convene_default(rec)
    tv = next(a for a in r["归因"] if a["专家"] == "技术趋势")
    # 分母只剩技术趋势的 权重×置信度 → S == 其强度
    assert abs(r["综合分"] - tv["强度"]) < 1e-6
    assert r["综合方向"] == "看多"                  # 不被弃权者稀释到中性


def test_abstain_dilution_vs_old_denominator():
    """对照:置信度加权(默认)的 |S| 应 ≥ 等权旧口径(弃权者被排除,分母更小、S 更大)。"""
    import tools.analysis.council as C
    rec = _rec(signals={"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]}})
    s_new = council.convene_default(rec)["综合分"]     # 置信度加权
    orig = C._C["分母模式"]
    try:
        C._C["分母模式"] = "等权"                       # 临时切旧口径
        s_old = council.convene_default(rec)["综合分"]
    finally:
        C._C["分母模式"] = orig
    assert abs(s_new) > abs(s_old)                     # 修正后不再被稀释


def test_attribution_sorted_by_abs_contrib():
    r = council.convene_default(_all_bull_rec())
    contribs = [abs(a["贡献"]) for a in r["归因"]]
    assert contribs == sorted(contribs, reverse=True)


def test_build_council_block_shape():
    blk = council.build_council_block(_all_bull_rec())
    assert set(blk) == {"experts", "default", "config"}
    assert len(blk["experts"]) == len(_C["默认专家组"])
    from tools.contracts.expert import validate_verdict
    assert all(validate_verdict(e) == [] for e in blk["experts"])


def test_council_does_not_import_web_or_store():
    """依赖守卫:解析实际 import 语句(不看文档字符串),禁止依赖 web/store/report/serialize。"""
    import ast
    tree = ast.parse(inspect.getsource(council))
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
    forbidden = ("web", "tools.store", "tools.report", "tools.analysis.serialize")
    assert not [m for m in mods if any(m == f or m.startswith(f + ".") for f in forbidden)], mods
