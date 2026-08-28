"""industry_history.py 单测(纯解析 + 时点还原,不触网)。

锁语义:巨潮变更史解析(升序/列名容错)、industry_at 取「变更日期≤date 的最后一条」、
无记录/时点前无生效记录降级 None。
"""
import pandas as pd

from tools.collectors import industry_history as ih


def test_parse_sort_and_colname():
    df = pd.DataFrame([
        {"变更日期": "2020-05-01", "分类标准": "证监会行业分类标准", "行业名称": "汽车制造"},
        {"变更日期": "2015-03-01", "分类标准": "证监会行业分类标准", "行业名称": "专用设备"},
    ])
    items = ih._parse(df)
    assert [x["date"] for x in items] == ["2015-03-01", "2020-05-01"]   # 升序
    assert items[0]["industry"] == "专用设备"


def test_industry_at(monkeypatch):
    hist = [
        {"date": "2015-03-01", "industry": "专用设备", "std": "证监会行业分类标准"},
        {"date": "2020-05-01", "industry": "汽车制造", "std": "证监会行业分类标准"},
    ]
    monkeypatch.setattr(ih, "load_industry_history", lambda code: hist)
    assert ih.industry_at("002594", "2018-06-30") == "专用设备"   # 落在两次变更之间
    assert ih.industry_at("002594", "2022-01-01") == "汽车制造"   # 最新一次之后
    assert ih.industry_at("002594", "2010-01-01") is None         # 首次变更之前


def test_industry_at_no_cache(monkeypatch):
    def _raise(code):
        raise FileNotFoundError
    monkeypatch.setattr(ih, "load_industry_history", _raise)
    assert ih.industry_at("999999", "2022-01-01") is None
