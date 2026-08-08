"""选股结果页单测:达标标注、兜底(无形态视图→按合议排序)、路由渲染。data-independent(monkeypatch)。"""
from fastapi.testclient import TestClient

from tools.analysis import council
from web import data_access as da
from web.app import app

client = TestClient(app)


def _rec(code, bull=True):
    if bull:
        sig = {"trend": {"评级": "偏多", "得分": 80, "依据": ["多头"]},
               "ob_os": {"verdict": "超卖", "resonance": 3},
               "reversal": {"拐点标签": "反弹启动", "拐点评分": 80}}
    else:
        sig = {"trend": {"评级": "偏空", "得分": -80, "依据": ["空头"]},
               "ob_os": {"verdict": "超买", "resonance": 3},
               "reversal": {"拐点标签": "无", "拐点评分": 0}}
    rec = {"schema_version": "1.0",
           "meta": {"code": code, "name": "T" + code, "sector": "半导体", "industry": "芯片", "as_of": "2026-08-08"},
           "snapshot": None, "valuation": None, "fundamental": None, "signals": sig,
           "prediction": None, "sentiment": None, "fundflow": None, "events": [],
           "timeseries_refs": {}, "provenance": {}}
    rec["council"] = council.build_council_block(rec)
    return rec


def _patch_records(monkeypatch, recs):
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")


def test_selection_fallback_no_view_ranks_by_council(monkeypatch):
    """无形态选股 view → 兜底:展示全部记录,按合议综合分降序,qualified=None。"""
    recs = {"000001": _rec("000001", bull=True), "000002": _rec("000002", bull=False)}
    _patch_records(monkeypatch, recs)
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["view_present"] is False and page["total"] == 2
    assert all(r["qualified"] is None for r in page["rows"])       # 无视图 → 达标未知
    # 看多(综合分高)排在看空前
    assert page["rows"][0]["code"] == "000001"
    assert page["rows"][0]["council_score"] >= page["rows"][1]["council_score"]
    assert page["rows"][0]["experts"]                              # 带专家信封供前端重排
    assert page["config"]                                          # 带共享 config(分母模式等)


def test_selection_with_view_marks_qualified(monkeypatch):
    """有形态选股 view → 标达标 + 达标理由(命中形态/正向确认)。"""
    recs = {"000001": _rec("000001", bull=True), "000002": _rec("000002", bull=False)}
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 2, "有效样本": 2, "达标数": 1, "达标占比": 0.5,
            "纪律": "突破不裸用", "RS模式": "单层",
            "达标清单": [{"code": "000001", "命中形态": "杯柄", "正向确认依据": ["净利增速+"]}]}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    page = da.selection_page()
    assert page["view_present"] is True and page["qualified"] == 1
    byc = {r["code"]: r for r in page["rows"]}
    assert byc["000001"]["qualified"] is True
    assert byc["000001"]["达标理由"]["命中形态"] == "杯柄"
    assert byc["000002"]["qualified"] is False and byc["000002"]["达标理由"] is None


def test_daily_section_groups_by_board(monkeypatch):
    """区块②:达标票按行业分组(取自中心记录 meta.industry),每组带合议 + 专家信封。"""
    r1 = _rec("000001", bull=True); r1["meta"]["industry"] = "半导体"
    r2 = _rec("000002", bull=True); r2["meta"]["industry"] = "电力"
    recs = {"000001": r1, "000002": r2}
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 500, "有效样本": 480, "达标数": 2, "达标占比": 0.004,
            "达标清单": [{"code": "000001", "命中形态": "杯柄", "正向确认依据": []},
                        {"code": "000002", "命中形态": "箱体", "正向确认依据": []}]}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    assert d["present"] is True and d["total_scanned"] == 500 and d["board_count"] == 2
    boards = {g["板块"] for g in d["groups"]}
    assert boards == {"半导体", "电力"}
    g0 = d["groups"][0]
    assert g0["rows"][0]["experts"]                              # 带专家信封供前端重排
    assert g0["rows"][0]["council_dir"] in ("看多", "看空", "中性")


def test_daily_section_absent_when_no_view(monkeypatch):
    """区块② 兜底:无 view → present=False、groups 空,不报错。"""
    recs = {"000001": _rec("000001", bull=True)}
    _patch_records(monkeypatch, recs)
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    d = da.selection_page()["daily"]
    assert d["present"] is False and d["groups"] == []


def test_daily_top_n_per_board(monkeypatch):
    """每板块只取 top_n(按合议分)。"""
    recs = {}
    clist = []
    for i in range(8):
        c = f"00010{i}"
        r = _rec(c, bull=True); r["meta"]["industry"] = "半导体"
        recs[c] = r
        clist.append({"code": c, "命中形态": "杯柄", "正向确认依据": []})
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 8, "达标数": 8, "达标清单": clist}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    g = d["groups"][0]
    assert g["板块"] == "半导体" and g["count"] == 8 and len(g["rows"]) == d["top_n"] == 5


def test_selection_route_renders(monkeypatch):
    recs = {"000001": _rec("000001", bull=True)}
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    r = client.get("/selection")
    assert r.status_code == 200
    assert "选股结果" in r.text
    assert "持续关注" in r.text and "每日筛选" in r.text          # 两栏
    assert "/static/council.js" in r.text                         # 复用合议公式
    assert 'id="selBody"' in r.text
    assert "T000001" in r.text                                    # 票名渲染
    assert "待扫描生成" in r.text                                 # 区块② 兜底(无 view)


def test_selection_empty_day_no_crash(monkeypatch):
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["total"] == 0 and page["rows"] == []
