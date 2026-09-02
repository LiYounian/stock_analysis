"""个股两融采集 + 「主力净流入 vs 融资买入」背离甄别 单测(mock 网络,不触网)。

锁的语义:
- 采集:沪深北三所归一到 {code,date,融资买入额,融资余额,融券余量,market};盘后披露标记;
  按票落盘 + 按 date 去重幂等 + 前向增量并集;单所降级不中断整批。
- 防未来函数(红线):summarize_asof 只取 date≤as_of 的最新一日;未来日被切掉。
- 背离判据(纯函数):net 大额正值 且 融资买入/net ≥ 阈值 → 命中;net≤0 / 无两融 / 缺融资买入 → 不命中。
- kill-switch:启用=False → enabled()=False、divergence_asof 未命中(全链路 no-op)。
- expert_资金流 接入:命中→降级(强度打折)/ 翻风险(方向翻中性);缺两融数据→现状不回归;
  看空/中性信号永不被甄别改动。
"""
import pandas as pd
import pytest

from tools.analysis import experts, margin_divergence as md
from tools.collectors import margin
from tools.store import repo as store


# ———————————————————————————— akshare mock(三所明细)————————————————————————————
def _sse_df(date="20240105"):
    return pd.DataFrame([
        {"信用交易日期": date, "标的证券代码": "600000", "标的证券简称": "浦发",
         "融资余额": 1e10, "融资买入额": 8e8, "融资偿还额": 7e8,
         "融券余量": 1000, "融券卖出量": 100, "融券偿还量": 50},
    ])


def _szse_df():
    return pd.DataFrame([
        {"证券代码": "300502", "证券简称": "新易盛", "融资买入额": 16e8, "融资余额": 5e9,
         "融券卖出量": 0, "融券余量": 0, "融券余额": 0, "融资融券余额": 5e9},
        {"证券代码": "000001", "证券简称": "平安银行", "融资买入额": 3e8, "融资余额": 5e9,
         "融券卖出量": 200, "融券余量": 5000, "融券余额": 0, "融资融券余额": 5e9},
    ])


def _bse_df():
    return pd.DataFrame([
        {"证券代码": "430017", "证券简称": "星昊", "融资买入额": 1e6, "融资余额": 3e7,
         "融券卖出量": 0, "融券余量": 0, "融券余额": 0, "融资融券余额": 3e7},
    ])


def _mock_ak(monkeypatch, sse=None, szse=None, bse=None):
    import akshare as ak
    monkeypatch.setattr(ak, "stock_margin_detail_sse",
                        lambda date: sse if sse is not None else _sse_df(date))
    monkeypatch.setattr(ak, "stock_margin_detail_szse",
                        lambda date: szse if szse is not None else _szse_df())
    monkeypatch.setattr(ak, "stock_margin_detail_bse",
                        lambda date: bse if bse is not None else _bse_df())


# ———————————————————————————— 采集:归一 / 契约 ————————————————————————————
def test_norm_record_contract():
    rec = margin._norm_record("300502", "20240105", 16e8, 5e9, rq_vol=0, market="SZSE")
    assert rec["code"] == "300502" and rec["date"] == "2024-01-05"
    assert rec["融资买入额"] == 16e8 and rec["融资余额"] == 5e9
    assert rec["visible_after_close"] is True          # 盘后披露(防未来函数)标记
    assert margin._norm_record("", "20240105", 1, 1) is None
    assert margin._norm_record("abc", "20240105", 1, 1) is None
    assert margin._norm_record("300502", "20240105", None, None) is None  # 两值皆缺→无效


def test_fetch_detail_merges_three_exchanges(monkeypatch):
    _mock_ak(monkeypatch)
    df = margin.fetch_detail_by_date("2024-01-05")
    assert set(df["code"]) == {"600000", "300502", "000001", "430017"}
    assert set(df["market"]) == {"SSE", "SZSE", "BSE"}


def test_fetch_detail_degrades_per_exchange(monkeypatch):
    """单交易所抛错 → 跳过该所,其余照常(优雅降级,不抛)。"""
    import akshare as ak
    _mock_ak(monkeypatch)
    monkeypatch.setattr(ak, "stock_margin_detail_szse",
                        lambda date: (_ for _ in ()).throw(ConnectionError("限流")))
    df = margin.fetch_detail_by_date("2024-01-05")
    assert set(df["market"]) == {"SSE", "BSE"}          # 深所降级,沪北仍在


# ———————————————————————————— 采集:落盘 / 幂等 / 增量 / as-of ————————————————————————————
def test_fetch_persists_and_incremental(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(margin, "_daterange_days", lambda s, e: ["2024-01-05"])
    _mock_ak(monkeypatch)
    out = margin.fetch_margin("20240101", "20240110")
    assert "300502" in out and out["300502"][0]["融资买入额"] == 16e8
    assert store.get_raw_meta("margin", "300502")["source"] == "akshare"

    # 增量:新一日并入,旧日保留;同日重跑去重(幂等)
    monkeypatch.setattr(margin, "_daterange_days", lambda s, e: ["2024-01-08"])
    monkeypatch.setattr(margin, "fetch_detail_by_date",
                        lambda d: pd.DataFrame([margin._norm_record(
                            "300502", "20240108", 20e8, 6e9, market="SZSE")]))
    out2 = margin.fetch_margin("20240106", "20240110")
    dates = {r["date"] for r in out2["300502"]}
    assert dates == {"2024-01-05", "2024-01-08"}        # 前向增量并集


def test_summarize_asof_no_future(monkeypatch):
    """防未来函数:只取 date≤as_of 的最新一日。"""
    recs = [
        {"code": "300502", "date": "2024-01-08", "融资买入额": 20e8, "融资余额": 6e9},
        {"code": "300502", "date": "2024-01-05", "融资买入额": 16e8, "融资余额": 5e9},
    ]
    assert margin.summarize_asof(recs, "2024-01-05")["融资买入额"] == 16e8   # 未来日08被切
    assert margin.summarize_asof(recs, "2024-01-08")["融资买入额"] == 20e8
    assert margin.summarize_asof(recs, "2024-01-04") is None                 # 全在未来→None
    assert margin.summarize_asof([], "2024-01-08") is None


# ———————————————————————————— 背离判据(纯函数)————————————————————————————
_CFG_ON = {"启用": True, "最小主力净流入_元": 5e7, "融资解释比阈值": 0.5,
           "动作": "降级", "降级系数": 0.3,
           "融资占成交额_高档": 0.15, "融资余额占流通市值_高档": 0.04}


def test_verdict_hit_xinyisheng():
    """样本外靶子:新易盛 20 亿主力净流入 / 16 亿融资买入 = 80% ≥ 50% → 命中降级。"""
    v = md.divergence_verdict(20e8, {"融资买入额": 16e8, "融资余额": 5e9}, c=_CFG_ON)
    assert v["命中"] is True and v["动作"] == "降级"
    assert abs(v["融资解释比"] - 0.8) < 1e-6
    assert v["强度系数"] == 0.3
    assert any("疑似融资盘" in s for s in v["依据"])


def test_verdict_no_hit_low_ratio():
    """融资买入只占主力净流入 25% < 50% → 不命中(真主力吸筹,不误压制)。"""
    v = md.divergence_verdict(20e8, {"融资买入额": 5e8, "融资余额": 5e9}, c=_CFG_ON)
    assert v["命中"] is False and v["强度系数"] == 1.0


def test_verdict_no_hit_small_net():
    """主力净流入低于门槛(小额)→ 不表态(滤噪声)。"""
    v = md.divergence_verdict(3e7, {"融资买入额": 3e7}, c=_CFG_ON)
    assert v["命中"] is False


def test_verdict_no_hit_net_nonpositive_or_missing():
    assert md.divergence_verdict(-20e8, {"融资买入额": 16e8}, c=_CFG_ON)["命中"] is False
    assert md.divergence_verdict(20e8, None, c=_CFG_ON)["命中"] is False
    assert md.divergence_verdict(20e8, {"融资余额": 5e9}, c=_CFG_ON)["命中"] is False  # 缺融资买入


def test_verdict_killswitch_off():
    """kill-switch:启用=False → 应用=False、恒不命中(no-op)。"""
    cfg_off = dict(_CFG_ON, 启用=False)
    v = md.divergence_verdict(20e8, {"融资买入额": 16e8}, c=cfg_off)
    assert v["应用"] is False and v["命中"] is False and v["强度系数"] == 1.0


def test_verdict_flip_risk_mode():
    v = md.divergence_verdict(20e8, {"融资买入额": 16e8}, c=dict(_CFG_ON, 动作="翻风险"))
    assert v["命中"] is True and v["动作"] == "翻风险" and v["强度系数"] == 0.0


def test_verdict_high_margin_info_ratios():
    """信息档位:融资占成交额/流通市值达高档 → 高两融占比=True(仅标注)。"""
    v = md.divergence_verdict(20e8, {"融资买入额": 16e8, "融资余额": 5e9},
                              turnover=100e8, float_mv=100e9, c=_CFG_ON)
    assert v["融资占成交额"] == pytest.approx(0.16)
    assert v["融资余额占流通市值"] == pytest.approx(0.05)
    assert v["高两融占比"] is True


# ———————————————————————————— expert_资金流 接入 ————————————————————————————
def _record(code="300502", net=20e8, streak=3, as_of="2024-01-08"):
    return {"meta": {"code": code, "as_of": as_of},
            "fundflow": {"今日主力净流入": net, "主力连续净流入天数": streak}}


def _install_margin(monkeypatch, tmp_path, records):
    """把某票两融序列写进临时 store,供 expert as-of 读取。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    for code, recs in records.items():
        store.put_raw("margin", code, recs, meta={"source": "akshare"})


def test_expert_downgrade_on_hit(monkeypatch, tmp_path):
    """命中背离 → 看多强度打折(连续3天本应顶格1.0 → 0.3)。"""
    _install_margin(monkeypatch, tmp_path,
                    {"300502": [{"code": "300502", "date": "2024-01-08",
                                 "融资买入额": 16e8, "融资余额": 5e9}]})
    v = experts.expert_资金流(_record())
    assert v.方向 == "看多" and abs(v.强度 - 0.3) < 1e-6
    assert "融资盘背离" in v.原始
    assert any("疑似融资盘" in d for d in v.依据)


def test_expert_noop_when_no_margin_data(monkeypatch, tmp_path):
    """无两融数据 → 现状不回归(连续3天顶格看多 1.0)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)     # 空 store
    v = experts.expert_资金流(_record())
    assert v.方向 == "看多" and abs(v.强度 - 1.0) < 1e-6
    assert "融资盘背离" not in v.原始


def test_expert_killswitch_noop(monkeypatch, tmp_path):
    """kill-switch 关 → 即便有两融数据也现状(顶格看多)。"""
    _install_margin(monkeypatch, tmp_path,
                    {"300502": [{"code": "300502", "date": "2024-01-08",
                                 "融资买入额": 16e8, "融资余额": 5e9}]})
    monkeypatch.setattr(md, "cfg", lambda: dict(_CFG_ON, 启用=False))
    v = experts.expert_资金流(_record())
    assert abs(v.强度 - 1.0) < 1e-6 and "融资盘背离" not in v.原始


def test_expert_flip_risk(monkeypatch, tmp_path):
    _install_margin(monkeypatch, tmp_path,
                    {"300502": [{"code": "300502", "date": "2024-01-08",
                                 "融资买入额": 16e8, "融资余额": 5e9}]})
    monkeypatch.setattr(md, "cfg", lambda: dict(_CFG_ON, 动作="翻风险"))
    v = experts.expert_资金流(_record())
    assert v.方向 == "中性" and v.强度 == 0.0


def test_expert_sell_signal_untouched(monkeypatch, tmp_path):
    """看空信号(主力净流出)永不被甄别改动。"""
    _install_margin(monkeypatch, tmp_path,
                    {"300502": [{"code": "300502", "date": "2024-01-08",
                                 "融资买入额": 16e8, "融资余额": 5e9}]})
    v = experts.expert_资金流(_record(net=-20e8, streak=0))
    assert v.方向 == "看空" and v.强度 == -0.5 and "融资盘背离" not in v.原始


def test_expert_asof_no_future(monkeypatch, tmp_path):
    """防未来函数:as_of 早于两融披露日 → 取不到数据 → 不命中(现状看多)。"""
    _install_margin(monkeypatch, tmp_path,
                    {"300502": [{"code": "300502", "date": "2024-01-08",
                                 "融资买入额": 16e8, "融资余额": 5e9}]})
    v = experts.expert_资金流(_record(as_of="2024-01-05"))   # 08 的两融在 05 不可见
    assert abs(v.强度 - 1.0) < 1e-6 and "融资盘背离" not in v.原始
