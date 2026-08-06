"""policy.py 单测(mock 东财,不触网)。

锁语义:
- default_keywords 含票池行业关键词 + 宏观词;
- 归一 + region/行业命中打标正确;
- 时间窗过滤 / 去重 / require_industry_hit 过滤;
- 落盘/读盘往返;空数据(全失败)抛错不静默。
"""
import sys
import types

import pandas as pd
import pytest

from tools.collectors import policy as pol


def _fake_df(rows):
    return pd.DataFrame(rows, columns=["关键词", "新闻标题", "新闻内容",
                                       "发布时间", "文章来源", "新闻链接"])


def _domestic_row(kw="半导体 补贴"):
    return [kw, "国家出台半导体产业补贴新政", "发改委发布集成电路专项补贴政策",
            "2026-08-06 09:00:00", "证券时报", "http://x/1"]


def _foreign_row(kw="出口管制"):
    return [kw, "美国收紧对华芯片出口管制", "美国商务部BIS更新半导体出口管制清单",
            "2026-08-06 10:00:00", "财联社", "http://x/2"]


def _old_row(kw="芯片"):
    return [kw, "很旧的芯片政策", "2000年的芯片补贴", "2000-01-01 08:00:00",
            "旧报", "http://x/old"]


def _nohit_row(kw="政策"):
    return [kw, "耶路撒冷地位争议", "与票池行业无关的国际政治新闻",
            "2026-08-06 11:00:00", "某报", "http://x/3"]


# ---------- default_keywords ----------

def test_default_keywords_covers_pool_industries():
    kws = pol.default_keywords()
    joined = " ".join(kws)
    # 票池核心行业词都应出现在检索词里
    for term in ("半导体", "机器人", "算力", "新能源"):
        assert term in joined, term
    # 宏观独立词也在
    assert "美联储" in kws and "关税" in kws
    # 组合词形如「半导体 补贴」
    assert any(" " in kw for kw in kws)
    # 去重
    assert len(kws) == len(set(kws))


# ---------- 打标:region / 行业 ----------

def test_region_and_industry_tagging(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)
    raw = [dict(zip(["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"], r))
           for r in (_domestic_row(), _foreign_row())]
    out = pol.tag_and_dump(raw, days=3650)
    assert len(out) == 2
    by_url = {r["url"]: r for r in out}
    assert by_url["http://x/1"]["region"] == "国内"
    assert by_url["http://x/2"]["region"] == "国外"
    # 两条都命中半导体板块
    assert all("半导体" in r["industries"] for r in out)
    # 契约字段齐全
    assert set(out[0]) == {"date", "title", "source", "url", "region",
                           "summary", "industries", "keyword"}


# ---------- 时间窗 / 去重 / 无命中过滤 ----------

def test_window_dedup_and_hit_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)
    d = dict(zip(["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"],
                 _domestic_row()))
    raw = [
        d, dict(d),                 # 同 url → 去重
        dict(zip(["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"], _old_row())),   # 超窗
        dict(zip(["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"], _nohit_row())),  # 无行业命中
    ]
    out = pol.tag_and_dump(raw, days=7)
    assert len(out) == 1                 # 去重后只剩 1 条国内政策
    assert out[0]["url"] == "http://x/1"


def test_hit_filter_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)
    raw = [dict(zip(["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"],
                    _nohit_row()))]
    out = pol.tag_and_dump(raw, days=3650, require_industry_hit=False)
    assert len(out) == 1 and out[0]["industries"] == []


# ---------- fetch_policy 端到端(mock akshare)----------

def _install_ak(monkeypatch, df):
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: df)
    monkeypatch.setitem(sys.modules, "akshare", fake)


def test_fetch_policy_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: _fake_df([_domestic_row(kw)]))
    out = pol.fetch_policy(keywords=["半导体 补贴"], days=3650)
    assert len(out) == 1 and out[0]["region"] == "国内"
    # 读盘往返
    loaded = pol.load_policy()
    assert loaded == out
    with pytest.raises(FileNotFoundError):
        pol.load_policy("1999-01-01")


def test_fetch_policy_empty_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: _fake_df([]))
    with pytest.raises(RuntimeError):
        pol.fetch_policy(keywords=["半导体 补贴"], days=3650)


def test_fetch_policy_single_keyword_failure_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(pol, "_POLICY_DIR", tmp_path)

    def flaky(kw):
        if kw == "boom":
            raise RuntimeError("接口炸了")
        return _fake_df([_domestic_row(kw)])

    monkeypatch.setattr(pol, "_fetch_em", flaky)
    out = pol.fetch_policy(keywords=["boom", "半导体 补贴"], days=3650)
    assert len(out) == 1        # 失败关键词跳过,好的仍入库
