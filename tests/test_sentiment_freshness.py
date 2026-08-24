"""情绪数据新鲜度单测(date-pin + 三态新鲜度,mock LLM,不联网)。

锁死「为什么改」的语义(防未来重写误删):
- date-pin 后回退不再静默:窗口内回退旧 raw 标「陈旧」+ 采集日期为旧分区(A2);A1 严格锁定标「无数据」。
- 当日锁定日采到 → 「新鲜」;超窗 → 「无数据」(不喂旧数据)。
- 顶层新鲜度=三层最坏优先聚合;顶层采集日期=最旧层。
- 「无数据」(样本0)与「真中性 0.0」(有样本)可区分。
- record.schema:带新字段合规,旧记录(无新字段)仍合规,非法枚举报错。
- 稳定消费接口(净情绪分/样本数/利好数/利空数/口径/三层.*.净情绪)结构与口径不变。

隔离:tmp_path + monkeypatch store 路径根 + LLM_CACHE;绝不污染真实 data/。
"""
import pytest

from tools.analysis import event as ev
from tools.config.stock_pool import Stock
from tools.contracts import record as rc
from tools.store import repo as store


# ---------- mock LLM ----------
class _Fake:
    """按 schema 内容返回固定结果(新闻默认利好;政策/UGC 各自分支)。"""

    def extract(self, text, schema, *, instruction, temperature=0.0):
        if "受影响行业" in schema:
            return {"影响方向": "利好", "影响强度": 4, "受影响行业": ["半导体"]}
        if "净情绪" in schema:
            return {"净情绪": 0.6, "多空": "偏多", "依据": "x"}
        return {"事件类型": "业绩", "影响方向": "利好", "影响强度": 4,
                "与本股关系": "直接", "摘要": "x"}


class _NeutralNews(_Fake):
    """新闻判「中性」→ 净情绪算 0.0 但样本数>0(用于区分「真中性」与「无数据」)。"""

    def extract(self, text, schema, *, instruction, temperature=0.0):
        if "受影响行业" in schema or "净情绪" in schema:
            return super().extract(text, schema, instruction=instruction)
        return {"事件类型": "业绩", "影响方向": "中性", "影响强度": 3,
                "与本股关系": "直接", "摘要": "x"}


# ---------- 隔离 fixture ----------
@pytest.fixture
def iso(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path / "llm_cache")
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(ev.settings, "SENTIMENT_FRESHNESS_MODE", "A2")
    monkeypatch.setattr(ev.settings, "SENTIMENT_MAX_STALE_DAYS", 3)
    monkeypatch.setattr(ev.stock_pool, "get",
                        lambda c: Stock(c, "测试票", "半导体封测", "半导体"))
    yield tmp_path
    store.set_active_date(None)


CODE = "002156"


def _put_news(tmp, date, items=None):
    items = items or [{"title": "中标5亿", "content": "利好业绩", "time": f"{date}T09:00",
                       "source": "东财", "url": "n1"}]
    store.put_raw("news", CODE, items, meta={"source": "东财"}, date=date)


def _put_ugc(tmp, date, items=None):
    items = items or [{"text": "看多"}, {"text": "冲"}]
    store.put_raw("ugc", CODE, items, meta={"source": "guba"}, date=date)


def _mkdate(tmp, date):
    """仅创建空日期分区目录(用于交易日代理计数)。"""
    (tmp / "raw" / date).mkdir(parents=True, exist_ok=True)


def _news_layer(tmp, mode="A2", locked=None, client=None):
    rec = ev.analyze_stock(CODE, client=client or _Fake(), date=locked)
    return rec["sentiment"]


# ============ 锁 date-pin ============
def test_fallback_not_silent_A2(iso):
    """A2:当日缺 news、T-1 有 → 该层回退旧数据但标「陈旧」,采集日期=T-1,锁定日期=T。"""
    _put_news(iso, "2026-08-05")
    _put_ugc(iso, "2026-08-06")            # 舆情当日有(不干扰新闻断言)
    s = _news_layer(iso, locked="2026-08-06")
    layer = s["三层"]["新闻"]
    assert layer["新鲜度"] == "陈旧"
    assert layer["采集日期"] == "2026-08-05"
    assert s["锁定日期"] == "2026-08-06"
    assert layer["样本数"] == 1             # 读到了 T-1 的旧内容(可识别,非静默)


def test_fallback_strict_A1_no_data(iso, monkeypatch):
    """A1 严格锁定:当日缺 news → 该层「无数据」,绝不回退旧数据(样本0)。"""
    monkeypatch.setattr(ev.settings, "SENTIMENT_FRESHNESS_MODE", "A1")
    _put_news(iso, "2026-08-05")
    s = _news_layer(iso, locked="2026-08-06")
    layer = s["三层"]["新闻"]
    assert layer["新鲜度"] == "无数据"
    assert layer["采集日期"] is None
    assert layer["样本数"] == 0             # 未读 T-1 内容


def test_fresh_hit_same_day(iso):
    """当日锁定日采到 → 「新鲜」,采集日期=T。"""
    _put_news(iso, "2026-08-06")
    s = _news_layer(iso, locked="2026-08-06")
    layer = s["三层"]["新闻"]
    assert layer["新鲜度"] == "新鲜"
    assert layer["采集日期"] == "2026-08-06"


def test_over_window_no_fallback(iso, monkeypatch):
    """A2 超窗(> max_stale_days 个交易日代理)→ 「无数据」,不读旧数据。"""
    monkeypatch.setattr(ev.settings, "SENTIMENT_MAX_STALE_DAYS", 3)
    _put_news(iso, "2026-08-01")
    for d in ("2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08", "2026-08-10"):
        _mkdate(iso, d)                     # 期间 5 个采集分区 → rank=5 > 3
    s = _news_layer(iso, locked="2026-08-10")
    layer = s["三层"]["新闻"]
    assert layer["新鲜度"] == "无数据"
    assert layer["采集日期"] is None
    assert layer["样本数"] == 0


# ============ 锁三层独立 + 顶层聚合 ============
def test_three_layers_independent_and_top_worst(iso):
    """新闻新鲜、UGC陈旧、政策无数据 → 各层正确;顶层=陈旧(最坏),采集日期=最旧层。"""
    _put_news(iso, "2026-08-06")            # 新闻:当日 → 新鲜
    _put_ugc(iso, "2026-08-04")             # 舆情:回退 2 天 → 陈旧
    _mkdate(iso, "2026-08-05")              # 期间分区(rank 内)
    # 不写政策 raw、不 score_policy → 政策无数据
    s = _news_layer(iso, locked="2026-08-06")
    assert s["三层"]["新闻"]["新鲜度"] == "新鲜"
    assert s["三层"]["舆情"]["新鲜度"] == "陈旧"
    assert s["三层"]["舆情"]["采集日期"] == "2026-08-04"
    assert s["三层"]["政策"]["新鲜度"] == "无数据"
    assert s["新鲜度"] == "陈旧"            # 最坏优先:任一层陈旧
    assert s["采集日期"] == "2026-08-04"    # 最旧层日期


def test_nodata_vs_true_neutral(iso):
    """「无数据」(样本0)与「真中性 0.0」(有样本、净=0)可区分。"""
    _put_news(iso, "2026-08-06")            # 新闻当日有 → 新鲜;判中性 → 净0.0 但样本>0
    # 不写 UGC → 舆情无数据
    s = _news_layer(iso, locked="2026-08-06", client=_NeutralNews())
    n = s["三层"]["新闻"]
    assert n["新鲜度"] == "新鲜" and n["样本数"] > 0 and n["净情绪"] == 0.0
    u = s["三层"]["舆情"]
    assert u["新鲜度"] == "无数据" and u["样本数"] == 0


# ============ 锁稳定消费接口不动 ============
def test_frozen_consumption_fields_intact(iso):
    """冻结字段:净情绪分/样本数/利好数/利空数/口径/三层.*.净情绪 键名·类型·口径不变。"""
    _put_news(iso, "2026-08-06")
    _put_ugc(iso, "2026-08-06")
    s = _news_layer(iso, locked="2026-08-06")
    assert isinstance(s["净情绪分"], float) and -1 <= s["净情绪分"] <= 1
    assert isinstance(s["样本数"], int)
    assert isinstance(s["利好数"], int) and isinstance(s["利空数"], int)
    assert s["口径"] == "三层加权 新闻0.5/政策0.3/舆情0.2,缺层重归一"
    for lname in ("新闻", "舆情", "政策"):
        assert "净情绪" in s["三层"][lname] and "样本数" in s["三层"][lname]
    # 新字段为附加并列,不覆盖旧结构
    assert set(s) >= {"净情绪分", "利好数", "利空数", "样本数", "口径", "三层",
                      "采集日期", "新鲜度", "锁定日期"}


# ============ 锁契约兼容 ============
def _rec_with(sentiment):
    return {"schema_version": "1.0",
            "meta": {"code": CODE, "name": "测试", "as_of": "2026-08-06"},
            "sentiment": sentiment, "events": [],
            "timeseries_refs": {}, "provenance": {}}


def test_schema_new_fields_valid():
    rec = _rec_with({"净情绪分": 0.1, "利好数": 1, "利空数": 0, "样本数": 1,
                     "采集日期": "2026-08-05", "新鲜度": "陈旧", "锁定日期": "2026-08-06",
                     "三层": {"新闻": {"净情绪": 0.1, "样本数": 1,
                                      "采集日期": "2026-08-05", "新鲜度": "陈旧"},
                             "舆情": {"净情绪": 0.0, "样本数": 0,
                                      "采集日期": None, "新鲜度": "无数据"}}})
    assert rc.validate_record(rec) == []


def test_schema_old_record_still_valid():
    """旧记录(无任何新鲜度字段)仍合规(向后兼容)。"""
    rec = _rec_with({"净情绪分": 0.2, "利好数": 1, "利空数": 0, "样本数": 1,
                     "events": [{"影响方向": "利好", "与本股关系": "直接",
                                 "层": "公司行为", "影响强度": 4}]})
    assert rc.validate_record(rec) == []


def test_schema_bad_freshness_enum():
    rec = _rec_with({"净情绪分": 0.0, "新鲜度": "很新鲜"})
    assert any("新鲜度" in e for e in rc.validate_record(rec))
    rec2 = _rec_with({"净情绪分": 0.0,
                      "三层": {"新闻": {"净情绪": 0.0, "样本数": 1, "新鲜度": "超陈旧"}}})
    assert any("三层.新闻.新鲜度" in e for e in rc.validate_record(rec2))


def test_schema_bad_asof_date():
    rec = _rec_with({"净情绪分": 0.0, "采集日期": "2026/08/06"})
    assert any("采集日期" in e for e in rc.validate_record(rec))
