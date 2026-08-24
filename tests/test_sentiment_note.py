"""第二步·保守版消息面提示 _sentiment_note 单测 + '不改预测数字'回归 + 契约兼容。

锁住语义(守则6):
- 方向分档复用买卖倾向 ±情绪阈值;看涨/看跌/中性。
- 向后兼容:无 sentiment / 样本数0 / 新鲜度=无数据 → None。
- 新鲜度=陈旧 → 文案标"消息偏旧,仅参考"。
- 与纯技术趋势评级 一致/背离。
- **红线**:加提示不改动 情景预测/持有期建议/结构位 任何数字。
"""
import math

import pandas as pd

from tools.analysis import predict as pr
from tools.analysis import technical as ta


def _sent(net, n=5, 好=3, 坏=1, 新鲜度=None):
    d = {"净情绪分": net, "样本数": n, "利好数": 好, "利空数": 坏}
    if 新鲜度 is not None:
        d["新鲜度"] = 新鲜度
    return d


def _tech(rating):
    return {"signal": {"评级": rating}}


def test_note_direction_buckets():
    assert "看涨" in pr._sentiment_note(_sent(0.35))
    assert "看跌" in pr._sentiment_note(_sent(-0.35))
    assert "中性" in pr._sentiment_note(_sent(0.05))
    note = pr._sentiment_note(_sent(0.35))
    assert "+0.35" in note and "共 5 条" in note and "利好 3/利空 1" in note


def test_note_none_cases():
    assert pr._sentiment_note(None) is None
    assert pr._sentiment_note(_sent(0.5, n=0)) is None            # 样本数0
    assert pr._sentiment_note({"样本数": 3}) is None               # 无净情绪分
    assert pr._sentiment_note(_sent(0.5, 新鲜度="无数据")) is None  # 无数据不出提示


def test_note_stale_flag():
    stale = pr._sentiment_note(_sent(0.35, 新鲜度="陈旧"))
    assert "看涨" in stale and "消息偏旧" in stale
    fresh = pr._sentiment_note(_sent(0.35, 新鲜度="新鲜"))
    assert "消息偏旧" not in fresh


def test_note_agree_diverge():
    assert "与技术面一致" in pr._sentiment_note(_sent(0.35), _tech("偏多"))
    assert "背离" in pr._sentiment_note(_sent(0.35), _tech("偏空"))
    # 技术中性 → 不下一致/背离结论
    neu = pr._sentiment_note(_sent(0.35), _tech("中性"))
    assert "一致" not in neu and "背离" not in neu


def _kline(n=80):
    close = [20 + math.sin(i / 6) * 2 + i * 0.02 for i in range(n)]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n, freq="D"),
        "open": close, "high": [c + 0.5 for c in close], "low": [c - 0.5 for c in close],
        "close": close, "volume": [1e6] * n, "pct_chg": [0.0] * n,
    })


def test_conservative_does_not_change_predictions():
    """红线:加消息面提示,情景预测/持有期建议/结构位等数字逐字段不变。"""
    k = _kline()
    tech = ta.compute(k)
    strong = _sent(0.8, n=20, 好=15, 坏=1, 新鲜度="新鲜")
    a = pr.predict(k, tech, None, sentiment=None, with_conditional=False)
    b = pr.predict(k, tech, None, sentiment=strong, with_conditional=False)
    for key in ("情景预测", "持有期建议", "结构位", "现价", "atr", "atr_pct", "支撑位", "压力位"):
        assert a[key] == b[key], f"{key} 不应随消息面变化"
    assert a["消息面提示"] is None
    assert b["消息面提示"] is not None and "看涨" in b["消息面提示"]


def test_prediction_contract_tolerates_note():
    """prediction 带/不带 消息面提示 都过契约(可空、旧记录兼容)。"""
    from tools.contracts import record as contracts
    rec = {k: None for k in contracts.REQUIRED_TOP}
    rec["meta"] = {"code": "000001", "name": "测试", "as_of": "2026-08-25"}
    rec["prediction"] = {"买卖倾向": {"结论": "观望"},
                         "消息面提示": "消息面看涨(净情绪 +0.35,共 8 条)。仅供留意,不改预测。"}
    assert contracts.validate_record(rec) == []
    rec["prediction"]["消息面提示"] = None      # 旧记录/无消息
    assert contracts.validate_record(rec) == []
