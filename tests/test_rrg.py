"""F8 单测:板块轮动 RRG 专家。

锁语义:
  - RS 线 / RS-Ratio / RS-Momentum 的计算口径(相对基准 + 二阶动量,绕 100 中枢);
  - 四象限判定(领先/走弱/改善/落后)与 象限→方向 映射;
  - compute_series 的强度符号与方向一致(过契约守门)、数据充分度分档、样本不足→None;
  - 专家产 ExpertVerdict:看多/看空/弃权三态,行业缺 / RRG 数据缺 → 弃权可见(中性+置信度0);
  - 注册:进 BUILTIN、进「合议.默认专家组」、进默认权重;build_council_block 含之且不炸(空 store 弃权);
  - config 块可 JSON 序列化(dump 不漂移)。

不依赖磁盘数据:纯计算用合成序列;专家/合议路径用 monkeypatch(data-independent)。
"""
import json

import pytest

from tools.analysis import experts, rrg
from tools.contracts.expert import validate_verdict
from tools.config.strategy import THRESHOLDS, dump_json

_C = THRESHOLDS["板块轮动"]


@pytest.fixture(autouse=True)
def _clear_rrg_cache():
    """每个用例前后清 RRG 缓存,避免跨用例污染(基准/行业惰性缓存)。"""
    rrg.clear_cache()
    yield
    rrg.clear_cache()


# ———————————— 纯计算:RS 线 / Ratio / Momentum ————————————
def test_rs_line_relative_strength():
    board = [110.0, 121.0, 133.1]          # 行业强于基准
    bench = [100.0, 100.0, 100.0]
    rs = rrg.rs_line(board, bench)
    assert rs == [110.0, 121.0, 133.1]     # 100×board/bench,基准平 → rs=board
    # 尾部等长对齐:较长的一侧取尾部,与基准逐位对齐
    assert rrg.rs_line([1.0, 2.0, 110.0, 121.0, 133.1], bench) == [110.0, 121.0, 133.1]


def test_rs_line_zero_benchmark_raises():
    with pytest.raises(ValueError):
        rrg.rs_line([100.0, 101.0], [0.0, 100.0])


def test_rs_ratio_series_above_100_when_rising():
    rs = [float(x) for x in range(1, 51)]  # 递增
    ratio = rrg.rs_ratio_series(rs, win=10)
    assert len(ratio) == len(rs) - 10 + 1
    assert ratio[-1] > 100.0               # 最新值高于自身均线 → 走强


def test_rs_momentum_series_length_and_center():
    ratio = [100.0] * 20                    # 恒定 → 动量恰在中枢
    mom = rrg.rs_momentum_series(ratio, win=5)
    assert len(mom) == len(ratio) - 5 + 1
    assert all(abs(m - 100.0) < 1e-9 for m in mom)


# ———————————— 四象限判定 ————————————
@pytest.mark.parametrize("ratio,mom,expect", [
    (105.0, 105.0, "领先"),
    (105.0, 95.0, "走弱"),
    (95.0, 105.0, "改善"),
    (95.0, 95.0, "落后"),
    (100.0, 100.0, "领先"),                # 中枢含 = 归强/动量正
])
def test_classify_quadrants(ratio, mom, expect):
    assert rrg.classify(ratio, mom) == expect


# ———————————— 象限 → 方向 + 强度符号一致 ————————————
@pytest.mark.parametrize("象限,方向,符号", [
    ("领先", "看多", 1),
    ("改善", "看多", 1),
    ("落后", "看空", -1),
    ("走弱", "中性", 0),
])
def test_direction_strength_sign(象限, 方向, 符号):
    d, s = rrg._direction_strength(象限, 106.0, 103.0 if 符号 >= 0 else 97.0)
    assert d == 方向
    if 符号 > 0:
        assert s >= 0
    elif 符号 < 0:
        # 落后:两指标都在弱侧,强度应为负
        d2, s2 = rrg._direction_strength("落后", 96.0, 96.0)
        assert d2 == "看空" and s2 < 0
    else:
        assert s == 0.0


# ———————————— compute_series:符号一致 / 充分度 / 样本不足 ————————————
def _up_series(n):
    """加速上行(凸增)→ 相对基准走强且动量为正。"""
    return [100.0 * (1.0 + 0.0004 * t * t) for t in range(n)]


def _down_series(n):
    return [100.0 / (1.0 + 0.0004 * t * t) for t in range(n)]


def test_compute_series_up_is_not_bearish():
    n = 70
    row = rrg.compute_series(_up_series(n), [100.0] * n)
    assert row is not None
    assert row["方向"] in ("看多", "中性") and row["强度"] >= 0
    assert row["数据充分度"] == "充分"        # 70 ≥ 充分样本(60)
    # 由 compute 结果拼 ExpertVerdict 字段应过契约(符号一致)
    v = {"专家": "板块轮动", "能力类型": "方向", "方向": row["方向"], "强度": row["强度"],
         "置信度": 1.0, "默认权重": 1.0, "依据": row["依据"],
         "数据充分度": row["数据充分度"], "原始": {}}
    assert validate_verdict(v) == []


def test_compute_series_down_is_not_bullish():
    n = 70
    row = rrg.compute_series(_down_series(n), [100.0] * n)
    assert row is not None
    assert row["方向"] in ("看空", "中性") and row["强度"] <= 0


def test_compute_series_partial_when_short():
    n = 55                                    # 49 ≤ 55 < 60 → 部分降级
    row = rrg.compute_series(_up_series(n), [100.0] * n)
    assert row is not None and row["数据充分度"] == "部分降级"


def test_compute_series_none_when_too_few():
    n = 45                                    # < win_ratio+win_mom-1(49)→ 算不出
    assert rrg.compute_series(_up_series(n), [100.0] * n) is None


# ———————————— 专家路径:看多 / 弃权 ————————————
def _rec(industry="电子", code="000001"):
    meta = {"code": code, "name": "测试"}
    if industry is not None:
        meta["industry"] = industry
    return {"meta": meta}


def test_expert_bullish_from_leading(monkeypatch):
    monkeypatch.setattr(rrg, "industry_row", lambda name: {
        "象限": "领先", "方向": "看多", "强度": 0.7, "RS_Ratio": 106.0,
        "RS_Momentum": 103.0, "数据充分度": "充分", "依据": ["领先象限·RS-Ratio 106.0·RS-Momentum 103.0"]})
    v = experts.build("板块轮动", _rec("电子"))
    assert v.专家 == "板块轮动" and v.方向 == "看多" and v.强度 == 0.7
    assert v.置信度 == 1.0 and v.数据充分度 == "充分"
    assert v.原始["象限"] == "领先" and v.原始["行业"] == "电子"
    assert validate_verdict(v) == []


def test_expert_bearish_from_lagging(monkeypatch):
    monkeypatch.setattr(rrg, "industry_row", lambda name: {
        "象限": "落后", "方向": "看空", "强度": -0.6, "RS_Ratio": 94.0,
        "RS_Momentum": 96.0, "数据充分度": "部分降级", "依据": ["落后象限"]})
    v = experts.build("板块轮动", _rec("钢铁"))
    assert v.方向 == "看空" and v.强度 < 0 and v.置信度 == 0.5
    assert validate_verdict(v) == []


def test_expert_abstains_when_no_industry(monkeypatch):
    # 无 industry 且 board_of 回退查不到 → 弃权
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda code: None)
    v = experts.build("板块轮动", _rec(industry=None))
    assert v.方向 == "中性" and v.强度 == 0.0 and v.置信度 == 0.0
    assert v.数据充分度 == "缺失"
    assert validate_verdict(v) == []


def test_expert_abstains_when_industry_has_no_rrg(monkeypatch):
    monkeypatch.setattr(rrg, "industry_row", lambda name: None)   # 名称口径不一致 / 数据缺
    v = experts.build("板块轮动", _rec("不存在的行业"))
    assert v.方向 == "中性" and v.置信度 == 0.0 and v.数据充分度 == "缺失"
    assert validate_verdict(v) == []


def test_expert_uses_board_of_fallback(monkeypatch):
    from tools.collectors import board
    monkeypatch.setattr(board, "board_of", lambda code: "银行")
    monkeypatch.setattr(rrg, "industry_row", lambda name: {
        "象限": "改善", "方向": "看多", "强度": 0.2, "RS_Ratio": 98.0,
        "RS_Momentum": 101.0, "数据充分度": "充分", "依据": ["改善象限"]} if name == "银行" else None)
    v = experts.build("板块轮动", _rec(industry=None))   # 无 industry → 回退 board_of → "银行"
    assert v.方向 == "看多" and v.原始["行业"] == "银行"


# ———————————— industry_row 恒不抛(store 异常→降级) ————————————
def test_industry_row_never_raises_on_store_error(monkeypatch):
    from tools.collectors import index
    monkeypatch.setattr(index, "load_index", lambda code: (_ for _ in ()).throw(FileNotFoundError("no 沪深300")))
    rrg.clear_cache()
    assert rrg.industry_row("电子") is None       # 基准缺 → None,不抛


# ———————————— 注册 / 三对齐(专家体系侧)————————————
def test_registered_in_builtin_and_default_group():
    assert "板块轮动" in experts.BUILTIN
    assert "板块轮动" in THRESHOLDS["合议"]["默认专家组"]
    assert THRESHOLDS["合议"]["默认权重"]["板块轮动"] == 1.0


def test_council_block_includes_rrg_and_survives_empty_store():
    """空 store 下,板块轮动在默认组里弃权,不炸批量;信封数 = 默认组人数。"""
    from tools.analysis import council
    rrg.clear_cache()
    rec = {"meta": {"code": "000001", "name": "测试", "industry": "查无此行业_ZZZ"}}
    blk = council.build_council_block(rec)
    assert len(blk["experts"]) == len(THRESHOLDS["合议"]["默认专家组"])
    rrg_env = [e for e in blk["experts"] if e["专家"] == "板块轮动"]
    assert len(rrg_env) == 1
    assert rrg_env[0]["方向"] == "中性" and rrg_env[0]["置信度"] == 0.0


# ———————————— config 块 JSON 可序列化(dump 不漂移)————————————
def test_config_block_json_serializable():
    blk = THRESHOLDS["板块轮动"]
    for k in ("基准", "RS_Ratio窗口", "RS_Momentum窗口", "象限中枢", "强度归一scale", "充分样本"):
        assert k in blk
    round_trip = json.loads(json.dumps(THRESHOLDS, ensure_ascii=False))
    assert round_trip["板块轮动"] == blk
    assert "板块轮动" in round_trip["合议"]["默认专家组"]
    assert callable(dump_json)          # dump 入口在(真正落盘由构建步骤跑,不在测试里改文件)
