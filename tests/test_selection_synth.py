"""S5 选股合成 · 语义锁测试(离线·monkeypatch LLM,不烧网关)。

锁死:
  ① 产物结构完整(md/json 结构、必备段落/字段)。
  ② 反选纪律:涨停不可买 / 财报红旗 / 龙虎榜否决 / 极高位高抛压 → 档=剔除(即便 LLM 给推荐)。
  ③ 龙头/中军为主线,跟涨只『观察』(LLM 给推荐也被降级)。
  ④ 非 gating:不改 sector_focus/策略0合议 等上游产物字节。
  ⑤ DeepSeek 调用可 monkeypatch(离线全程不触网/不烧 LLM)。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tools.analysis import selection_synth as ss


# ──────────────── 假 K 线构造(可控形态) ────────────────
def _mk_kline(n=120, base=100.0, last_pct=0.02, uptrend=0.0, high_far=True):
    """构造 n 根升序 K 线;last_pct=当日涨幅。

    high_far=True → 尾段回踩:近 8 根从峰值回落 ~6%,现价明显低于 20 日高(健康低吸,不贴高)。
    high_far=False → 现价贴近高点(极高位)。
    """
    dates = pd.bdate_range("2026-03-01", periods=n)
    close = (base + np.arange(n) * uptrend).astype(float)
    if high_far:
        peak = close[-8]
        close[-8:] = np.linspace(peak, peak * 0.94, 8)   # 峰后回踩 ~6%
    else:
        close[-1] = close[:-1].max() * 1.001             # 创新高(贴高)
    close[-1] = close[-2] * (1 + last_pct)
    high = close * 1.01
    low = close * 0.99
    return pd.DataFrame({
        "date": dates, "open": close * 0.999, "high": high, "low": low,
        "close": close, "volume": np.full(n, 1e6), "amount": close * 1e6,
        "turnover": np.full(n, 2.0), "pct_chg": np.zeros(n),
    })


# ──────────────── ② 反选纪律硬闸(不依赖 LLM) ────────────────
def test_hard_veto_涨停不可买():
    form = {"涨停不可买": True, "距20高": -0.1, "获利盘": 0.5}
    assert ss.hard_veto_reason(ss.ROLE_LEADER, {"财报红旗数": 0}, form)
    s = ss.apply_hard_discipline({"档": "推荐", "建议分": 8.0}, ss.ROLE_LEADER,
                                 {"财报红旗数": 0}, form)
    assert s["档"] == "剔除" and s["建议分"] <= 2


def test_hard_veto_财报红旗():
    s = ss.apply_hard_discipline({"档": "推荐", "建议分": 7.5}, ss.ROLE_CORE,
                                 {"财报红旗数": 1}, {"涨停不可买": False, "距20高": -0.2})
    assert s["档"] == "剔除" and "红旗" in s.get("硬纪律命中", "")


def test_hard_veto_龙虎榜否决():
    s = ss.apply_hard_discipline({"档": "推荐", "建议分": 6.0}, ss.ROLE_LEADER,
                                 {"财报红旗数": 0, "龙虎榜否决": True},
                                 {"涨停不可买": False, "距20高": -0.3})
    assert s["档"] == "剔除"


def test_hard_veto_极高位高抛压():
    form = {"涨停不可买": False, "获利盘": 0.97, "距20高": -0.005, "位置pos60": 0.99}
    assert ss.hard_veto_reason(ss.ROLE_LEADER, {}, form)
    # 高获利但远离高点 → 不命中极高位闸
    assert ss.hard_veto_reason(ss.ROLE_LEADER, {}, {"获利盘": 0.97, "距20高": -0.15}) is None


# ──────────────── ③ 跟涨主线纪律 ────────────────
def test_跟涨最高只观察():
    s = ss.apply_hard_discipline({"档": "推荐", "建议分": 8.0}, ss.ROLE_FOLLOW,
                                 {"财报红旗数": 0}, {"涨停不可买": False, "距20高": -0.2})
    assert s["档"] == "观察"


def test_中军推荐不被降级():
    s = ss.apply_hard_discipline({"档": "推荐", "建议分": 8.0}, ss.ROLE_CORE,
                                 {"财报红旗数": 0}, {"涨停不可买": False, "距20高": -0.2,
                                                    "获利盘": 0.6})
    assert s["档"] == "推荐"


# ──────────────── 形态指标 as-of/防未来 ────────────────
def test_pattern_metrics_基本量():
    df = _mk_kline(n=120, last_pct=0.03, uptrend=0.1)
    m = ss.pattern_metrics(df, "600183", date="2026-08-01")
    assert not m.get("数据不足")
    assert m["现价"] > 0 and m["ma5"] > 0
    # as-of 切片:date 之后的行不计入
    m_full = ss.pattern_metrics(df, "600183", date="2026-12-31")
    assert m_full["现价"] != m["现价"]


def test_pattern_metrics_涨停不可买():
    df = _mk_kline(n=80, last_pct=0.099, base=50.0)          # 主板≈涨停
    m = ss.pattern_metrics(df, "600001", date=str(df["date"].iloc[-1].date()))
    assert m["涨停不可买"] is True
    df2 = _mk_kline(n=80, last_pct=0.02, base=50.0)
    m2 = ss.pattern_metrics(df2, "600001", date=str(df2["date"].iloc[-1].date()))
    assert m2["涨停不可买"] is False


def test_pattern_metrics_历史不足():
    df = _mk_kline(n=30)
    assert ss.pattern_metrics(df, "600001", date="2026-12-31").get("数据不足")


# ──────────────── ① + ③ + ⑤ 端到端(monkeypatch LLM) ────────────────
class _FakeClient:
    """假 LLM:一律给『推荐』高分(故意违纪),用于验证硬闸/主线纪律在代码层强制生效。"""
    def extract(self, text, schema, *, instruction, temperature=0.0):
        import json
        payload = json.loads(text)
        return {"个股": [{"code": c["code"], "name": c.get("name", ""), "档": "推荐",
                         "建议分": 9.0, "理由": "fake", "入场": "~ma5", "止损": "ma20",
                         "风险": "-"} for c in payload["候选"]],
                "规避提示": "无"}


def _fake_synthesize(monkeypatch, tmp_path):
    """构造最小 sector_focus + roster + K 线,全 monkeypatch 离线。"""
    date = "2026-09-16"
    # 候选:龙头(涨停不可买)/中军(健康)/跟涨(健康)
    boards_block = {
        "board": "电子", "tag": "利好", "强弱": "强", "持续性": "两周多条同向",
        "龙头候选": [{"code": "605058", "name": "澳弘电子", "已动": True}],
        "跟涨候选": [{"code": "688802", "name": "沐曦股份", "联动依据": "补涨"}],
    }
    focus = {"date": date, "风险偏好": {"风险偏好": "中性", "广度档": "中性"},
             "宏观净方向": "中性", "宏观情景": "中性",
             "消息驱动": {"as_of": date, "利好板块": [boards_block]}}

    monkeypatch.setattr(ss, "load_sector_focus", lambda d, data_root=None: focus)
    monkeypatch.setattr(ss, "board_core_codes",
                        lambda d, b: [{"code": "002463", "name": "沪电股份", "role": ss.ROLE_CORE}])
    # council:龙头涨停(无红旗但涨停不可买由形态判)/中军干净/跟涨干净
    council = {
        "605058": {"综合分": -0.2, "综合方向": "看空", "财报红旗数": 0, "龙虎榜否决": False},
        "002463": {"综合分": 0.1, "综合方向": "中性", "财报红旗数": 0, "龙虎榜否决": False},
        "688802": {"综合分": 0.0, "综合方向": "中性", "财报红旗数": 0, "龙虎榜否决": False},
    }
    monkeypatch.setattr(ss, "run_council", lambda codes, as_of: council)
    klines = {
        "605058": _mk_kline(n=80, last_pct=0.099, base=50.0),           # 涨停不可买
        "002463": _mk_kline(n=120, last_pct=0.01, uptrend=0.2),         # 健康回踩
        "688802": _mk_kline(n=120, last_pct=0.01, uptrend=0.2),
    }
    import tools.backtest.screen_forward_common as sfc
    monkeypatch.setattr(sfc, "load_klines", lambda codes, min_bars: klines)
    return date, focus


def test_synthesize_end2end(monkeypatch, tmp_path):
    date, _ = _fake_synthesize(monkeypatch, tmp_path)
    result = ss.synthesize(date, data_root=tmp_path, client=_FakeClient())
    assert result["date"] == date and result["板块"]
    stocks = {s["code"]: s for b in result["板块"] for s in b["个股"]}
    # ② 涨停不可买龙头 → 剔除(即便 LLM 给推荐 9 分)
    assert stocks["605058"]["档"] == "剔除"
    # ③ 跟涨 → 观察(不进主选)
    assert stocks["688802"]["档"] == "观察"
    # 中军健康 → 保留 LLM 推荐
    assert stocks["002463"]["档"] == "推荐"
    # ① json 结构 + md 段落完整
    md = ss.render_md(result)
    for seg in ["整体盘面", "板块消息面评价", "三维合成", "反选剔除", "规避策略"]:
        assert seg in md
    for code in stocks:
        assert code in md


# ──────────────── ④ 非 gating:不改上游产物字节 ────────────────
def test_non_gating_不改上游(monkeypatch, tmp_path):
    date, focus = _fake_synthesize(monkeypatch, tmp_path)
    import copy
    snapshot = copy.deepcopy(focus)
    ss.synthesize(date, data_root=tmp_path, client=_FakeClient())
    assert focus == snapshot        # 合成不得原地修改读入的 sector_focus(消息驱动块)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
