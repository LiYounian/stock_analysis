"""口径日期与新鲜度单测:「会过期的块必须自证它是哪天的口径」。

**为什么有这一整个文件**(重写/精简本模块前请先读完,这些断言锁的是语义、不是实现细节):

2026-09-03 实证到两起同类高危静默失真 —— 产出**不自证口径日期**,于是过期数据被当成"今日":

  ① `fundflow` 块里没有任何日期字段。源采集失败时 store 会黑盒回退到更早的分区,某票因此
     把 **13 天前**的缓存原样写成「今日主力净流入」,而那天的真实值**符号相反**(旧缓存净流出、
     当日实为大幅净流入)。下游分析直读 record,**看不出**这是旧数据 → 直接误导决策。

  ② `valuation`/`fundamental` 的 报告期 可能整体滞后一个报告期(半年报已披露、块里还是一季报)。
     实测某票换成半年报后 PE_TTM 从 39.79 跳到 96.95(+144%)、净利率由 +3.08% 翻成 −0.37%。
     而 `provenance` 当时只记 `{"fundamental": true}` 这样的**布尔值**、不记口径日期 →
     滞后完全静默。

修法的三条不变量(下面每个测试各锁一条,别删):
  1. **保留数据 + 标明它是哪天的 + 标明是陈旧**。不静默沿用(那是①的病);也不一律清空
     (清空会丢掉"有旧数据可参考"这一信息)。
  2. 三态**只有一套** —— contracts.record.ENUMS["新鲜度"](= event.FRESH/STALE/NODATA)。
     「无数据」与「陈旧」严格区分:前者什么都没有,后者有值只是旧的,下游处置不同。
  3. `provenance` 的布尔位按**该维实际有无可用数据**判,且**类型不变**(大量既有代码/测试
     读它的 True/False)。新信息只挂在 `provenance.口径` 下。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.analysis import panel, serialize as sz
from tools.contracts import record as rc
from tools.store import repo as store

AS_OF = "2026-09-03"
STALE_DAY = "2026-08-19"          # as_of 前 13 天(问题①的真实间距)
CODE = "603161"


# ————————————————————————————————————————————————
# fixtures:把 raw 根切到 tmp_path,并把非目标数据块统一降级为 None(hermetic)
# ————————————————————————————————————————————————
@pytest.fixture
def only_fundflow(monkeypatch, tmp_path):
    """只让 fundflow 这条链路真跑(读 tmp_path 下的真 parquet),其余块降级 None。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    return tmp_path


def _flow_df(last_day: str, n: int = 6, 主力: float = -702392.0) -> pd.DataFrame:
    """造一段资金流日序列,**最后一根 bar 落在 last_day**(口径日期的真源)。"""
    dates = pd.bdate_range(end=pd.Timestamp(last_day), periods=n)
    return pd.DataFrame({
        "date": dates,
        "主力净流入": [1e6] * (n - 1) + [主力],
        "小单净流入": [0.0] * n, "中单净流入": [0.0] * n,
        "大单净流入": [0.0] * n, "超大单净流入": [0.0] * n,
        "主力净占比": [1.0] * (n - 1) + [-1.2],
    })


def _put_flow(code: str, partition_day: str, last_bar_day: str) -> None:
    """把资金流落进 `partition_day` 分区,序列最后一根 bar 为 `last_bar_day`。"""
    store.set_active_date(partition_day)
    try:
        store.put_raw("fundflow", code, _flow_df(last_bar_day), meta={"source": "test"})
    finally:
        store.set_active_date(None)


# ————————————————————————————————————————————————
# 1. 三态不另造第四套(单一真源)
# ————————————————————————————————————————————————
def test_三态复用契约枚举_不另造第四套():
    """serialize 的新鲜度三态必须 == contracts.ENUMS[新鲜度] == event 的三态。

    锁的语义:项目里**只允许一套**新鲜度词表。曾经的风险是各模块各造一套("过期"/"stale"/
    "陈旧"混用),下游就没法统一判断。谁再加第四套,这条会挂。
    """
    from tools.analysis import event
    assert (sz.FRESH, sz.STALE, sz.NODATA) == rc.ENUMS["新鲜度"]
    assert (sz.FRESH, sz.STALE, sz.NODATA) == (event.FRESH, event.STALE, event.NODATA)


# ————————————————————————————————————————————————
# 2. 旗舰回归:缓存最后一根是 as_of 前 13 天 → 绝不许声称是"今日"(直接对应问题①)
# ————————————————————————————————————————————————
def test_资金流缓存陈旧13天_不得声称今日(only_fundflow):
    """真实场景 fixture:资金流分区与最后一根 bar 都在 as_of 前 13 天(源采集失败、store 回退)。

    锁的语义:record 不得把 13 天前的资金流呈现为 as_of 当日的数据。
    产出必须同时满足 —— 数据还在(可参考)、口径日期是真实的那天、新鲜度标「陈旧」。
    """
    _put_flow(CODE, STALE_DAY, STALE_DAY)
    rec = sz.build_record(CODE, AS_OF)
    flow = rec["fundflow"]

    assert flow is not None, "陈旧不等于清空:旧数据要保留,否则丢掉「有旧数据可参考」的信息"
    assert flow["口径日期"] == STALE_DAY, "口径日期必须是数据真实那天,不是 as_of"
    assert flow["新鲜度"] == sz.STALE
    assert flow["口径日期"] != rec["meta"]["as_of"], "13 天前的数据不得与 as_of 同日"
    # 数据本体保留(不清空)
    assert flow["今日主力净流入"] == -702392.0
    # 字段名里的「今日」有歧义 → 必须有一条人读/LLM 可读的提示点明它指哪天
    assert STALE_DAY in (flow.get("口径提示") or "")
    # provenance 两条轴都要说得清:有数据(布尔 True)+ 但是旧的(口径.新鲜度=陈旧)
    assert rec["provenance"]["fundflow"] is True
    assert rec["provenance"]["口径"]["fundflow"] == {"口径日期": STALE_DAY, "新鲜度": sz.STALE}
    assert rc.validate_record(rec) == []


def test_资金流当日新鲜_标新鲜(only_fundflow):
    """口径日期 == as_of → 新鲜,且不挂多余的口径提示(避免天天报警、报警疲劳)。"""
    _put_flow(CODE, AS_OF, AS_OF)
    rec = sz.build_record(CODE, AS_OF)
    flow = rec["fundflow"]
    assert flow["口径日期"] == AS_OF and flow["新鲜度"] == sz.FRESH
    assert "口径提示" not in flow
    assert rec["provenance"]["口径"]["fundflow"]["新鲜度"] == sz.FRESH


def test_资金流无数据_标无数据而非陈旧(only_fundflow):
    """一根 bar 都没有 → 「无数据」,与「陈旧」严格区分。

    锁的语义:两者下游处置完全不同 —— 陈旧有旧值可参考(打折用),无数据必须走缺失降级。
    过去 summarize(空 df) 会返回一份「全 None + 连续天数 0」的空壳,使 provenance.fundflow
    报 True(撒谎说有资金流);这条同时锁住那个空壳不再出现。
    """
    rec = sz.build_record(CODE, AS_OF)          # tmp_path 下没落过任何 fundflow
    assert rec["fundflow"] is None
    assert rec["provenance"]["fundflow"] is False
    assert rec["provenance"]["口径"]["fundflow"] == {"口径日期": None, "新鲜度": sz.NODATA}
    assert sz.NODATA != sz.STALE                # 两态不可混同


def test_分区日与bar日不一致时以bar日为准(only_fundflow):
    """分区日 = as_of(今天确实采了),但序列最后一根 bar 仍是 13 天前(源返回了旧序列)。

    锁的语义:口径日期取**最后一根 bar 的日期**,不取分区日。分区日只说明"哪天采的",
    源返回旧序列时它会骗人;bar 自带的日期永远诚实(问题①里"今日主力净流入"取的就是最后一行)。
    """
    _put_flow(CODE, AS_OF, STALE_DAY)
    flow = sz.build_record(CODE, AS_OF)["fundflow"]
    assert flow["口径日期"] == STALE_DAY and flow["新鲜度"] == sz.STALE


# ————————————————————————————————————————————————
# 3. 问题②:报告期整体滞后 → 显式降级(不静默)
# ————————————————————————————————————————————————
def test_报告期滞后于已披露最新报告期_标陈旧并提示(monkeypatch, tmp_path):
    """valuation/fundamental 的 报告期 比披露日锚定的 financial.报告期 旧 → 陈旧 + 滞后标记。

    锁的语义:换一个报告期会让 PE_TTM 翻倍、净利率翻正负号,这种滞后**必须显式可见**。
    参照系用 financial 块(analysis.financial 按披露日锚定产出,PIT 正确),不是随手比日期。
    """
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    monkeypatch.setattr(sz.fd, "load_fundamental",
                        lambda c: {"PE_TTM": 39.79, "PB": 2.1, "总市值": 66.3,
                                   "报告期": "20260331", "净利": 1e7, "净利率": 3.08})
    # financial 块:半年报已披露(2026-06-30 报告期,08-27 披露)→ 基本面缓存明显滞后
    _fake_financial(monkeypatch, {"报告期": "20260630", "披露日": "2026-08-27", "评级": "中"})
    rec = sz.build_record(CODE, AS_OF)

    for blk_name in ("valuation", "fundamental"):
        blk = rec[blk_name]
        assert blk["新鲜度"] == sz.STALE, f"{blk_name} 报告期滞后必须标陈旧"
        assert "20260630" in blk["口径提示"] and "20260331" in blk["口径提示"]
    assert rec["valuation"]["报告期滞后"] is True
    assert rec["valuation"]["pe_ttm"] == 39.79, "陈旧不清空:旧口径的数还留着,只是标明了"
    assert rc.validate_record(rec) == []


def test_报告期不滞后时不误报(monkeypatch, tmp_path):
    """基本面报告期 == 已披露最新报告期 → 不打滞后标记(防止这条闸门变成天天响的噪声)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    monkeypatch.setattr(sz.fd, "load_fundamental",
                        lambda c: {"PE_TTM": 96.95, "报告期": "20260630"})
    _fake_financial(monkeypatch, {"报告期": "20260630", "披露日": "2026-08-27"})
    rec = sz.build_record(CODE, AS_OF)
    assert rec["valuation"]["报告期滞后"] is False
    assert "落后于已披露" not in (rec["valuation"].get("口径提示") or "")


def _fake_financial(monkeypatch, block: dict) -> None:
    """把 financial 块换成给定内容(它是报告期滞后判定的参照系)。

    只切数据入口,不伪造整套财报 raw:本测试要锁的是「valuation 报告期 vs 已披露最新报告期」
    这条交叉核对,而不是 analysis.financial 自己的取数逻辑(那有它自己的单测)。
    """
    from tools.analysis.financial import analyzer as fr
    monkeypatch.setattr(fr, "build_financial_block", lambda *a, **k: block)


# ————————————————————————————————————————————————
# 4. provenance:布尔向后兼容 + 按「实际有无数据」判 + 口径子字典
# ————————————————————————————————————————————————
def test_provenance布尔读法不回归(only_fundflow):
    """既有布尔键**类型不变**(大量代码/测试读 provenance.xxx 的 True/False)。

    锁的语义:向后兼容是硬要求 —— 升级"可自证"不能顺手把布尔改成 dict。
    """
    _put_flow(CODE, AS_OF, AS_OF)
    prov = sz.build_record(CODE, AS_OF)["provenance"]
    for k in ("tech", "fundamental", "fundflow", "chip", "consensus",
              "holder", "tick", "financing"):
        assert isinstance(prov[k], bool), f"provenance.{k} 必须仍是布尔"
    assert isinstance(prov["announcements"], int) and not isinstance(prov["announcements"], bool)
    assert prov["fundflow"] is True and prov["tech"] is False
    assert isinstance(prov["口径"], dict)          # 新信息只挂在这里,不覆盖任何老键


def test_只剩源不可得标记的空块_provenance必须报False():
    """块里只有「源不可得/降级」这类元信息标记 → 该维**没有可用数据**,布尔必须 False。

    锁的语义:过去用 `bool(块)` 判,块里只要有任何键就为 True —— 若某采集器把静默 None
    改成"带 源不可得 标记的空块",provenance 会**明确撒谎说有数据**。判据必须是
    "有没有非元信息的实际值",不是"块是不是真值"。
    """
    assert sz._has_data({"源不可得": True, "降级": ["源整体不可用"]}) is False
    assert sz._has_data({"as_of": "2026-09-03", "源状态": {"可转债": "不可得"}}) is False
    assert sz._has_data({"口径日期": "2026-09-03", "新鲜度": "新鲜"}) is False
    assert sz._has_data(None) is False and sz._has_data({}) is False
    # 有一个真实值就算有数据(哪怕其余全空)
    assert sz._has_data({"源不可得": False, "只数": 2}) is True


def test_provenance口径覆盖全部会过期的维():
    """口径子字典必须覆盖所有会过期的维,漏一个就等于那一维继续静默。"""
    assert set(rc.VINTAGE_BLOCKS) <= {
        "snapshot", "valuation", "fundamental", "fundflow", "chip", "consensus", "holder", "tick"}
    expect = {"tech", "fundamental", "valuation", "fundflow", "chip", "consensus", "holder",
              "tick", "announcements", "financial", "financing", "sentiment"}
    import inspect
    src = inspect.getsource(sz.build_record)
    for dim in expect:
        assert f'"{dim}": _provenance_dim' in src or f'"{dim}": (' in src, f"provenance.口径 漏了 {dim}"


# ————————————————————————————————————————————————
# 5. 契约层闸门
# ————————————————————————————————————————————————
def test_契约拦住第四种新鲜度说法():
    rec = _bare_record()
    rec["fundflow"] = {"今日主力净流入": 1.0, "口径日期": "2026-08-19", "新鲜度": "过期"}
    assert any("新鲜度" in e for e in rc.validate_record(rec))


def test_契约拦住陈旧却不说是哪天的():
    """标了「陈旧」却没有口径日期 → 不合规。

    锁的语义:"承认旧了但不说是哪天的"下游照样无法判断,等于没修 —— 那就是本轮 bug 的原形。
    """
    rec = _bare_record()
    rec["valuation"] = {"pe_ttm": 24.08, "新鲜度": "陈旧", "口径日期": None}
    errs = rc.validate_record(rec)
    assert any("陈旧" in e for e in errs), errs
    rec["valuation"]["口径日期"] = "2026-08-31"
    assert rc.validate_record(rec) == []


def test_陈旧可用口径提示替代口径日期():
    """「旧在报告期这条轴上」时判不出采集分区日 → 允许用口径提示说清,仍合规。

    锁的语义:闸门要的是**说清旧在哪**,不是死抠某个字段。报告期整体滞后时,旧在报告期
    而未必判得出分区日;此时提示里写明"报告期 X 落后于已披露 Y"已经足够下游判断。
    但两者**都没有**仍然不合规(见上一条)——那才是本轮 bug 的原形。
    """
    rec = _bare_record()
    rec["fundamental"] = {"净利率": 3.08, "新鲜度": "陈旧", "口径日期": None,
                          "口径提示": "报告期 20260331 落后于已披露最新报告期 20260630"}
    assert rc.validate_record(rec) == []


def test_契约对旧记录宽容_无口径字段仍合规():
    """向后兼容:历史记录没有口径日期/新鲜度字段,不许因此判为不合规。"""
    rec = _bare_record()
    rec["fundflow"] = {"今日主力净流入": 1.0}
    rec["provenance"] = {"fundflow": True}
    assert rc.validate_record(rec) == []


def test_契约校验provenance口径子字典():
    rec = _bare_record()
    rec["provenance"] = {"fundflow": True, "口径": {"fundflow": {"新鲜度": "很新"}}}
    assert any("provenance.口径.fundflow" in e for e in rc.validate_record(rec))
    rec["provenance"]["口径"]["fundflow"] = {"口径日期": "08/19", "新鲜度": "陈旧"}
    assert any("非日期" in e for e in rc.validate_record(rec))


def _bare_record() -> dict:
    return {"schema_version": "1.0",
            "meta": {"code": CODE, "name": "科华控股", "as_of": AS_OF},
            "events": [], "timeseries_refs": {}, "provenance": {}}


# ————————————————————————————————————————————————
# 6. panel:同一行各列不同龄必须可见
# ————————————————————————————————————————————————
def test_panel暴露混龄(monkeypatch):
    """实证过的混龄:收盘是 09-02 口径,而同一行的 PE/市值按 08-31 收盘价折算。

    锁的语义:panel 只拍平、不自己另算口径(单一真源在 record 的块内),
    但必须把各列口径日期和一个一眼可见的 `混龄` 标记摊到行上 —— 否则横向比较看不出错位。
    """
    rec = {"meta": {"code": "601882", "name": "海天精工", "as_of": "2026-09-02"},
           "snapshot": {"close": 22.6, "口径日期": "2026-09-02", "新鲜度": "新鲜"},
           "valuation": {"pe_ttm": 24.08, "mktcap_yi": 114.37,
                         "口径日期": "2026-08-31", "新鲜度": "陈旧", "报告期滞后": False},
           "fundflow": {"今日主力净流入": 8.03e6, "口径日期": "2026-08-19", "新鲜度": "陈旧"},
           "events": []}
    row = panel._row(rec)
    assert row["记录日期"] == "2026-09-02"
    assert row["价格日期"] == "2026-09-02" and row["估值日期"] == "2026-08-31"
    assert row["资金流日期"] == "2026-08-19" and row["资金流新鲜度"] == "陈旧"
    assert row["混龄"] is True, "价格 09-02 / 估值 08-31 / 资金流 08-19 三个口径,必须标混龄"
    assert row["报告期滞后"] is False


def test_panel同龄不报混龄():
    rec = {"meta": {"code": "601882", "name": "海天精工", "as_of": AS_OF},
           "snapshot": {"close": 22.6, "口径日期": AS_OF},
           "valuation": {"pe_ttm": 24.08, "口径日期": AS_OF},
           "fundflow": {"今日主力净流入": 1.0, "口径日期": AS_OF},
           "events": []}
    assert panel._row(rec)["混龄"] is False


def test_panel旧记录无口径字段_混龄为None不假装没问题():
    """判不出就是 None,不许写成 False —— 那会把"不知道"说成"没问题"。"""
    rec = {"meta": {"code": "601882", "name": "海天精工"},
           "snapshot": {"close": 22.6}, "valuation": {"pe_ttm": 24.08}, "events": []}
    row = panel._row(rec)
    assert row["混龄"] is None and row["价格日期"] is None
