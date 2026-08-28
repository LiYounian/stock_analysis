"""consensus.py 单测(纯解析/派生,不触网)。

锁语义:年度/EPS/机构数列名容错解析、前瞻当年/次年选取与增速派生、空降级。
"""
import pandas as pd

from tools.collectors import consensus


def test_parse_forecast_colname_tolerance():
    df = pd.DataFrame([
        {"预测年度": "2026", "预测每股收益": 2.0, "机构数": 6},
        {"预测年度": "2027", "预测每股收益": 2.6, "机构数": 5},
        {"预测年度": "2028E", "预测每股收益": None, "机构数": 3},   # EPS 缺→丢弃
    ])
    fc = consensus._parse_forecast(df)
    assert set(fc.keys()) == {"2026", "2027"}
    assert fc["2026"]["eps"] == 2.0 and fc["2026"]["insts"] == 6


def test_eps_col_fuzzy_match():
    # 无精确列名,靠「含每股收益且不含最小/最大」兜底
    df = pd.DataFrame([{"年度": "2026", "预测每股收益(元)": 1.5,
                        "预测每股收益-最小": 1.2, "预测每股收益-最大": 1.8}])
    fc = consensus._parse_forecast(df)
    assert fc["2026"]["eps"] == 1.5


def test_summarize_growth():
    fc = {"2026": {"eps": 2.0, "insts": 6}, "2027": {"eps": 2.6, "insts": 5}}
    s = consensus.summarize(fc)
    assert s["预期EPS当年"] == 2.0 and s["预期EPS次年"] == 2.6
    assert s["预期增速"] == 0.3 and s["覆盖机构数"] == 6


def test_summarize_empty():
    s = consensus.summarize({})
    assert s["预期EPS当年"] is None and s["预期增速"] is None
