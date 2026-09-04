"""baidu_news.py 单测(mock 网络,不触网)。

锁的语义:
- 解析归一到契约字段(title/source/publish_time/publish_ts/benefit_type/benefit_label/abstract/url/news_id);
- publish_ts 是**真实发布时间戳**(unix 秒),publish_time 是其北京时区解码(非采集日冒充);
- benefitType 1/−1/0 → 利好/利空/中性 标签映射 + benefit_to_sentiment 方向;
- 落盘倒序 + news_id 去重幂等 + 前向增量并集(旧快照独有条保留);
- 新鲜度门控:缓存新鲜跳过重拉(不触网),陈旧/无缓存才拉;
- 反爬容错:非 200/结构漂移/空数据优雅降级不崩;
- news_asof 用真实发布时间做无未来函数切片;
- 港股落空降级;load 往返 + 缺失抛错。
"""
import sys
import types

import pytest

from tools.collectors import baidu_news as bn
from tools.store import repo as store


# 2026-08-30 15:41:27 CST / 2026-08-29 / 2026-08-25 的 unix 秒(东八区)
TS_0830 = 1788075687
TS_0829 = 1788075687 - 86400
TS_0825 = 1788075687 - 5 * 86400


def _raw_item(nid, title, ts, benefit="0", provider="东方财富网", url="http://e/x"):
    return {"title": title, "abstract": "摘要" + title, "publishTime": str(ts),
            "provider": provider, "originUrl": url, "benefitType": str(benefit),
            "messageType": "2", "news_id": nid}


def _payload(items):
    """包成百度真实嵌套结构 Result[0].TplData.aiSentimentXcxListInfo.sentimentListInfo。"""
    return {"ResultCode": "0", "Result": [
        {"TplData": {"aiSentimentXcxListInfo": {"sentimentListInfo": items}}}]}


def _install_fetch(monkeypatch, items):
    """把 _fetch_raw 换成返回给定原始条目(等价 mock 网络层),并 A 股化。"""
    monkeypatch.setattr(bn, "_fetch_raw", lambda code, rn: items)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "is_hk", lambda code: False)


# ———————————— 解析 / 时间戳 / 标签 ————————————
def test_parse_normalizes_contract_and_real_publish_time():
    items = bn._parse([_raw_item("id1", "利好新闻", TS_0830, benefit="1")])
    assert len(items) == 1
    it = items[0]
    assert set(it.keys()) == {"title", "source", "publish_time", "publish_ts",
                              "benefit_type", "benefit_label", "abstract", "url", "news_id"}
    assert it["publish_ts"] == TS_0830                 # 真实发布时间戳,非采集日
    assert it["publish_time"] == "2026-08-30 15:41:27"  # 北京时区解码
    assert it["benefit_type"] == 1 and it["benefit_label"] == "利好"
    assert it["source"] == "东方财富网" and it["news_id"] == "id1"


def test_parse_sorts_desc_and_drops_bad():
    items = bn._parse([
        _raw_item("a", "早", TS_0825),
        _raw_item("b", "晚", TS_0830),
        {"title": "无时间戳", "publishTime": "", "news_id": "c"},   # 无 ts → 丢
        {"title": "", "publishTime": str(TS_0830), "news_id": "d"},  # 无标题 → 丢
    ])
    assert [it["news_id"] for it in items] == ["b", "a"]   # 倒序,坏条被丢


def test_benefit_label_and_sentiment_mapping():
    assert bn.benefit_label("1") == "利好" and bn.benefit_label(-1) == "利空"
    assert bn.benefit_label("0") == "中性" and bn.benefit_label(None) == "中性"
    assert bn.benefit_label("99") == "中性"    # 未知 → 保守中性
    assert bn.benefit_to_sentiment("1") == 1.0
    assert bn.benefit_to_sentiment("-1") == -1.0
    assert bn.benefit_to_sentiment("0") == 0.0 and bn.benefit_to_sentiment(None) == 0.0


# ———————————— 结构提取 / 反爬容错 ————————————
def test_extract_list_defensive_on_drift():
    assert bn._extract_list({}) == []                       # 空
    assert bn._extract_list({"Result": "垃圾"}) == []        # 类型漂移
    assert bn._extract_list(None) == []                     # 非 dict
    good = _payload([_raw_item("x", "t", TS_0830)])
    assert len(bn._extract_list(good)) == 1


def test_fetch_tolerates_network_error(monkeypatch, tmp_path):
    """单票抛异常(限流/网络)→ 降级跳过,不入返回值,不中断整批。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "is_hk", lambda code: False)

    def _boom(code, rn):
        raise ConnectionError("被限流")

    monkeypatch.setattr(bn, "_fetch_raw", _boom)
    out = bn.fetch_baidu_news(["000001"])
    assert "000001" not in out          # 真失败不入返回


# ———————————— 落盘 / 幂等 / 增量 / 门控 ————————————
def test_fetch_persists_dedup_and_meta(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    # 同一 news_id 出现两次 → 落盘只留一条(幂等去重)
    _install_fetch(monkeypatch, [
        _raw_item("dup", "重复", TS_0830),
        _raw_item("dup", "重复(再)", TS_0830),
        _raw_item("uniq", "唯一", TS_0829),
    ])
    out = bn.fetch_baidu_news(["000001"])
    items = out["000001"]
    ids = [it["news_id"] for it in items]
    assert ids.count("dup") == 1 and "uniq" in ids
    assert items[0]["publish_ts"] >= items[-1]["publish_ts"]      # 倒序
    assert store.get_raw_meta("baidu_news", "000001")["source"] == "baidu"


def test_incremental_union_keeps_old_snapshot(monkeypatch, tmp_path):
    """前向增量:第二次拉只返新条,旧快照独有条仍保留(累积不丢)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("old", "旧闻", TS_0825)])
    bn.fetch_baidu_news(["000001"])
    # 第二次:接口只返一条新的(旧条已从接口滑出),门控关掉强制重拉
    _install_fetch(monkeypatch, [_raw_item("new", "新闻", TS_0830)])
    out = bn.fetch_baidu_news(["000001"], skip_fresh=False)
    ids = {it["news_id"] for it in out["000001"]}
    assert ids == {"old", "new"}        # 旧快照独有条被增量并集保留


def test_freshness_gate_skips_fresh(monkeypatch, tmp_path):
    """缓存新鲜 → 跳过重拉(不触网),沿用既有快照。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("a", "首采", TS_0830)])
    bn.fetch_baidu_news(["000001"])          # 首采写入(fetched_at=now)
    # 换成会爆炸的 fetch;门控应直接跳过不调用它
    monkeypatch.setattr(bn, "_fetch_raw",
                        lambda code, rn: (_ for _ in ()).throw(AssertionError("不该触网")))
    out = bn.fetch_baidu_news(["000001"], skip_fresh=True, max_days=5)
    assert out["000001"][0]["news_id"] == "a"    # 沿用既有,未触网


def test_stale_codes_filters(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("a", "x", TS_0830)])
    bn.fetch_baidu_news(["000001"])
    # 000001 刚采(新鲜)→ 不在需重拉列表;未采的 000002 → 在
    need = bn.stale_codes(["000001", "000002"], max_days=5)
    assert need == ["000002"]


def test_hk_falls_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "is_hk", lambda code: True)
    monkeypatch.setattr(bn, "_fetch_raw",
                        lambda code, rn: (_ for _ in ()).throw(AssertionError("港股不该触网")))
    out = bn.fetch_baidu_news(["00700"])
    assert out["00700"] == []
    assert store.get_raw_meta("baidu_news", "00700")["source"] == "none(hk)"


# ———————————— as-of 无未来函数切片 ————————————
def test_news_asof_filters_by_real_publish_time(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [
        _raw_item("late", "8-30 的", TS_0830),
        _raw_item("early", "8-25 的", TS_0825),
    ])
    bn.fetch_baidu_news(["000001"])
    # as_of=8-28:只应看到 8-25 那条,8-30 的未来条被切掉
    got = bn.news_asof("000001", "2026-08-28")
    assert [it["news_id"] for it in got] == ["early"]


# ———————————— load 往返 / 缺失 ————————————
def test_load_roundtrip_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("a", "x", TS_0830)])
    bn.fetch_baidu_news(["000001"])
    assert isinstance(bn.load_baidu_news("000001"), list)
    with pytest.raises(FileNotFoundError):
        bn.load_baidu_news("999999")


# ———— #27 条目级新鲜度:看最新条目发布日(非采集时刻),陈旧显式标记 + 告警 ————
import logging  # noqa: E402

_NOW = 1788000000                          # 2026-08-29 附近的一个固定"当前"时间戳
_OLD = _NOW - 60 * 86400                    # 60 天前(> 14 天阈值)→ 陈旧


def test_newest_item_ts_and_stale_days():
    items = [{"publish_ts": _OLD}, {"publish_ts": _OLD - 86400}]
    assert bn.newest_item_ts(items) == _OLD                 # 取最新(最大)
    assert bn.newest_item_ts([]) is None
    assert round(bn.content_stale_days(items, now_ts=_NOW), 0) == 60
    assert bn.content_stale_days([], now_ts=_NOW) is None    # 无有效条目 → None(不静默当 0)


def test_content_freshness_meta_boundary():
    fresh = bn._content_freshness_meta([{"publish_ts": int(bn.datetime.now(bn._CST).timestamp())}], 14.0)
    assert fresh["content_stale"] is False and fresh["newest_item_date"]
    stale = bn._content_freshness_meta([{"publish_ts": _OLD}], 14.0)
    assert stale["content_stale"] is True and stale["item_stale_days"] > 14
    empty = bn._content_freshness_meta([], 14.0)
    assert empty == {"newest_item_date": None, "item_stale_days": None, "content_stale": False}


def test_fetch_marks_and_warns_content_stale(monkeypatch, tmp_path, caplog):
    """最新条目落后超阈值 → meta.content_stale=True 且 warning(采到但陈旧不再静默当有效)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("old", "陈旧", _OLD)])
    with caplog.at_level(logging.WARNING, logger="collectors.baidu_news"):
        bn.fetch_baidu_news(["000001"])
    meta = store.get_raw_meta("baidu_news", "000001")
    assert meta["content_stale"] is True and meta["item_stale_days"] > 14
    assert meta["newest_item_date"]                          # 落了最新条目日期
    assert any("内容陈旧" in r.message for r in caplog.records)


def test_skip_fresh_still_flags_stale_content(monkeypatch, tmp_path, caplog):
    """核心:采集时刻新鲜(fetched_at 新)但条目陈旧,跳过重拉时仍告警(旧门控此处永不报)。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install_fetch(monkeypatch, [_raw_item("old", "陈旧", _OLD)])
    bn.fetch_baidu_news(["000001"])                          # 首采,fetched_at=now(新鲜)
    monkeypatch.setattr(bn, "_fetch_raw",
                        lambda code, rn: (_ for _ in ()).throw(AssertionError("不该触网")))
    with caplog.at_level(logging.WARNING, logger="collectors.baidu_news"):
        bn.fetch_baidu_news(["000001"], skip_fresh=True, max_days=5)   # 采集时刻新鲜 → 跳过
    assert any("缓存新鲜跳过,但内容陈旧" in r.message for r in caplog.records)
