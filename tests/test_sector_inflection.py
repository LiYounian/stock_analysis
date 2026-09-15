"""④ 消息驱动板块拐点·应用层 单测（合成数据锁检测语义）。

锁"冷→热正拐点"判定 + 情绪 forward-only 优雅缺省 + 成员缩量回踩，
防未来 prompt/代码重写破坏 non-gating advisory 的信号语义。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.research import sector_inflection as si


def _panel(rows):
    """rows=[(date,industry,动量_时序分位,动量_时序档), ...] → panel DataFrame。"""
    return pd.DataFrame([{"date": d, "industry": ind, "动量_时序分位": q, "动量_时序档": g}
                         for d, ind, q, g in rows])


def test_vol_price_inflection_fires_on_档翻转():
    # 昨"冷"今"温" → 正拐点
    p = _panel([("2026-09-09", "电子", 0.20, "冷"),
                ("2026-09-10", "电子", 0.28, "冷"),
                ("2026-09-11", "电子", 0.45, "温")])
    ok, why, info = si.vol_price_inflection(p, "电子", ["2026-09-09", "2026-09-10", "2026-09-11"])
    assert ok and "冷→温" in why and info["动量档"] == "温"


def test_vol_price_inflection_fires_on_分位低位上穿():
    # 档未翻(都温)但分位低位上穿 0.28→0.45
    p = _panel([("d1", "存储", 0.22, "温"), ("d2", "存储", 0.28, "温"), ("d3", "存储", 0.45, "温")])
    ok, why, _ = si.vol_price_inflection(p, "存储", ["d1", "d2", "d3"])
    assert ok and "上穿" in why


def test_vol_price_no_inflection_on_flat():
    p = _panel([("d1", "银行", 0.90, "热"), ("d2", "银行", 0.92, "热"), ("d3", "银行", 0.93, "热")])
    ok, _, _ = si.vol_price_inflection(p, "银行", ["d1", "d2", "d3"])
    assert not ok


def test_vol_price_no_inflection_on_持续冷():
    p = _panel([("d1", "地产", 0.10, "冷"), ("d2", "地产", 0.12, "冷"), ("d3", "地产", 0.15, "冷")])
    ok, _, _ = si.vol_price_inflection(p, "地产", ["d1", "d2", "d3"])
    assert not ok


def test_sentiment_inflection_forward_empty_graceful(monkeypatch):
    # shadow 无历史 → get_industry_sentiment_series 返回 {} → 优雅 False，不报错
    monkeypatch.setattr(si.sentiment_judge, "get_industry_sentiment_series",
                        lambda ind, dates, sd: {})
    ok, why, na = si.sentiment_inflection("电子", ["d1", "d2"], "/nonexist")
    assert not ok and "forward未积累" in why and na is None


def test_sentiment_inflection_fires_on_净A度抬升(monkeypatch):
    monkeypatch.setattr(si.sentiment_judge, "get_industry_sentiment_series",
                        lambda ind, dates, sd: {"d1": -0.10, "d2": 0.20})
    ok, why, na = si.sentiment_inflection("电子", ["d1", "d2"], "/x", delta_min=0.15)
    assert ok and na == 0.20


def test_member_pullback_缩量回踩():
    # 造上升趋势中、缩量、贴MA20、温和整理的一天
    n = 40
    close = np.linspace(9, 11, n)                      # 上升趋势
    close[-1] = close[-2] * 0.995                      # 末日温和回踩 -0.5%
    vol = np.full(n, 1e6); vol[-1] = 5e5               # 末日缩量
    df = pd.DataFrame({"date": pd.date_range("2026-07-01", periods=n).astype(str),
                       "open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": vol,
                       "pct_chg": pd.Series(close).pct_change().fillna(0).to_numpy() * 100})
    assert si.member_pullback(df, n - 1) is True
    # 放量大涨日 → 非缩量回踩
    df2 = df.copy(); df2.loc[n - 1, "volume"] = 3e6; df2.loc[n - 1, "pct_chg"] = 8.0
    assert si.member_pullback(df2, n - 1) is False


def test_inflection_正拐点_property():
    a = si.Inflection("电子", "d", 价量拐点=True)
    b = si.Inflection("银行", "d")
    assert a.正拐点 and not b.正拐点
