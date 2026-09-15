"""④ 消息驱动板块拐点·应用层 单测（合成数据锁检测语义）。

锁"冷→热正拐点"判定 + 情绪 forward-only 优雅缺省 + 成员缩量回踩，
防未来 prompt/代码重写破坏 non-gating advisory 的信号语义。
"""
from __future__ import annotations

import json

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


# ---------- CLI（每日 shadow runner）冒烟 ----------
def test_cli_非交易日跳过不落盘(monkeypatch, tmp_path):
    # 非交易日 → 返 0、不加载 K 线、不落盘
    monkeypatch.setattr(si, "_load_all_klines",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("非交易日不应加载K线")))
    import tools.collectors.calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    rc = si.main(["--date", "2026-09-13", "--out-dir", str(tmp_path)])
    assert rc == 0 and not list(tmp_path.glob("*.json"))


def test_cli_交易日落盘_advisory_schema(monkeypatch, tmp_path):
    # 交易日 + 注入合成 K 线 → 落盘 advisory，schema 带 non_gating 标记
    n = 40
    close = np.linspace(9, 11, n)
    df = pd.DataFrame({"date": pd.date_range("2026-07-21", periods=n).astype(str),
                       "open": close, "high": close * 1.01, "low": close * 0.99,
                       "close": close, "volume": np.full(n, 1e6),
                       "pct_chg": pd.Series(close).pct_change().fillna(0).to_numpy() * 100})
    last = df["date"].iloc[-1]
    monkeypatch.setattr(si, "_load_all_klines", lambda *a, **k: {"000001": df})
    monkeypatch.setattr(si, "detect", lambda *a, **k: [])  # 隔离拐点检测(regime panel 需真数据)
    import tools.collectors.calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    rc = si.main(["--date", last, "--out-dir", str(tmp_path), "--shadow-dir", str(tmp_path / "sd")])
    assert rc == 0
    out = tmp_path / f"{last}.json"
    assert out.exists()
    adv = json.loads(out.read_text(encoding="utf-8"))
    assert adv["non_gating"] is True and adv["非validated"] is True and adv["date"] == last


def test_cli_幂等跳过已存在(monkeypatch, tmp_path):
    import tools.collectors.calendar as cal
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    (tmp_path / "2026-07-21.json").write_text("{}", encoding="utf-8")
    # 已存在且无 --force → 不加载 K 线、直接返 0
    monkeypatch.setattr(si, "_load_all_klines",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("幂等应跳过加载")))
    rc = si.main(["--date", "2026-07-21", "--out-dir", str(tmp_path)])
    assert rc == 0
