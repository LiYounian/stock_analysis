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


# 落在窗口内的近期时间戳:用相对日期(昨天),避免硬编码日期随 today 推移滑出 days 窗口
# 导致窗口/去重/命中过滤用例误报(cutoff = today - days,写死日期迟早 < cutoff 被丢)。
def _recent_ts(hour=9):
    return (pd.Timestamp.today() - pd.Timedelta(days=1)).strftime(f"%Y-%m-%d {hour:02d}:00:00")


def _domestic_row(kw="半导体 补贴"):
    return [kw, "国家出台半导体产业补贴新政", "发改委发布集成电路专项补贴政策",
            _recent_ts(9), "证券时报", "http://x/1"]


def _foreign_row(kw="出口管制"):
    return [kw, "美国收紧对华芯片出口管制", "美国商务部BIS更新半导体出口管制清单",
            _recent_ts(10), "财联社", "http://x/2"]


def _old_row(kw="芯片"):
    return [kw, "很旧的芯片政策", "2000年的芯片补贴", "2000-01-01 08:00:00",
            "旧报", "http://x/old"]


def _nohit_row(kw="政策"):
    return [kw, "耶路撒冷地位争议", "与票池行业无关的国际政治新闻",
            _recent_ts(11), "某报", "http://x/3"]


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


def _pool(*sectors):
    """按给定 sector 造一个假票池(只用于 default_keywords 的派生逻辑测试)。"""
    from tools.config.stock_pool import Stock
    return [Stock(f"00000{i}", "n", "x", sec) for i, sec in enumerate(sectors)]


def test_default_keywords_survives_sector_vocab_drift(monkeypatch):
    """语义锁:票池 sector 与 _INDUSTRY_TERMS key 用**不同措辞**指同一行业时,仍须派生出该行业检索词。

    二者是两套各自演进的自由文本词表。若池过滤退化成字符串直等,行业词会被**整组静默丢弃**
    (不报错、不告警),表现为该板块的政策/产业消息永远检索不到。此处的池措辞与表 key **无一字面
    相同**,只有经 industry_map.to_sw 归申万一级才对得上——直等实现必然跑挂本用例。
    """
    monkeypatch.setattr(pol.stock_pool, "get_pool",
                        lambda: _pool("光模块", "光学元件", "光伏"))
    kws = pol.default_keywords()
    joined = " ".join(kws)
    # 光模块→通信 ⇒ 光通信组;光学元件→电子 ⇒ 半导体/电子元件/消费电子组;光伏→电力设备 ⇒ 新能源材料组
    for term in ("光通信", "半导体", "新能源"):
        assert term in joined, f"{term} 未派生(池过滤疑似退回字符串直等)"


def test_default_keywords_excludes_sectors_outside_pool(monkeypatch):
    """语义锁:归一后取交集 ≠ 全都要——池外行业不得混进检索词。

    防「修漂移」时矫枉过正改成不过滤,导致关键词爆炸(每词一次网络请求 + 每条一次 LLM 打分)。
    """
    monkeypatch.setattr(pol.stock_pool, "get_pool", lambda: _pool("光模块"))
    kws = pol.default_keywords()
    joined = " ".join(kw for kw in kws if kw not in pol._MACRO_TERMS)
    assert "光通信" in joined                       # 池内(光模块→通信)
    for term in ("机器人", "电价", "锂电"):          # 池外:机械设备 / 公用事业 / 电力设备
        assert term not in joined, f"{term} 属池外行业,不应出现"


def test_default_keywords_tolerates_unmappable_sector(monkeypatch):
    """语义锁:归不到申万一级的 sector(to_sw → None)只被跳过,不得崩、不得污染交集。

    票池里存在「大模型」「新材料」这类自由文本,to_sw 认不得。宁可漏一个行业也不硬凑错映射。
    """
    monkeypatch.setattr(pol.stock_pool, "get_pool", lambda: _pool("大模型", "新材料", "光模块"))
    kws = pol.default_keywords()                    # 不抛异常
    assert "光通信 政策" in kws                      # 可归一的那个仍生效
    assert len(kws) == len(set(kws))


def test_default_keywords_emits_no_bare_industry_terms(monkeypatch):
    """语义锁:检索词只有「行业词 × 政策词」组合 + 宏观独立词,**不含裸行业词**。

    裸行业词曾作为「组合词接不住无政策词的产业/盘面快讯」的对策被提出,但实测证伪:
    东财关键词检索是模糊匹配,组合词并不排斥无政策词的条目,裸词边际贡献极低。
    保留一个设计理由已被证伪的改动会误导后来者,故砍掉。日后若要扩召回,应基于关键词
    产出率数据决策,而不是重新凭这条直觉加回来。
    """
    monkeypatch.setattr(pol.stock_pool, "get_pool", lambda: _pool("光模块", "光学元件"))
    kws = pol.default_keywords()
    bare = [kw for kw in kws if " " not in kw and kw not in pol._MACRO_TERMS]
    assert bare == [], f"出现裸行业词 {bare}"


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
