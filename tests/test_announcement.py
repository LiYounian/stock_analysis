"""announcement.py 单测(锁打标规则 + mock 巨潮源)。"""
import types

import pandas as pd
import pytest

from tools.collectors import announcement as an
from tools.store import repo as store


def test_classify_title():
    assert an.classify_title("关于回购公司股份的进展公告") == "回购"
    assert an.classify_title("控股股东增持股份计划") == "增持"
    assert an.classify_title("股东减持股份结果公告") == "减持"
    assert an.classify_title("2026年半年度业绩预告") == "业绩预告"
    assert an.classify_title("关于签订重大合同的公告") == "合同订单"
    assert an.classify_title("关于收到诉讼通知的公告") == "诉讼仲裁"
    assert an.classify_title("2022年股票期权激励计划注销公告") == "股权激励"
    assert an.classify_title("向特定对象发行A股股票") == "再融资"
    assert an.classify_title("持股5%以上股东权益变动") == "权益变动"
    assert an.classify_title("股票交易异常波动公告") == "交易异动"
    assert an.classify_title("某项无关紧要的说明") == "其他"


def test_impact_hint():
    assert an.impact_hint("控股股东增持计划") == "利好"
    assert an.impact_hint("业绩预告:预计净利润预增80%") == "利好"
    assert an.impact_hint("股东减持股份") == "利空"
    assert an.impact_hint("关于诉讼事项的公告") == "利空"
    assert an.impact_hint("2026半年度业绩预告") == "待判"      # 无方向词
    assert an.impact_hint("董事会决议公告") == "待判"


def _fake_cninfo_df():
    return pd.DataFrame({
        "代码": ["000021", "000021"],
        "简称": ["深科技", "深科技"],
        "公告标题": ["关于回购公司股份的公告", "股东减持计划公告"],
        "公告时间": ["2026-08-01 00:00:00", "2026-08-03 00:00:00"],
        "公告链接": ["http://a", "http://b"],
    })


def _install(monkeypatch, df):
    fake = types.SimpleNamespace(
        stock_zh_a_disclosure_report_cninfo=lambda **kw: df)
    monkeypatch.setitem(__import__("sys").modules, "akshare", fake)


def test_fetch_tags_and_sorts(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install(monkeypatch, _fake_cninfo_df())
    out = an.fetch_announcements(["000021"], days=30)
    items = out["000021"]
    assert items[0]["date"] == "2026-08-03"          # 倒序:减持在前
    assert items[0]["type"] == "减持" and items[0]["impact"] == "利空"
    assert items[1]["type"] == "回购" and items[1]["impact"] == "利好"
    assert store.get_raw_meta("announcement", "000021")["source"] == "cninfo"


def test_load_roundtrip_and_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    _install(monkeypatch, _fake_cninfo_df())
    an.fetch_announcements(["000021"], days=30)
    assert len(an.load_announcements("000021")) == 2
    with pytest.raises(FileNotFoundError):
        an.load_announcements("999999")
