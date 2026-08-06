"""ugc.py 单测(mock 东财股吧 HTML,不触网)。

锁语义:article_list 解析(字段归一/两处作者兜底/is_v/时间倒序/空帖过滤)、
热度计算(post_count/v_ratio/reply_total/heat_score 公式)、
空数据抛错、落盘读盘往返。
"""
import json

import pytest

from tools.collectors import ugc


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


def test_heat_empty():
    h = ugc._heat([])
    assert h == {"post_count": 0, "v_ratio": 0.0, "reply_total": 0, "heat_score": 0.0}


def test_heat_handles_missing_fields():
    # 缺字段不崩,当 0 处理
    h = ugc._heat([{"is_v": False}, {}])
    assert h["post_count"] == 2 and h["reply_total"] == 0 and h["v_ratio"] == 0.0


# ============ 取数 / 落盘 / 读盘 ============
def test_fetch_one_empty_raises(monkeypatch):
    monkeypatch.setattr(ugc, "_http_get", lambda code: _fake_html([]))
    with pytest.raises(ValueError):
        ugc.fetch_one("600519")


def test_fetch_and_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(ugc, "_UGC_DIR", tmp_path)
    monkeypatch.setattr(ugc, "_ugc_path", lambda code: tmp_path / f"{code}.json")
    html = _fake_html([
        _hot_post("大V看多", 50, 300, "2026-08-06 10:00:00", user_v=1),
        _plain_post("散户跟风", 5, 10, "2026-08-06 09:00:00"),
    ])
    monkeypatch.setattr(ugc, "_http_get", lambda code: html)

    out = ugc.fetch_ugc(["600519"])
    assert "600519" in out and len(out["600519"]) == 2

    loaded = ugc.load_ugc("600519")
    assert loaded[0]["author"] == "大V看多" or loaded[0]["is_v"] is True

    # compute_heat 走缓存读盘
    h = ugc.compute_heat("600519")
    assert h["post_count"] == 2 and h["v_ratio"] == 0.5

    with pytest.raises(FileNotFoundError):
        ugc.load_ugc("999999")


def test_fetch_ugc_failure_isolated(monkeypatch, tmp_path):
    """单票抓取抛错不中断整批,失败票不入结果。"""
    monkeypatch.setattr(ugc, "_UGC_DIR", tmp_path)
    monkeypatch.setattr(ugc, "_ugc_path", lambda code: tmp_path / f"{code}.json")

    def _fake_get(code):
        if code == "000000":
            raise RuntimeError("网络炸了")
        return _fake_html([_plain_post("OK", 1, 1, "2026-08-06 10:00:00")])

    monkeypatch.setattr(ugc, "_http_get", _fake_get)
    out = ugc.fetch_ugc(["000000", "600519"])
    assert "000000" not in out and "600519" in out


def test_fetch_ugc_limit_passthrough(monkeypatch, tmp_path):
    monkeypatch.setattr(ugc, "_UGC_DIR", tmp_path)
    monkeypatch.setattr(ugc, "_ugc_path", lambda code: tmp_path / f"{code}.json")
    html = _fake_html([_plain_post(f"帖{i}", 0, 0, f"2026-08-0{i} 09:00:00")
                       for i in range(1, 6)])
    monkeypatch.setattr(ugc, "_http_get", lambda code: html)
    out = ugc.fetch_ugc(["600519"], limit=3)
    assert len(out["600519"]) == 3
