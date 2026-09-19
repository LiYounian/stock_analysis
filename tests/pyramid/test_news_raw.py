"""W1 · news_raw 语义锁测试（守则6）。

锁死"为什么改"：
  · 返回条目含 source/url/摘要（辩证查证"看来源"的物质基础，去掉即回退）；
  · benefit_label 三值原样透传、兜底 news 源无标签标"未标注"（绝不臆测正负）；
  · 防未来按 publish 日期 ≤ as_of 滤（post-date 条被剔除）；
  · 三态缺数据 missing/空list/有条 各自 freshness（样本无≠缺失）；
  · 浓缩块 ≤8 行（G3·紧凑不塞 url）；
  · CLI --json 能 dump 全量 fields（下钻拿 url/source 的能力，废了则下钻无效）。
"""
import json
import os

from tools.pyramid.registry import get, all_names
import tools.pyramid.tools  # noqa: F401 触发注册
from tools.pyramid import __main__ as cli

AS_OF = "2026-09-18"


def _write(root, rel, obj):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
    return p


def _baidu(root, code, items):
    return _write(root, f"data/raw/{AS_OF}/baidu_news/{code}.json", items)


def _news(root, code, items):
    return _write(root, f"data/raw/{AS_OF}/news/{code}.json", items)


# ── 注册契约 ──
def test_已注册():
    assert "news_raw" in all_names()
    t = get("news_raw")
    assert t.塔层 == "②消息"
    assert t.面 == "消息情绪面"


# ── 返回含 source/url/摘要（"看来源"的物质基础）──
def test_返回条目含source_url_摘要(tmp_path):
    root = str(tmp_path)
    _baidu(root, "300001", [
        {"title": "利好标题A", "source": "证券之星", "publish_time": "2026-09-17 10:00:00",
         "publish_ts": 1789000000, "benefit_label": "利好", "abstract": "摘要正文A", "url": "http://x/a"},
    ])
    r = get("news_raw").run(AS_OF, "300001", root=root)
    n = r.fields["news"]
    assert len(n) == 1
    assert n[0]["来源"] == "证券之星"
    assert n[0]["url"] == "http://x/a"        # url 原样保留（下钻辨真假）
    assert n[0]["摘要"] == "摘要正文A"


# ── benefit_label 三值原样透传 ──
def test_benefit_label三值原样(tmp_path):
    root = str(tmp_path)
    _baidu(root, "300002", [
        {"title": "好", "source": "s", "publish_time": "2026-09-17 10:00:00", "benefit_label": "利好", "url": "u1"},
        {"title": "空", "source": "s", "publish_time": "2026-09-16 10:00:00", "benefit_label": "利空", "url": "u2"},
        {"title": "平", "source": "s", "publish_time": "2026-09-15 10:00:00", "benefit_label": "中性", "url": "u3"},
    ])
    r = get("news_raw").run(AS_OF, "300002", root=root)
    assert (r.fields["利好"], r.fields["利空"], r.fields["中性"]) == (1, 1, 1)
    assert "[利好]" in r.浓缩块 and "[利空]" in r.浓缩块  # 标签原样进浓缩块，未被改写


# ── 防未来：publish 日期 > as_of 的条目被剔除 ──
def test_防未来_post_date滤除(tmp_path):
    root = str(tmp_path)
    _baidu(root, "300003", [
        {"title": "当日", "source": "s", "publish_time": f"{AS_OF} 09:00:00", "benefit_label": "利好", "url": "u"},
        {"title": "未来", "source": "s", "publish_time": "2026-09-25 09:00:00", "benefit_label": "利好", "url": "u"},
    ])
    r = get("news_raw").run(AS_OF, "300003", root=root)
    assert r.fields["条数"] == 1           # 未来条被防未来滤除
    assert all("未来" not in it["标题"] for it in r.fields["news"])


# ── 三态缺数据 ──
def test_三态_missing(tmp_path):
    r = get("news_raw").run(AS_OF, "999999", root=str(tmp_path))  # 两源皆无
    assert r.freshness == "missing"
    assert r.fields["条数"] == 0 and r.fields["source_used"] is None
    assert "无新闻覆盖" in r.浓缩块        # 诚实：不编造


def test_三态_空list_已落盘0条(tmp_path):
    root = str(tmp_path)
    _baidu(root, "300004", [])            # 文件在但 0 条
    r = get("news_raw").run(AS_OF, "300004", root=root)
    assert r.freshness == "fresh"          # 样本无 ≠ 缺失
    assert r.fields["条数"] == 0 and r.fields["source_used"] == "baidu_news"


def test_三态_有条_fresh(tmp_path):
    root = str(tmp_path)
    _baidu(root, "300005", [
        {"title": "t", "source": "s", "publish_time": "2026-09-17 10:00:00", "benefit_label": "利好", "url": "u"}])
    r = get("news_raw").run(AS_OF, "300005", root=root)
    assert r.freshness == "fresh" and r.fields["条数"] == 1


# ── 兜底 news 源：无标签标"未标注"、摘要用 content ──
def test_兜底news源无标签标未标注(tmp_path):
    root = str(tmp_path)
    _news(root, "300006", [
        {"title": "兜底标题", "content": "兜底正文", "time": "2026-09-17 12:00:00", "source": "新浪", "url": "http://n/1"}])
    r = get("news_raw").run(AS_OF, "300006", root=root)  # 无 baidu → 走兜底
    assert r.fields["source_used"] == "news"
    assert r.fields["news"][0]["标签"] == "未标注"       # 兜底无 benefit_label 不臆测正负
    assert r.fields["news"][0]["摘要"] == "兜底正文"      # 摘要用 content
    assert r.fields["news"][0]["url"] == "http://n/1"


# ── G3：浓缩块 ≤8 行（多条也不膨胀）──
def test_浓缩块_le8行(tmp_path):
    root = str(tmp_path)
    items = [{"title": f"t{i}", "source": "s", "publish_time": f"2026-09-1{i%9} 10:00:00",
              "benefit_label": "利好", "url": f"u{i}"} for i in range(20)]
    _baidu(root, "300007", items)
    r = get("news_raw").run(AS_OF, "300007", root=root)
    assert len([ln for ln in r.浓缩块.splitlines() if ln.strip()]) <= 8
    assert r.max_浓缩块_行 == 8            # 未偷抬 G3 上限
    assert r.fields["条数"] == 20          # 但 fields 保全量（浓缩块只显 TOP_N）


# ── CLI --json：dump 全量 fields（下钻拿 url/source 的能力锁）──
def test_cli_json_dump_全量含url(tmp_path, capsys):
    root = str(tmp_path)
    _baidu(root, "300008", [
        {"title": "t", "source": "证券之星", "publish_time": "2026-09-17 10:00:00",
         "benefit_label": "利好", "abstract": "摘要", "url": "http://x/deep"}])
    rc = cli.main(["tool", "news_raw", "--as-of", AS_OF, "--code", "300008", "--data-root", root, "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)   # --json 输出须是合法 json，非 8 行浓缩块
    assert out["name"] == "news_raw"
    assert out["fields"]["news"][0]["url"] == "http://x/deep"   # 下钻能真拿到 url
    assert out["fields"]["news"][0]["来源"] == "证券之星"


# ── 真实数据 smoke（有票·防回归）──
def test_真实数据_smoke():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    real = os.path.join(root, "data", "raw", AS_OF, "baidu_news", "000026.json")
    if not os.path.exists(real):
        import pytest
        pytest.skip("真实 baidu_news 未落盘")
    r = get("news_raw").run(AS_OF, "000026", root=root)
    assert r.freshness == "fresh" and r.fields["条数"] > 0
    assert r.fields["news"][0]["url"]           # 真实条目有 url
