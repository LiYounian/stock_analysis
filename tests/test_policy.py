"""policy.py 单测(mock 东财 / 新闻联播,不触网)。

锁语义:
- default_keywords 含票池行业关键词 + 宏观词;
- 归一 + region/行业命中打标正确;
- 时间窗过滤 / 去重 / require_industry_hit 过滤;
- 落盘/读盘往返经 store 层(路径 monkeypatch 到临时目录,不污染真实 data/);
- 主源(东财)失败/空时回落新闻联播备源出数,备源条目同样经行业打标;
- meta.source 记录实际命中源(eastmoney / cctv);
- 两源均无结果才抛错不静默。
"""
import pandas as pd
import pytest

from tools.collectors import policy as pol
from tools.store import repo as store


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    """把 store 的 raw 路径根 monkeypatch 到临时目录,policy 落盘/读盘全走 store。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(store, "_RAW_DIR", raw)
    return store


_EM_COLS = ["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"]


def _fake_df(rows):
    return pd.DataFrame(rows, columns=_EM_COLS)


def _em_dict(row):
    return dict(zip(_EM_COLS, row))


def _cctv_df(rows):
    """新闻联播返回 DataFrame,列 {date(YYYYMMDD), title, content}。"""
    return pd.DataFrame(rows, columns=["date", "title", "content"])


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


def _today_str():
    return pd.Timestamp.today().strftime("%Y-%m-%d")


# ---------- default_keywords ----------

def test_default_keywords_covers_pool_industries(monkeypatch):
    # 池无关:default_keywords 从自选池 sector 派生,若耦合 live 池,砍池就误挂。
    # 固定一个覆盖核心行业的测试池,只验"池行业→关键词"的派生逻辑本身。
    from tools.config.stock_pool import Stock
    fake = [Stock(f"00000{i}", "n", "x", sec) for i, sec in
            enumerate(("半导体", "机器人/自动化", "AI算力", "新能源材料"))]
    monkeypatch.setattr(pol.stock_pool, "get_pool", lambda: fake)
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

def test_region_and_industry_tagging(store_dir):
    raw = [_em_dict(r) for r in (_domestic_row(), _foreign_row())]
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

def test_window_dedup_and_hit_filter(store_dir):
    d = _em_dict(_domestic_row())
    raw = [
        d, dict(d),                 # 同 url → 去重
        _em_dict(_old_row()),       # 超窗
        _em_dict(_nohit_row()),     # 无行业命中
    ]
    out = pol.tag_and_dump(raw, days=7)
    assert len(out) == 1                 # 去重后只剩 1 条国内政策
    assert out[0]["url"] == "http://x/1"


def test_hit_filter_can_be_disabled(store_dir):
    raw = [_em_dict(_nohit_row())]
    out = pol.tag_and_dump(raw, days=3650, require_industry_hit=False)
    assert len(out) == 1 and out[0]["industries"] == []


def test_tag_and_dump_records_source_meta(store_dir):
    """tag_and_dump 把命中源写进 store meta.source(默认 eastmoney,可覆盖)。"""
    pol.tag_and_dump([_em_dict(_domestic_row())], days=3650)
    m = store_dir.get_raw_meta("policy", pol._policy_code(_today_str()))
    assert m["source"] == "eastmoney"


# ---------- fetch_policy 端到端(mock akshare)----------

def test_fetch_policy_roundtrip(store_dir, monkeypatch):
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: _fake_df([_domestic_row(kw)]))
    out = pol.fetch_policy(keywords=["半导体 补贴"], days=3650)
    assert len(out) == 1 and out[0]["region"] == "国内"
    # 读盘往返(经 store)
    loaded = pol.load_policy()
    assert loaded == out
    # meta 记录主源命中
    m = store_dir.get_raw_meta("policy", pol._policy_code(_today_str()))
    assert m["source"] == "eastmoney"
    with pytest.raises(FileNotFoundError):
        pol.load_policy("1999-01-01")


def test_fetch_policy_single_keyword_failure_skipped(store_dir, monkeypatch):
    def flaky(kw):
        if kw == "boom":
            raise RuntimeError("接口炸了")
        return _fake_df([_domestic_row(kw)])

    monkeypatch.setattr(pol, "_fetch_em", flaky)
    out = pol.fetch_policy(keywords=["boom", "半导体 补贴"], days=3650)
    assert len(out) == 1        # 失败关键词跳过,好的仍入库


# ---------- 备源 fallback:东财空 → 新闻联播 ----------

def test_fetch_policy_falls_back_to_cctv(store_dir, monkeypatch):
    """东财按全部关键词返回空 → 回落新闻联播,备源出数且经行业打标。"""
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: _fake_df([]))
    today = pd.Timestamp.today().strftime("%Y%m%d")

    def fake_cctv(date):
        # 仅当日联播含命中半导体的宏观条目,其余日期空
        if date == today:
            return _cctv_df([[date, "国家加大半导体产业补贴力度",
                              "发改委部署集成电路专项扶持措施"]])
        return _cctv_df([])

    monkeypatch.setattr(pol, "_fetch_cctv", fake_cctv)
    out = pol.fetch_policy(keywords=["半导体 补贴"], days=7)
    # (a) 备源触发出数
    assert len(out) == 1
    assert out[0]["source"] == "新闻联播"
    # (c) 备源条目也经过了行业打标
    assert "半导体" in out[0]["industries"]
    assert out[0]["region"] == "国内"      # 联播默认国内(无外国主体标记)
    # (b) meta 记录实际命中源为 cctv
    m = store_dir.get_raw_meta("policy", pol._policy_code(_today_str()))
    assert m["source"] == "cctv"


def test_fetch_policy_both_sources_empty_degrades(store_dir, monkeypatch):
    """主源东财 + 备源联播 均无结果 → 降级为空(不再 raise 中止流水线)。"""
    monkeypatch.setattr(pol, "_fetch_em", lambda kw: _fake_df([]))
    monkeypatch.setattr(pol, "_fetch_cctv", lambda date: _cctv_df([]))
    out = pol.fetch_policy(keywords=["半导体 补贴"], days=7)
    assert out == []                                    # 降级为空,数据源无 SLA 不硬抛
