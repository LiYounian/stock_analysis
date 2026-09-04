"""形态选股编排单测(mock 采集层,不触网)。

锁语义:
  · 基准沪深300 → 逐票 RS → 硬规则 AND → 达标占比 → 落 view「形态选股」。
  · 双层:板块基准=同业成分等权均值;个股收益 > 同业均值 且 同业均值 > 沪深300 才 RS 达标。
  · 成分缺失 → 全体降级单层;某行业样本 < 板块最小样本 → 该行业逐票降级单层。
"""
import pandas as pd
import pytest

from tools.collectors import announcement, board, fundamental, index, market
from tools.pipeline import screen_pattern as sp
from tools.store import repo as store


def _breakout_df(last=108.0):
    """箱体放量突破;末根收盘 last 决定 20 日收益(越高收益越大)。

    箱体.窗口=30 → 需 30 根箱体 + 1 根突破;箱体.突破幅度%=3 → last 需 >箱顶 3%(>~105.6)。
    """
    base = [100 + (2 if i % 2 else -2) for i in range(30)]
    closes = base + [last]
    vols = [1000.0] * 30 + [2500.0]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=31, freq="D"),
        "open": closes, "high": [c * 1.005 for c in closes],
        "low": [c * 0.995 for c in closes], "close": closes, "volume": vols,
    })


def _flat_df():
    flat = [100 + (0.5 if i % 2 else -0.5) for i in range(21)]
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=21, freq="D"),
        "open": flat, "high": flat, "low": flat, "close": flat,
        "volume": [1000.0] * 21})


def _bench_df():
    """沪深300 基准:平盘(20 日收益≈0)。"""
    return pd.DataFrame({"date": pd.date_range("2024-01-01", periods=21, freq="D"),
                         "close": [100.0] * 21})


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(index, "load_index", lambda code: _bench_df())
    # 默认注入"干净的正向确认"(净利增速>0),使 RS/板块/达标占比 类测试与正向确认解耦;
    # 针对护栏/正向确认的测试各自 override 下面两个。
    monkeypatch.setattr(fundamental, "load_fundamental",
                        lambda code: {"净利增速": 10.0, "PE分位": 0.5})
    monkeypatch.setattr(announcement, "load_announcements", lambda code: [])


def test_single_layer_fallback_when_no_membership(monkeypatch, tmp_path):
    """成分映射缺失(RAW 隔离到空 tmp)→ 全体降级单层;突破票达标。"""
    _isolate(monkeypatch, tmp_path)
    klines = {"AAA": _breakout_df(), "BBB": _flat_df()}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    view = sp.run_pattern_screen(["AAA", "BBB"], as_of="2024-06-01", fetch=False)
    assert view["RS模式"].startswith("单层")
    assert view["达标数"] == 1 and [x["code"] for x in view["达标清单"]] == ["AAA"]
    assert store.get_view("形态选股", date="2024-06-01")["达标占比"] == 0.5


def test_two_layer_uses_board_mean(monkeypatch, tmp_path):
    """双层:同业(3 只达最小样本)等权均值当板块基准;跑输同业均值的票 RS 不达标。"""
    _isolate(monkeypatch, tmp_path)
    # 同一行业 3 只,均为突破形态,20 日收益不同 → 均值≈10.2%
    klines = {"HI": _breakout_df(112.0), "MID": _breakout_df(108.0), "LO": _breakout_df(106.0)}
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    monkeypatch.setattr(board, "load_membership",
                        lambda: {"HI": "计算机", "MID": "计算机", "LO": "计算机"})
    view = sp.run_pattern_screen(["HI", "MID", "LO"], as_of="2024-06-01", fetch=False)
    assert view["RS模式"].startswith("双层") and view["板块数"] == 1
    hit = {x["code"] for x in view["达标清单"]}
    assert "LO" not in hit                      # 跑输同业均值 → 个股vs板块 RS<0 → 出局
    assert "HI" in hit                          # 跑赢同业均值且板块跑赢沪深300 → 达标
    assert view["单层降级票数"] == 0


def test_thin_board_degrades_to_single_layer(monkeypatch, tmp_path):
    """行业成分数 < 板块最小样本(默认 3)→ 该行业逐票降级单层。"""
    _isolate(monkeypatch, tmp_path)
    klines = {"HI": _breakout_df(112.0), "LO": _breakout_df(106.0)}   # 同行业仅 2 只 < 3
    monkeypatch.setattr(market, "load_kline", lambda code: klines[code])
    monkeypatch.setattr(board, "load_membership", lambda: {"HI": "计算机", "LO": "计算机"})
    view = sp.run_pattern_screen(["HI", "LO"], as_of="2024-06-01", fetch=False)
    assert view["板块数"] == 0 and view["单层降级票数"] == 2
    # 降级单层后两只(个股 vs 沪深300 平盘)收益均为正 → 都达标
    assert view["达标数"] == 2


def test_skips_insufficient_kline(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market, "load_kline", lambda code: _breakout_df().head(5))
    view = sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)
    assert view["跳过数"] == 1 and view["有效样本"] == 0 and view["达标占比"] == 0.0


# ---------- 批次A:护栏接线（对照组见 test_single_layer_fallback：护栏缺数据→AAA 达标）----------
def _guard(monkeypatch, tmp_path, fund, anns):
    """隔离 + 单层(无成分) + 突破票 AAA;注入指定基本面/公告。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market, "load_kline", lambda code: _breakout_df())
    monkeypatch.setattr(fundamental, "load_fundamental", lambda code: fund)
    monkeypatch.setattr(announcement, "load_announcements", lambda code: anns)
    return sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)


def test_guardrail_rejects_negative_growth(monkeypatch, tmp_path):
    v = _guard(monkeypatch, tmp_path, {"净利增速": -5.0, "PE分位": 0.5}, [])
    assert v["达标数"] == 0                      # 形态+RS 过,净利增速<0 被护栏剔除
    assert v["护栏覆盖"] == "1/1"


def test_guardrail_rejects_extreme_pe(monkeypatch, tmp_path):
    v = _guard(monkeypatch, tmp_path, {"净利增速": 10.0, "PE分位": 0.97}, [])
    assert v["达标数"] == 0                      # PE 分位 >0.90 被剔除


def test_guardrail_reason_labels_pe_window():
    """#32:PE 极端剔除原因必须标出分位的历史窗口口径(供解释力可见);无窗口则不标。"""
    from tools.analysis.pattern_screener import screen as ps
    _, r_win = ps.guardrail(0.97, 10.0, pe_window="全部")
    assert any("PE分位" in x and "全部窗口" in x for x in r_win)
    _, r_none = ps.guardrail(0.97, 10.0)
    assert any("PE分位" in x for x in r_none) and all("窗口" not in x for x in r_none)


def test_guardrail_rejects_regulatory_announcement(monkeypatch, tmp_path):
    v = _guard(monkeypatch, tmp_path, {"净利增速": 10.0, "PE分位": 0.5},
               [{"title": "关于收到中国证监会立案告知书的公告"}])
    assert v["达标数"] == 0                      # 合规风险关键词"立案"被剔除


def test_guardrail_clean_passes(monkeypatch, tmp_path):
    v = _guard(monkeypatch, tmp_path, {"净利增速": 12.0, "PE分位": 0.4},
               [{"title": "关于回购公司股份的公告"}])
    assert v["达标数"] == 1 and v["护栏覆盖"] == "1/1"   # 干净票不被误杀


# ---------- 批次A:正向确认纪律（突破不裸用，须叠加基本面或事件）----------
def test_no_positive_confirm_rejected(monkeypatch, tmp_path):
    """形态+RS+量能+过护栏,但无正向确认(净利增速缺 + 无正向事件)→ 不计入达标。"""
    v = _guard(monkeypatch, tmp_path, {"净利增速": None, "PE分位": 0.5}, [])
    assert v["达标数"] == 0
    assert "突破不裸用" in v["纪律"]


def test_event_alone_confirms(monkeypatch, tmp_path):
    """仅事件(增持公告,无基本面)也构成正向确认 → 可达标。"""
    v = _guard(monkeypatch, tmp_path, {"净利增速": None, "PE分位": 0.5},
               [{"title": "关于控股股东增持公司股份的公告"}])
    assert v["达标数"] == 1
    assert v["达标清单"][0]["正向确认依据"]                # 依据非空(事件)


# ---------- 达标清单「行业」字段(供选股页 region② 按板块分组)----------
def test_sector_helper_maps_zjh_to_sw():
    m = {"A": "C39计算机、通信和其他电子设备制造业", "B": "某不存在门类xyz"}
    assert sp._sector("A", m) == "电子"           # 证监会 C39 → 申万一级 电子
    assert sp._sector("B", m) == "某不存在门类xyz"  # 对齐不上→回退证监会名
    assert sp._sector("C", m) == "未分类"          # 不在映射→未分类


def test_达标清单_carries_sector(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market, "load_kline", lambda c: _breakout_df())
    monkeypatch.setattr(board, "load_membership",
                        lambda: {"AAA": "C39计算机、通信和其他电子设备制造业"})
    v = sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)
    assert v["达标清单"][0]["code"] == "AAA" and v["达标清单"][0]["行业"] == "电子"


def test_达标清单_sector_未分类_when_no_membership(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(market, "load_kline", lambda c: _breakout_df())
    monkeypatch.setattr(board, "load_membership", lambda: {})     # 空映射
    v = sp.run_pattern_screen(["AAA"], as_of="2024-06-01", fetch=False)
    assert v["达标清单"][0]["行业"] == "未分类"
