"""news_recall.py 单测(hermetic,不触网,全 mock)。

锁的语义:
- keywords_for:自选池票用手工 sector 出对主题词;全A 票用 baostock 证监会值(mock)
  经子串粗映射出主题词;不含股票名;NEWS_RECALL_KEYWORD_CAP 上限生效;
- recall:mock stock_news_em 按关键词返回假条目 → 去重、cutoff 过滤、cap 控量;单词失败隔离;
- llm_relevance_filter:mock LLM 判定(相关/不相关)→ 只留相关;并行按输入顺序不乱;
  缓存命中不重复调 LLM;LLM 未配置降级返 [];**关思考参数(enable_thinking=False)确被下传**;
- fetch_news(recall=True):三源 + 扩召回并入且去重;recall=False 时**完全不触发**扩召回。
"""
import sys
import types

import pandas as pd
import pytest

from tools.collectors import news as nw
from tools.collectors import news_recall as nr
from tools.config import settings
from tools.store import repo as store


# ————————————————————————————————————————————————
# keywords_for
# ————————————————————————————————————————————————
def _fake_pool_stock(sector: str):
    return types.SimpleNamespace(code="000000", name="某票", industry="x", sector=sector)


def test_keywords_for_pool_sector(monkeypatch):
    """自选池 sector(与 policy._INDUSTRY_TERMS 对齐)→ 直接取该板块主题词,不含股票名。"""
    # sector=AI算力 → 算力/数据中心/AI芯片/...(policy._INDUSTRY_TERMS 种子)
    from tools.config import stock_pool
    monkeypatch.setattr(stock_pool, "get", lambda code: _fake_pool_stock("AI算力"))
    kws = nr.keywords_for("002837", "英维克", nr.stock_industry("002837"))
    assert kws and all(isinstance(k, str) for k in kws)
    assert "算力" in kws
    assert "英维克" not in kws                     # 不把股票名当关键词
    assert len(kws) <= settings.NEWS_RECALL_KEYWORD_CAP


def test_keywords_for_baostock_coarse_map(monkeypatch):
    """全A 票(不在自选池):baostock 证监会门类(mock)经子串粗映射出主题词。"""
    # 行云科技这类:证监会门类给「计算机、通信和其他电子设备制造业」→ 命中「计算机」子串
    industry = "计算机、通信和其他电子设备制造业"
    kws = nr.keywords_for("300209", "行云科技", industry)
    assert "半导体" in kws and "算力" in kws
    assert len(kws) <= settings.NEWS_RECALL_KEYWORD_CAP


def test_keywords_for_cap_enforced(monkeypatch):
    """关键词上限:即便板块主题词很多,也截到 NEWS_RECALL_KEYWORD_CAP。"""
    monkeypatch.setattr(settings, "NEWS_RECALL_KEYWORD_CAP", 3)
    kws = nr.keywords_for("002837", "英维克", "AI算力")
    assert len(kws) == 3


def test_keywords_for_no_industry_returns_empty():
    """无行业(None)或映射不到 → 空(该票不扩召回,宁严)。"""
    assert nr.keywords_for("999999", "某退市股", None) == []
    assert nr.keywords_for("999999", "某零售股", "零售业") == []   # 无子串命中


def test_stock_industry_prefers_pool_then_baostock(monkeypatch):
    """行业源优先级:自选池手工 sector 优先;不在池则回退 baostock(board_of)。"""
    from tools.config import stock_pool
    from tools.collectors import board
    # 自选池命中 → 用手工 sector,不查 baostock
    monkeypatch.setattr(stock_pool, "get", lambda code: _fake_pool_stock("AI算力"))
    monkeypatch.setattr(board, "board_of", lambda code: "不该用到")
    assert nr.stock_industry("002837") == "AI算力"
    # 不在池(get→None)→ 回退 baostock 证监会
    monkeypatch.setattr(stock_pool, "get", lambda code: None)
    monkeypatch.setattr(board, "board_of", lambda code: "软件和信息技术服务业")
    assert nr.stock_industry("300209") == "软件和信息技术服务业"


# ————————————————————————————————————————————————
# recall
# ————————————————————————————————————————————————
def _kw_df(kw: str) -> pd.DataFrame:
    """按关键词返回假东财条目;两个关键词各含一条独有 + 一条重叠(同 url u_dup)。"""
    return pd.DataFrame({
        "新闻标题": [f"{kw}相关新闻", "重叠新闻", f"{kw}旧闻"],
        "新闻内容": ["c", "cdup", "cold"],
        "发布时间": ["2026-08-10 10:00:00", "2026-08-09 09:00:00", "2000-01-01 09:00:00"],
        "文章来源": ["s", "s", "s"],
        "新闻链接": [f"u_{kw}", "u_dup", f"u_old_{kw}"],
    })


def test_recall_dedup_cutoff_cap(monkeypatch):
    """多关键词召回:同 url 去重、cutoff 丢旧、cap 控量;命中条挂 _recall_kw。"""
    monkeypatch.setattr(nr, "_fetch_em_kw", _kw_df)
    out = nr.recall(["算力", "数据中心"], cutoff="2026-08-01", cap=30)
    urls = [it["url"] for it in out]
    assert urls.count("u_dup") == 1                 # 两关键词的重叠条只留一条
    assert "u_算力" in urls and "u_数据中心" in urls  # 各关键词独有条都在
    assert all("2000" not in it["time"] for it in out)   # cutoff 丢掉 2000 旧闻
    assert all("_recall_kw" in it for it in out)    # 命中关键词已标注
    assert out[0]["time"] >= out[-1]["time"]        # 倒序


def test_recall_cap_limits(monkeypatch):
    monkeypatch.setattr(nr, "_fetch_em_kw", _kw_df)
    out = nr.recall(["算力", "数据中心"], cutoff="2026-08-01", cap=1)
    assert len(out) == 1


def test_recall_keyword_failure_isolated(monkeypatch):
    """单关键词抛异常隔离跳过,其余关键词结果仍返回。"""
    def _flaky(kw):
        if kw == "算力":
            raise ConnectionError("东财挂了")
        return _kw_df(kw)
    monkeypatch.setattr(nr, "_fetch_em_kw", _flaky)
    out = nr.recall(["算力", "数据中心"], cutoff="2026-08-01", cap=30)
    urls = {it["url"] for it in out}
    assert "u_数据中心" in urls and "u_算力" not in urls


# ————————————————————————————————————————————————
# llm_relevance_filter
# ————————————————————————————————————————————————
class _FakeExtractClient:
    """按标题里的标记返回 相关/不相关,并计 extract 调用次数(验缓存命中)。"""
    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, *, instruction, temperature=0.0):
        self.calls += 1
        return {"相关": "相关" if "关联" in text else "不相关"}


def _cands():
    return [
        {"title": "关联本股利好", "content": "x", "time": "2026-08-10 10:00", "url": "u1"},
        {"title": "泛大盘综述", "content": "y", "time": "2026-08-10 09:00", "url": "u2"},
        {"title": "关联上下游", "content": "z", "time": "2026-08-09 09:00", "url": "u3"},
    ]


def test_relevance_filter_keeps_only_related(monkeypatch, tmp_path):
    """只留 LLM 判「相关」的条目,顺序保持;并行不乱序。"""
    monkeypatch.setattr(settings, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(settings, "LLM_EXTRACT_WORKERS", 4)   # 走并行分支
    cli = _FakeExtractClient()
    out = nr.llm_relevance_filter(_cands(), "英维克", "AI算力", client=cli)
    assert [it["url"] for it in out] == ["u1", "u3"]          # 相关的两条,原序


def test_relevance_filter_cache_hit_no_recall(monkeypatch, tmp_path):
    """相同 (指令+文本) 第二次命中文件缓存,不再调 LLM。"""
    monkeypatch.setattr(settings, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(settings, "LLM_EXTRACT_WORKERS", 1)   # 串行,便于计数
    cli = _FakeExtractClient()
    nr.llm_relevance_filter(_cands(), "英维克", "AI算力", client=cli)
    first = cli.calls
    assert first == 3
    nr.llm_relevance_filter(_cands(), "英维克", "AI算力", client=cli)
    assert cli.calls == first                                # 全部命中缓存,无新增调用


def test_relevance_filter_llm_not_configured_degrades(monkeypatch):
    """LLM 未配置且未显式传 client → 返回 [](宁严:不放行未筛候选)。"""
    from tools.llm import client as lc
    monkeypatch.setattr(lc, "is_configured", lambda: False)
    assert nr.llm_relevance_filter(_cands(), "英维克", "AI算力", client=None) == []


def test_relevance_filter_passes_disable_thinking(monkeypatch, tmp_path):
    """关思考参数确被下传:真 OpenAICompatClient 在 LLM_DISABLE_THINKING 时
    走 extra_body={'enable_thinking': False} 到底层 create()。"""
    monkeypatch.setattr(settings, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(settings, "LLM_EXTRACT_WORKERS", 1)
    monkeypatch.setattr(settings, "LLM_DISABLE_THINKING", True)
    from tools.llm import client as lc

    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)
        msg = types.SimpleNamespace(content='{"相关": "不相关"}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    # 绕开 __init__(不建真 OpenAI 连接),直接装底层 _cli
    cli = lc.OpenAICompatClient.__new__(lc.OpenAICompatClient)
    cli.model = "test-model"
    cli._cli = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_fake_create)))

    nr.llm_relevance_filter(_cands()[:1], "英维克", "AI算力", client=cli)
    assert captured.get("extra_body") == {"enable_thinking": False}


# ————————————————————————————————————————————————
# fetch_news(recall=...)
# ————————————————————————————————————————————————
def test_fetch_news_recall_true_merges_and_dedups(monkeypatch, tmp_path):
    """recall=True:三源并集 + 扩召回相关条并入,去重;meta.source 含「扩召回」。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    em_df = pd.DataFrame({
        "新闻标题": ["东财个股新闻"], "新闻内容": ["c"],
        "发布时间": ["2026-08-09 10:00:00"], "文章来源": ["em"], "新闻链接": ["u_em"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df,
                                 stock_info_global_cls=lambda: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])

    called = {"n": 0}

    def _fake_recall(code, name, cutoff, client=None):
        called["n"] += 1
        return [
            {"title": "扩召回行业消息", "content": "r", "time": "2026-08-10 11:00:00",
             "source": "扩召回:算力", "url": "u_recall"},
            {"title": "与东财重叠", "content": "c", "time": "2026-08-09 10:00:00",
             "source": "扩召回:算力", "url": "u_em"},        # 与东财 u_em 重叠 → 去重
        ]
    monkeypatch.setattr(nr, "recall_related", _fake_recall)

    items = nw.fetch_news(["002837"], days=30, recall=True)["002837"]
    assert called["n"] == 1                                  # 扩召回被触发一次
    urls = [it["url"] for it in items]
    assert urls.count("u_em") == 1                           # 重叠去重(留东财先到者)
    assert "u_recall" in urls                                # 扩召回独有条并入
    dup = next(it for it in items if it["url"] == "u_em")
    assert dup["source"] == "em"                             # 主源(东财)在前,先到者留
    assert "扩召回" in store.get_raw_meta("news", "002837")["source"]


def test_fetch_news_recall_false_never_triggers(monkeypatch, tmp_path):
    """recall=False:扩召回**完全不触发**(计数桩断言),范围不波及全A。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    em_df = pd.DataFrame({
        "新闻标题": ["东财个股新闻"], "新闻内容": ["c"],
        "发布时间": ["2026-08-09 10:00:00"], "文章来源": ["em"], "新闻链接": ["u_em"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df,
                                 stock_info_global_cls=lambda: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])

    called = {"n": 0}

    def _spy(code, name, cutoff, client=None):
        called["n"] += 1
        return []
    monkeypatch.setattr(nr, "recall_related", _spy)

    items = nw.fetch_news(["002837"], days=30, recall=False)["002837"]
    assert called["n"] == 0                                  # 未触发扩召回
    assert [it["url"] for it in items] == ["u_em"]
    assert "扩召回" not in (store.get_raw_meta("news", "002837")["source"])


def test_fetch_news_recall_default_from_settings(monkeypatch, tmp_path):
    """recall=None → 取 settings.NEWS_RECALL_ENABLED;默认关时不触发。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(settings, "NEWS_RECALL_ENABLED", False)
    em_df = pd.DataFrame({
        "新闻标题": ["东财个股新闻"], "新闻内容": ["c"],
        "发布时间": ["2026-08-09 10:00:00"], "文章来源": ["em"], "新闻链接": ["u_em"],
    })
    fake = types.SimpleNamespace(stock_news_em=lambda symbol: em_df,
                                 stock_info_global_cls=lambda: pd.DataFrame())
    monkeypatch.setitem(sys.modules, "akshare", fake)
    monkeypatch.setattr(nw, "_fetch_sina", lambda code, cutoff: [])

    called = {"n": 0}
    monkeypatch.setattr(nr, "recall_related",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [])

    nw.fetch_news(["002837"], days=30)          # recall 缺省 → 读 settings(False)
    assert called["n"] == 0
