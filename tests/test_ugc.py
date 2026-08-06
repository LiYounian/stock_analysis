"""ugc.py 单测(mock 东财股吧 HTML,不触网)。

锁语义:article_list 解析(字段归一/两处作者兜底/is_v/时间倒序/空帖过滤)、
时间窗过滤(早于 today-NEWS_LOOKBACK_DAYS 的帖被丢弃、无年份/解析不出的策略)、
热度计算(post_count/v_ratio/reply_total/heat_score 公式 + heat_per_day 日均归一)、
空数据抛错、经 store 落盘读盘往返 + 采集元数据 source。
"""
import json
from datetime import date, timedelta

import pytest

from tools.collectors import ugc
from tools.config import settings
from tools.store import repo


# —— 把 store 的 raw 路径根 monkeypatch 到临时目录(参考 tests/test_store.py)——
@pytest.fixture
def store_tmp(tmp_path, monkeypatch):
    """store 落盘指向 tmp,绝不污染真实 data/raw;返回 repo 模块。"""
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(repo, "_RAW_DIR", raw)
    return repo


# —— 构造一段东财股吧列表页 HTML(含 var article_list 内联 JSON)——
def _fake_html(posts):
    return (
        "<!DOCTYPE html><html><body>"
        "<script>var article_list = "
        + json.dumps({"re": posts, "count": len(posts)}, ensure_ascii=False)
        + ";</script></body></html>"
    )


def _hot_post(title, replies, likes, t, user_v=0):
    """热帖结构:作者在 post_user 子对象,带 user_v。"""
    return {
        "post_title": title,
        "post_content": "",
        "post_comment_count": replies,
        "post_like_count": likes,
        "post_publish_time": t,
        "post_user": {"user_nickname": "大V老张", "user_v": user_v},
    }


def _plain_post(title, replies, likes, t, v_user_code=0):
    """普通帖结构:作者在顶层 user_nickname,V 标记在 v_user_code。"""
    return {
        "post_title": title,
        "post_comment_count": replies,
        "post_like_count": likes,
        "post_publish_time": t,
        "post_user": None,
        "user_nickname": "散户小王",
        "v_user_code": v_user_code,
    }


# ============ 解析 ============
def test_parse_normalizes_and_sorts():
    html = _fake_html([
        _plain_post("旧帖", 1, 2, "2026-08-01 09:00:00"),
        _hot_post("大V帖", 100, 500, "2026-08-06 10:00:00", user_v=1),
    ])
    items = ugc._parse(html)
    assert len(items) == 2
    # 时间倒序:新的在前
    assert items[0]["time"] > items[1]["time"]
    # 字段契约
    assert set(items[0].keys()) == {"time", "author", "is_v", "text", "likes", "replies"}
    # 大V帖:post_user.user_v=1 → is_v True,作者取 post_user
    assert items[0]["is_v"] is True and items[0]["author"] == "大V老张"
    assert items[0]["replies"] == 100 and items[0]["likes"] == 500
    # 普通帖:顶层作者,v_user_code=0 → 非V
    assert items[1]["is_v"] is False and items[1]["author"] == "散户小王"


def test_parse_plain_post_v_flag_from_top_level():
    html = _fake_html([_plain_post("加V散户", 3, 4, "2026-08-06 11:00:00", v_user_code=1)])
    items = ugc._parse(html)
    assert items[0]["is_v"] is True


def test_parse_uses_content_over_title_and_filters_empty():
    posts = [
        {"post_title": "标题T", "post_content": "正文C",
         "post_publish_time": "2026-08-06 12:00:00", "post_user": None,
         "post_comment_count": 0, "post_like_count": 0},
        {"post_title": "", "post_content": "",  # 空帖应被过滤
         "post_publish_time": "2026-08-06 13:00:00", "post_user": None},
    ]
    items = ugc._parse(_fake_html(posts))
    assert len(items) == 1
    assert items[0]["text"] == "正文C"       # 正文优先于标题


def test_parse_limit_and_no_marker():
    html = _fake_html([_plain_post(f"帖{i}", 0, 0, f"2026-08-0{i} 09:00:00")
                       for i in range(1, 6)])
    assert len(ugc._parse(html, limit=2)) == 2
    # 无 article_list 锚点 / 坏 JSON → 空列表,不崩
    assert ugc._parse("<html>no data</html>") == []
    assert ugc._parse("<script>var article_list = {bad;</script>") == []


# ============ 时间窗过滤 ============
def test_post_date_parsing_strategies():
    """带年份取前10位;无年份补最近年份;解析不出返回 None(供保留)。"""
    assert ugc._post_date("2025-12-31 09:00:00") == "2025-12-31"
    # 无年份 MM-DD:补当前年;跨年边界回退去年 → 落在过去一年内、不为未来
    mmdd = ugc._post_date("01-15 08:00")
    assert mmdd is not None and mmdd[5:] == "01-15"
    assert mmdd <= date.today().isoformat()      # 绝不解析成未来
    # 解析不出 → None
    assert ugc._post_date("") is None
    assert ugc._post_date("刚刚") is None
    assert ugc._post_date("2小时前") is None


def test_filter_recent_drops_old_keeps_unparseable():
    cutoff = "2026-07-30"
    posts = [
        {"time": "2026-08-05 09:00:00"},   # 窗口内 → 留
        {"time": "2026-07-01 09:00:00"},   # 早于 cutoff → 丢
        {"time": "刚刚"},                   # 解析不出 → 宁保留勿误删
    ]
    kept = ugc._filter_recent(posts, cutoff)
    times = [p["time"] for p in kept]
    assert "2026-08-05 09:00:00" in times
    assert "刚刚" in times
    assert "2026-07-01 09:00:00" not in times


def test_fetch_one_filters_out_of_window(monkeypatch):
    """(a) 早于时间窗的帖子被过滤掉:构造 today 与 today-30d 两帖,只留近的。"""
    today = date.today()
    recent = today.strftime("%Y-%m-%d 10:00:00")
    old = (today - timedelta(days=settings.NEWS_LOOKBACK_DAYS + 30)).strftime(
        "%Y-%m-%d 10:00:00")
    html = _fake_html([
        _plain_post("窗口内新帖", 1, 1, recent),
        _plain_post("超窗旧帖", 1, 1, old),
    ])
    monkeypatch.setattr(ugc, "_http_get", lambda code: html)
    items = ugc.fetch_one("600519")
    assert len(items) == 1
    assert items[0]["time"] == recent


# ============ 热度计算(纯函数)============
def test_heat_math():
    posts = [
        {"is_v": True, "replies": 10, "likes": 100},
        {"is_v": False, "replies": 20, "likes": 200},
        {"is_v": False, "replies": 0, "likes": 0},
        {"is_v": True, "replies": 0, "likes": 0},
    ]
    h = ugc._heat(posts)
    assert h["post_count"] == 4
    assert h["v_ratio"] == 0.5                       # 2/4
    assert h["reply_total"] == 30                     # 10+20
    # heat = (4 + 0.5*30 + 0.2*300) * (1+0.5) = (4+15+60)*1.5 = 79*1.5 = 118.5
    assert h["heat_score"] == 118.5
    # 无可解析日期(帖子无 time 字段)→ 跨度退化为 1 → heat_per_day == heat_score
    assert h["heat_per_day"] == 118.5


def test_heat_empty():
    h = ugc._heat([])
    assert h == {"post_count": 0, "v_ratio": 0.0, "reply_total": 0,
                 "heat_score": 0.0, "heat_per_day": 0.0}


def test_heat_handles_missing_fields():
    # 缺字段不崩,当 0 处理
    h = ugc._heat([{"is_v": False}, {}])
    assert h["post_count"] == 2 and h["reply_total"] == 0 and h["v_ratio"] == 0.0


def test_heat_per_day_normalizes_across_days():
    """(b) heat_per_day 存在且多帖跨多天时 < heat_score(日均归一生效)。"""
    posts = [
        {"is_v": False, "replies": 10, "likes": 0, "time": "2026-08-01 09:00:00"},
        {"is_v": False, "replies": 10, "likes": 0, "time": "2026-08-05 09:00:00"},
    ]
    h = ugc._heat(posts)
    # 跨度 = (08-05 - 08-01) + 1 = 5 天 → heat_per_day = heat_score / 5 < heat_score
    assert "heat_per_day" in h
    assert h["heat_score"] > 0
    assert h["heat_per_day"] < h["heat_score"]
    assert h["heat_per_day"] == round(h["heat_score"] / 5, 2)


# ============ 取数 / 落盘 / 读盘(经 store)============
def test_fetch_one_empty_raises(monkeypatch):
    monkeypatch.setattr(ugc, "_http_get", lambda code: _fake_html([]))
    with pytest.raises(ValueError):
        ugc.fetch_one("600519")


def test_fetch_and_load_roundtrip(monkeypatch, store_tmp):
    # cutoff 拉到很早,隔离机器日期漂移(本用例只验往返,不验时间窗)
    monkeypatch.setattr(ugc, "_cutoff", lambda days=None: "1970-01-01")
    html = _fake_html([
        _hot_post("大V看多", 50, 300, "2026-08-06 10:00:00", user_v=1),
        _plain_post("散户跟风", 5, 10, "2026-08-06 09:00:00"),
    ])
    monkeypatch.setattr(ugc, "_http_get", lambda code: html)

    out = ugc.fetch_ugc(["600519"])
    assert "600519" in out and len(out["600519"]) == 2

    loaded = ugc.load_ugc("600519")
    assert loaded[0]["author"] == "大V看多" or loaded[0]["is_v"] is True

    # (c) 采集元数据 source 经 store 旁写
    assert store_tmp.get_raw_meta("ugc", "600519")["source"] == "eastmoney_guba"

    # compute_heat 走缓存读盘
    h = ugc.compute_heat("600519")
    assert h["post_count"] == 2 and h["v_ratio"] == 0.5

    with pytest.raises(FileNotFoundError):
        ugc.load_ugc("999999")


def test_fetch_ugc_failure_isolated(monkeypatch, store_tmp):
    """单票抓取抛错不中断整批,失败票不入结果。"""
    monkeypatch.setattr(ugc, "_cutoff", lambda days=None: "1970-01-01")

    def _fake_get(code):
        if code == "000000":
            raise RuntimeError("网络炸了")
        return _fake_html([_plain_post("OK", 1, 1, "2026-08-06 10:00:00")])

    monkeypatch.setattr(ugc, "_http_get", _fake_get)
    out = ugc.fetch_ugc(["000000", "600519"])
    assert "000000" not in out and "600519" in out


def test_fetch_ugc_limit_passthrough(monkeypatch, store_tmp):
    monkeypatch.setattr(ugc, "_cutoff", lambda days=None: "1970-01-01")
    html = _fake_html([_plain_post(f"帖{i}", 0, 0, f"2026-08-0{i} 09:00:00")
                       for i in range(1, 6)])
    monkeypatch.setattr(ugc, "_http_get", lambda code: html)
    out = ugc.fetch_ugc(["600519"], limit=3)
    assert len(out["600519"]) == 3
