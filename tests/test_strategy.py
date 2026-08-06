"""策略层注册表单测:注册/get/list/run + 非法 kind/重名抛错 + 三类示例签名。

断言锁住 docs/信息流转与层职责.md §2.3(B) 的接口语义:
- kind 只允许 选股/评分/信号,非法即抛(防未来误注册跑偏);
- 重名必须抛(防覆盖已有策略);
- 复用的 screener 预设能用 fake records 跑通(选股签名 records->list[code]);
- 评分/信号示例各自守签名。
"""
import pandas as pd
import pytest

from tools.strategy import registry as reg


# ————————————————————————————————————————————————
# fake 中心记录(对齐 tests/test_screener.py 的构造口径)
# ————————————————————————————————————————————————
def _rec(code, trend_score, rating, rev_label="无", rev_score=0,
         flow_days=0, flow_today=0.0, roe=None, pe_valid=False):
    return {
        "meta": {"code": code, "name": code},
        "signals": {"trend": {"评级": rating, "得分": trend_score},
                    "reversal": {"拐点标签": rev_label, "拐点评分": rev_score}},
        "fundflow": {"主力连续净流入天数": flow_days, "今日主力净流入": flow_today},
        "fundamental": {"ROE": roe},
        "valuation": {"pe_valid": pe_valid},
    }


def _pool():
    return {
        "A": _rec("A", 40, "偏多", "反弹启动", 60, flow_days=3, flow_today=1e8,
                  roe=2.0, pe_valid=True),
        "B": _rec("B", -50, "偏空", "超跌待反弹", 30, flow_days=0, flow_today=-1e8),
        "C": _rec("C", 10, "中性", flow_days=2, flow_today=5e7, roe=0.5, pe_valid=False),
    }


# ————————————————————————————————————————————————
# 注册 / get / list / run 正常路径
# ————————————————————————————————————————————————
def test_kinds_constant():
    assert reg.STRATEGY_KINDS == ("选股", "评分", "信号")


def test_screener_presets_registered_as_screen():
    """screener 的每个预设都应被包装成"选股"策略注册。"""
    from tools.screener import screen as sc
    screen_names = reg.list_strategies("选股")
    for name in sc.PRESETS:
        assert name in screen_names


def test_get_returns_meta_with_fn():
    meta = reg.get("均线金叉")
    assert meta.name == "均线金叉"
    assert meta.kind == "信号"
    assert callable(meta.fn)
    assert meta.params_schema is not None


def test_list_filter_by_kind():
    all_names = set(reg.list_strategies())
    screen = set(reg.list_strategies("选股"))
    score = set(reg.list_strategies("评分"))
    signal = set(reg.list_strategies("信号"))
    # 分区不重叠且并起来是全集
    assert screen & score == set()
    assert screen & signal == set()
    assert score & signal == set()
    assert screen | score | signal == all_names
    assert "买卖倾向评分" in score
    assert "均线金叉" in signal


def test_run_dispatches_to_fn():
    # run 等价于 get(name).fn(...)
    out = reg.run("买卖倾向评分", {"prediction": {"买卖倾向": {"得分": 3, "依据": ["x"]}}})
    assert out == {"score": 3.0, "依据": ["x"]}


# ————————————————————————————————————————————————
# 抛错语义:非法 kind / 重名
# ————————————————————————————————————————————————
def test_illegal_kind_raises():
    with pytest.raises(ValueError):
        @reg.strategy("_临时_非法kind", "回测")
        def _f(x):
            return x


def test_illegal_kind_on_list_raises():
    with pytest.raises(ValueError):
        reg.list_strategies("不存在")


def test_duplicate_name_raises():
    @reg.strategy("_临时_唯一名", "评分")
    def _f(r):
        return {"score": 0.0, "依据": []}

    with pytest.raises(ValueError):
        @reg.strategy("_临时_唯一名", "评分")
        def _g(r):
            return {"score": 1.0, "依据": []}


def test_get_missing_raises():
    with pytest.raises(KeyError):
        reg.get("_不存在的策略_")


# ————————————————————————————————————————————————
# 选股策略(复用 screener 预设)用 fake records 跑通
# ————————————————————————————————————————————————
def test_reuse_screen_strategy_runs():
    pool = _pool()
    hit = reg.run("趋势强势", pool)          # 趋势得分>=30 仅 A
    assert hit == ["A"]

    hit2 = reg.run("主力吸筹", pool)          # 连续净流入>=2 且今日为正 → A、C
    assert set(hit2) == {"A", "C"}


def test_reuse_screen_returns_list_of_codes():
    hit = reg.run("超跌反弹候选", _pool())
    assert isinstance(hit, list)
    assert all(isinstance(c, str) for c in hit)
    assert set(hit) == {"A", "B"}


# ————————————————————————————————————————————————
# 评分示例:签名 record -> {score, 依据}
# ————————————————————————————————————————————————
def test_score_signature():
    out = reg.run("买卖倾向评分",
                  {"prediction": {"买卖倾向": {"结论": "偏买入", "得分": 2.5, "依据": ["超卖", "偏多"]}}})
    assert set(out) == {"score", "依据"}
    assert out["score"] == 2.5
    assert out["依据"] == ["超卖", "偏多"]


def test_score_missing_data_zero():
    out = reg.run("买卖倾向评分", {})
    assert out["score"] == 0.0
    assert isinstance(out["依据"], list) and out["依据"]


# ————————————————————————————————————————————————
# 信号示例:签名 kline_df -> 逐日 买/卖/持
# ————————————————————————————————————————————————
def test_signal_signature_and_values():
    # 构造先跌后涨的收盘序列,制造一次金叉(买)
    closes = [10 - i * 0.3 for i in range(25)] + [3 + i * 0.8 for i in range(25)]
    df = pd.DataFrame({"close": closes})
    sig = reg.run("均线金叉", df, short=5, long=20)
    assert len(sig) == len(closes)
    assert set(sig) <= {"买", "卖", "持"}
    assert sig[0] == "持"                      # 首根无历史
    assert "买" in sig                          # 反转段应触发金叉买入


def test_signal_accepts_bare_sequence():
    # 直接传收盘价序列也应工作(签名容错)
    sig = reg.run("均线金叉", [1.0] * 30)
    assert len(sig) == 30
    assert set(sig) == {"持"}                   # 全平无交叉
