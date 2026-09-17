"""强势不回踩旁路 · 语义锁测试(纯离线·不触网/不烧 LLM)。

锁死(对应 docs/计划/2026-09-17_强势不回踩旁路_设计与回测方案.md):
  ① §2 够格判据 8 条 AND:每条单独破坏都令 bypass_eligible → None(不放行)。
  ② §2 完整够格样例 → 放行(返回非空说明)。
  ③ §3.1 通用硬闸不被旁路绕过:极高位/涨停/财报/龙虎命中 → maybe_route_bypass 不路由。
  ④ §3.2 旁路专属位置闸(pos60≥0.90)命中 → 不路由(严于通用 0.95 veto)。
  ⑤ 入场方式 "突破新高确认" 回填:挂单可>现价(受控追)但恒 ≤ cap;单调性 止损<挂单≤红线。
  ⑥ cap 硬顶:D日high 远高时 entry 被 cap 夹住,不裸追;近涨停线也夹 cap。
  ⑦ 回踩通道铁律不受影响:三种回踩方式 entry 仍 = min(entry, 现价)(旁路改动不污染回踩)。
  ⑧ 默认不启用:synthesize_group enable_bypass 缺省 False → 不覆盖入场方式(live 行为不变)。
  ⑨ G3 跟涨角色不走旁路;退化口径 require_mainline=False 可跳过 G2。
⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from tools.analysis import selection_synth as ss


# ──────────────── 够格样例工厂(可控·逐条可破坏) ────────────────
def _form_ok() -> dict:
    """一个恰好够格的形态(创业板 300 票):多头/温和量/低位起步/离顶有空间/当日温和/非涨停。"""
    return {
        "均线多头": True, "量比": 1.6, "位置pos60": 0.62, "距60高": -0.15,
        "当日涨跌": 0.04, "涨停不可买": False,
        "ma5": 10.0, "ma10": 9.8, "ma20": 9.5, "现价": 11.0, "前低": 9.0,
        "当日high": 11.3, "当日low": 10.6,
    }


def _cand_ok() -> dict:
    return {"code": "300502", "role": ss.ROLE_LEADER, "board": "光模块",
            "board_ctx": {"强弱": "强"}}


# ──────────────── ② 完整够格 → 放行 ────────────────
def test_bypass_eligible_完整够格放行():
    r = ss.bypass_eligible(_cand_ok(), _form_ok())
    assert r and "旁路够格" in r


# ──────────────── ① 8 条判据逐条破坏 → 不放行 ────────────────
def test_G1_均线非多头_不放行():
    f = _form_ok(); f["均线多头"] = False
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G2_板块非强_不放行():
    c = _cand_ok(); c["board_ctx"] = {"强弱": "中"}
    assert ss.bypass_eligible(c, _form_ok()) is None


def test_G2_无板块_不放行():
    c = _cand_ok(); c["board"] = None; c["board_ctx"] = {}
    assert ss.bypass_eligible(c, _form_ok()) is None


def test_G3_跟涨角色_不放行():
    c = _cand_ok(); c["role"] = ss.ROLE_FOLLOW
    assert ss.bypass_eligible(c, _form_ok()) is None


def test_G4_爆量_不放行():
    f = _form_ok(); f["量比"] = 3.0        # >2.5 爆量
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G4_缩量_不放行():
    f = _form_ok(); f["量比"] = 0.8        # <1.0 缩量
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G5_pos60过高_不放行():
    f = _form_ok(); f["位置pos60"] = 0.88   # >0.85 已偏高位
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G5_pos60过低_不放行():
    f = _form_ok(); f["位置pos60"] = 0.40   # <0.50 尚未启动
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G6_距60高不足_不放行():
    f = _form_ok(); f["距60高"] = -0.03     # 仅 3% 空间(<8%),贴顶
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G7_当日暴涨近涨停_不放行():
    f = _form_ok(); f["当日涨跌"] = 0.19    # 近 20cm 涨停
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G7_当日绿盘_不放行():
    f = _form_ok(); f["当日涨跌"] = -0.01   # 收绿
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_G8_涨停不可买_不放行():
    f = _form_ok(); f["涨停不可买"] = True
    assert ss.bypass_eligible(_cand_ok(), f) is None


def test_数据不足_不放行():
    assert ss.bypass_eligible(_cand_ok(), {"数据不足": True}) is None


# ──────────────── ⑨ 退化口径:require_mainline=False 跳过 G2 ────────────────
def test_退化口径_跳过主线约束():
    c = _cand_ok(); c["board"] = None; c["board_ctx"] = {}
    assert ss.bypass_eligible(c, _form_ok(), require_mainline=False)   # 无板块仍放行(纯个股强势)


# ──────────────── ③ 通用硬闸不被旁路绕过 ────────────────
def test_旁路不绕过_极高位veto():
    f = _form_ok()
    f.update({"位置pos60": 0.96, "距60高": -0.01})   # 极高位+贴高 → 通用 hard_veto 命中
    st = {"code": "300502", "入场方式": "回踩MA5", "council": {}}
    # 注:此形态本就 G5/G6 不够格,双保险;直接测 maybe_route 也不路由
    assert ss.maybe_route_bypass(st, _cand_ok(), f) is None
    assert st["入场方式"] == "回踩MA5"                 # 未被覆盖


def test_旁路不绕过_财报红旗():
    st = {"code": "300502", "入场方式": "回踩MA5", "council": {"财报红旗数": 1}}
    assert ss.maybe_route_bypass(st, _cand_ok(), _form_ok()) is None
    assert st["入场方式"] == "回踩MA5"


def test_旁路不绕过_龙虎否决():
    st = {"code": "300502", "入场方式": "回踩MA5", "council": {"龙虎榜否决": True}}
    assert ss.maybe_route_bypass(st, _cand_ok(), _form_ok()) is None


# ──────────────── ④ §3.2 旁路专属位置闸(严于通用) ────────────────
def test_旁路专属位置闸_pos60_0p92():
    # pos60=0.92:通用 veto(0.95)不命中,但旁路专属闸(0.90)命中 → 不路由
    f = _form_ok(); f.update({"位置pos60": 0.92, "距60高": -0.15})
    assert ss.bypass_hard_gate(f)                     # 专属闸命中
    st = {"code": "300502", "入场方式": "回踩MA5", "council": {}}
    assert ss.maybe_route_bypass(st, _cand_ok(), f) is None


# ──────────────── ⑤⑥ 入场回填:挂单可>现价但≤cap;单调性 ────────────────
def test_旁路回填_挂单大于现价但不越cap_单调():
    st = {"code": "300502", "入场方式": ss._旁路入场方式, "council": {}}
    ss.fill_entry_exit(st, _form_ok())
    entry, stop, cap = st["挂单价"], st["止损价"], st["不追高上限"]
    assert entry is not None and stop is not None and cap is not None
    assert entry >= _form_ok()["现价"]                 # 受控追:允许挂到现价之上
    assert entry <= cap                                # 但恒不越 cap
    assert stop < entry <= cap                         # 单调性铁律


def test_旁路回填_cap夹住裸追():
    # 当日high 远高于现价(如一字大长腿):cap = min(high×1.03, 近涨停线) 夹住,entry 不裸追到 high
    f = _form_ok()
    f["当日high"] = 20.0                               # 现价 11、high 20(极端)
    st = {"code": "300502", "入场方式": ss._旁路入场方式, "council": {}}
    ss.fill_entry_exit(st, f)
    # 创业板 20cm:近涨停线 = 11×(1+0.20-0.02)=12.98;cap 取 min(20×1.03, 12.98)=12.98
    assert abs(st["不追高上限"] - 11.0 * (1 + 0.20 - 0.02)) < 1e-6
    assert st["挂单价"] <= st["不追高上限"]              # entry 被 cap 夹住,不追到 20


def test_旁路回填_数据不足不编():
    st = {"code": "300502", "入场方式": ss._旁路入场方式, "council": {}}
    ss.fill_entry_exit(st, {"现价": 11.0})              # 缺当日high
    assert st["挂单价"] is None and "人工确认" in st.get("价位说明", "")


# ──────────────── ⑦ 回踩通道铁律不受污染 ────────────────
def test_回踩铁律不受污染_entry不超现价():
    # 回踩MA5 在现价跌破均线(现价 < ma5)时,entry=min(ma5,现价)=现价,绝不挂现价之上
    f = {"ma5": 12.0, "ma20": 11.0, "前低": 10.0, "现价": 11.5,
         "当日high": 11.8, "当日low": 11.2}
    st = {"code": "300502", "入场方式": "回踩MA5"}
    ss.fill_entry_exit(st, f)
    assert st["挂单价"] <= f["现价"]                    # 回踩铁律仍生效(不追)


# ──────────────── ⑧ 默认不启用:enable_bypass 缺省 False ────────────────
def test_默认不启用_入场方式不被覆盖(monkeypatch):
    # synthesize_group 默认 enable_bypass=False → 即便够格也不路由旁路(live 行为不变)
    captured = {}

    class _FakeClient:
        def extract(self, text, schema, instruction=""):
            return {"个股": [{"code": "300502", "name": "新易盛", "档": "推荐",
                            "建议分": 8.0, "理由": "策略:多头;板块:光模块强",
                            "入场方式": "回踩MA5", "风险": "追高"}],
                    "规避提示": "无"}

    cand = _cand_ok()
    council_map = {"300502": {"综合分": 0.5, "综合方向": "看多", "财报红旗数": 0,
                              "龙虎榜否决": False}}
    form_map = {"300502": _form_ok()}
    out = ss.synthesize_group("光模块", {"强弱": "强"}, [cand], council_map, form_map,
                              {"风险偏好": "中性"}, board_tag="利好",
                              client=_FakeClient())          # enable_bypass 缺省 False
    s = out["个股"][0]
    assert s["入场方式"] == "回踩MA5"                    # 未被旁路覆盖
    assert "旁路命中" not in s


def test_显式启用_够格票被路由():
    class _FakeClient:
        def extract(self, text, schema, instruction=""):
            return {"个股": [{"code": "300502", "name": "新易盛", "档": "推荐",
                            "建议分": 8.0, "理由": "策略:多头;板块:光模块强",
                            "入场方式": "回踩MA5", "风险": "追高"}],
                    "规避提示": "无"}

    council_map = {"300502": {"综合分": 0.5, "综合方向": "看多", "财报红旗数": 0,
                              "龙虎榜否决": False}}
    form_map = {"300502": _form_ok()}
    out = ss.synthesize_group("光模块", {"强弱": "强"}, [_cand_ok()], council_map, form_map,
                              {"风险偏好": "中性"}, board_tag="利好",
                              client=_FakeClient(), enable_bypass=True)
    s = out["个股"][0]
    assert s["入场方式"] == ss._旁路入场方式             # 够格 → 被路由旁路
    assert s.get("旁路命中")
    assert s["挂单价"] >= form_map["300502"]["现价"]     # 受控追高


# ──────────────── ⑩ 回测向量化形态 == pattern_metrics(防漂移·单一真源) ────────────────
def test_回测向量化form_对齐pattern_metrics():
    """backtest_strong_bypass.precompute_bypass(末根)须与 pattern_metrics 同语义(容 pm 的四舍五入)。"""
    from tools.backtest import backtest_strong_bypass as B
    rng = np.random.default_rng(3)
    n = 90
    close = np.abs(10 + np.cumsum(rng.normal(0.05, 0.3, n))) + 5
    high = close * (1 + np.abs(rng.normal(0.01, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0.01, 0.01, n)))
    vol = np.abs(rng.normal(1e6, 2e5, n))
    df = pd.DataFrame({
        "date": pd.bdate_range("2026-01-01", periods=n), "open": close * 0.999,
        "high": high, "low": low, "close": close, "volume": vol,
        "amount": close * vol, "turnover": np.full(n, 2.0),
    })
    F = B.precompute_bypass(df)
    fa = B._form_at(F, n - 1)
    pm = ss.pattern_metrics(df, "600000", date=df["date"].iloc[-1].strftime("%Y-%m-%d"))
    assert fa["均线多头"] == pm["均线多头"]
    for k, nd in [("量比", 2), ("位置pos60", 4), ("距60高", 4), ("当日涨跌", 4),
                  ("ma5", 3), ("ma10", 3), ("ma20", 3), ("现价", 3),
                  ("当日high", 3), ("当日low", 3)]:
        assert abs(round(fa[k], nd) - pm[k]) < 10 ** (-nd) + 1e-9, f"{k} 漂移:{fa[k]} vs {pm[k]}"
