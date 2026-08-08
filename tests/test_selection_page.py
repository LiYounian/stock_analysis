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


def test_near_miss_dict_shape_flattened_not_crash(monkeypatch):
    """回归:screen_pattern 的「接近达标」真实形状是 {板块:[items]} 字典,selection_page 须拍平。

    此前 _top_picks/_daily_sections 按扁平列表假设写,遇字典会 AttributeError('str' has no 'get')炸整页。
    锁死:字典形状下 selection_page 正常返回,top_picks 为列表且含接近达标票。
    """
    recs = {"000001": _rec("000001", bull=True)}
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 5539, "达标数": 1,
            "达标清单": [{"code": "000001", "行业": "芯片", "命中形态": "箱体", "正向确认依据": []}],
            "接近达标": {                                   # ← 字典:板块→列表(真实形状)
                "电子": [{"code": "300001", "行业": "电子", "最接近形态": ["杯柄"], "差距说明": "待突破", "合议分": 0.2}],
                "银行": [{"code": "600000", "行业": "银行", "最接近形态": ["旗形"], "差距说明": "待放量", "合议分": 0.1}]}}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    page = da.selection_page()                              # 不得抛异常
    assert isinstance(page["top_picks"], list)
    picks = {p["code"] for p in page["top_picks"]}
    assert "000001" in picks and ("300001" in picks or "600000" in picks)   # 达标∪接近∪自选 都进候选


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


def test_daily_near_when_zero_qualified(monkeypatch):
    """达标0但有接近达标 → mode=near、按板块分组(组内合议分降序、板块按最高分降序),不空页。"""
    recs = {}  # 全A票可无中心记录:合议分取契约自带
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 4800, "有效样本": 4600, "达标数": 0, "达标清单": [],
            "接近达标": [
                {"code": "000001", "行业": "半导体", "最接近形态": "杯柄", "差距说明": "颈线差1.2%", "合议分": 60},
                {"code": "000002", "行业": "半导体", "最接近形态": "箱体", "差距说明": "量能不足", "合议分": 80},
                {"code": "000003", "行业": "电力", "最接近形态": "旗形", "差距说明": "回踩未确认", "合议分": 90},
            ]}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    assert d["present"] is True and d["mode"] == "near"
    assert d["qualified_n"] == 0 and d["near_n"] == 3 and d["top_n"] == 3
    # 电力(90)组内最高分 > 半导体(80)→ 电力在前
    assert [g["板块"] for g in d["groups"]] == ["电力", "半导体"]
    semi = next(g for g in d["groups"] if g["板块"] == "半导体")
    assert [r["code"] for r in semi["rows"]] == ["000002", "000001"]   # 组内合议分降序
    assert semi["rows"][0]["最接近形态"] == "箱体" and semi["rows"][0]["差距说明"] == "量能不足"


def test_daily_near_top3_per_board(monkeypatch):
    """接近达标每板块只取 top3(按合议分)。"""
    _patch_records(monkeypatch, {})
    near = [{"code": f"0001{i}", "行业": "半导体", "最接近形态": "杯柄",
             "差距说明": "d", "合议分": i} for i in range(6)]
    view = {"扫描数": 5000, "达标数": 0, "达标清单": [], "接近达标": near}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    g = d["groups"][0]
    assert g["count"] == 6 and len(g["rows"]) == 3           # 取 top3
    assert [r["合议分"] if "合议分" in r else r["council_score"] for r in g["rows"]] == [5, 4, 3]


def test_daily_zero_qualified_and_no_near(monkeypatch):
    """达标0且无接近达标 → present、groups 空(前端显示"无达标且无接近达标"),不报错。"""
    _patch_records(monkeypatch, {})
    view = {"扫描数": 5000, "达标数": 0, "达标清单": []}   # 无「接近达标」字段
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    assert d["present"] is True and d["groups"] == [] and d["near_n"] == 0


def test_daily_qualified_takes_priority_over_near(monkeypatch):
    """达标>0 时即使有接近达标也走达标分组(mode=qualified)。"""
    r1 = _rec("000001", bull=True); r1["meta"]["industry"] = "半导体"
    _patch_records(monkeypatch, {"000001": r1})
    view = {"扫描数": 500, "达标数": 1,
            "达标清单": [{"code": "000001", "命中形态": "杯柄", "正向确认依据": []}],
            "接近达标": [{"code": "000009", "行业": "电力", "最接近形态": "旗形", "差距说明": "x", "合议分": 99}]}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    d = da.selection_page()["daily"]
    assert d["mode"] == "qualified" and d["near_n"] == 1 and d["top_n"] == 5
    assert [g["板块"] for g in d["groups"]] == ["半导体"]


def test_selection_route_renders_near(monkeypatch):
    """路由:达标0+有接近达标 → 渲染"仅提示"标注 + 接近达标分组,不空页。"""
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    view = {"扫描数": 4800, "达标数": 0, "达标清单": [],
            "接近达标": [{"code": "000001", "行业": "半导体", "最接近形态": "杯柄",
                        "差距说明": "颈线差1.2%", "合议分": 60}]}
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    r = client.get("/selection")
    assert r.status_code == 200
    assert "仅提示,非达标信号" in r.text and "接近达标" in r.text
    assert "000001" in r.text and "颈线差1.2%" in r.text
    assert "待扫描生成" not in r.text                          # 有接近达标 → 不落兜底


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


def _pred_full():
    """一份有效 prediction 块(含 5日止盈止损 + 上涨概率),口径同 predict.py 产出。"""
    return {
        "现价": 12.34,
        "持有期建议": {"5日": {"止损位": 11.50, "最大亏损%": 6.8,
                              "止盈位": 13.60, "目标盈利%": 10.2, "风险收益比": 1.5}},
        "情景预测": {"5日": {"上涨概率%": 58.0, "样本数": 120}},
        "买卖倾向": {"结论": "偏买入", "依据": ["多头"]},
    }


def test_stops_view_guards_missing_and_error():
    """防空口径单测:prediction 缺失/None/error → 全字段 None;有效 → 透传 5日字段。"""
    assert da.stops_view({})["止损位"] is None
    assert da.stops_view({"prediction": None})["现价"] is None
    err = da.stops_view({"prediction": {"error": "数据不足", "n": 5}})   # 次新股
    assert all(v is None for v in err.values())
    ok = da.stops_view({"prediction": _pred_full()})
    assert ok["现价"] == 12.34 and ok["止损位"] == 11.50 and ok["最大亏损%"] == 6.8
    assert ok["止盈位"] == 13.60 and ok["目标盈利%"] == 10.2 and ok["风险收益比"] == 1.5
    assert ok["上涨概率%"] == 58.0


def test_selection_row_carries_stops_and_renders(monkeypatch):
    """自选票行带 5日止盈止损列;路由渲染含止损/止盈数值与新列表头。"""
    r = _rec("000001", bull=True)
    r["prediction"] = _pred_full()
    recs = {"000001": r}
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["rows"][0]["stops"]["止损位"] == 11.50
    assert page["rows"][0]["stops"]["上涨概率%"] == 58.0
    resp = client.get("/selection")
    assert resp.status_code == 200
    assert "现价" in resp.text and "止损位" in resp.text and "5日涨概率" in resp.text   # 新列表头
    assert "11.5" in resp.text and "13.6" in resp.text                              # 止损/止盈数值
    assert "58.0%" in resp.text                                                     # 5日上涨概率


def test_selection_error_prediction_renders_dash_no_crash(monkeypatch):
    """次新股 error-prediction 行不炸,相关列为「—」(不抛 UndefinedError)。"""
    r = _rec("301583", bull=True)
    r["prediction"] = {"error": "数据不足", "n": 12}       # K线<30
    recs = {"301583": r}
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert all(v is None for v in page["rows"][0]["stops"].values())
    resp = client.get("/selection")
    assert resp.status_code == 200 and "—" in resp.text                             # 占位符,未炸页


def test_dashboard_stops_columns_and_error_guard(monkeypatch):
    """首页全池速览带 5日止盈止损列;有效票显数值,error-prediction 票显「—」,不炸页。"""
    ok = _rec("000001", bull=True); ok["prediction"] = _pred_full()
    ok["snapshot"] = {"close": 12.34, "pct_chg": 1.2}
    err = _rec("301583", bull=True); err["prediction"] = {"error": "数据不足", "n": 12}
    err["snapshot"] = {"close": 20.0, "pct_chg": 0.5}
    recs = {"000001": ok, "301583": err}
    monkeypatch.setattr(da, "_load_all", lambda date="latest": recs)
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    resp = client.get("/")
    assert resp.status_code == 200
    assert "止损位" in resp.text and "5日涨概率" in resp.text     # 新列表头
    assert "11.5" in resp.text and "13.6" in resp.text          # 有效票止损/止盈
    assert "—" in resp.text                                     # error 票占位,未炸


def _pred_with_structure():
    """一份含 L3「结构位 / 情景锚定」的 prediction 块(口径同 predict.py 产出)。"""
    return {
        "现价": 12.34,
        "持有期建议": {"5日": {"止损位": 11.50, "最大亏损%": 6.8,
                              "止盈位": 13.60, "目标盈利%": 10.2, "风险收益比": 1.5}},
        "情景预测": {"5日": {"上涨概率%": 58.0, "样本数": 120}},
        "买卖倾向": {"结论": "偏买入", "依据": ["多头"]},
        "结构位": {
            "支撑": [11.8, 11.2], "压力": [13.0, 13.8],
            "距支撑%": 4.6, "距压力%": 5.3, "区间位置%": 47.0,
            "当日量比": 1.8, "放量": True, "突破": "放量突破", "趋势": "偏多", "bias20": 3.2,
            "锚定": {"情景": "突破回踩企稳", "止损位": 11.80, "止盈位": 13.60,
                    "盈亏比": 3.3, "依据": ["放量突破颈线", "距支撑近"]},
        },
    }


def test_structure_view_and_anchor_stops():
    """L3 取数 + 回退链单测:结构位存在→透传+锚定优先;缺失→回退 5日ATR;error→全 None。"""
    # 结构位存在
    r = {"prediction": _pred_with_structure()}
    sv = da.structure_view(r)
    assert sv is not None and sv["突破"] == "放量突破" and sv["锚定"]["盈亏比"] == 3.3
    a = da.anchor_stops(r)
    assert a["source"] == "结构位" and a["情景"] == "突破回踩企稳"
    assert a["止损位"] == 11.80 and a["止盈位"] == 13.60 and a["盈亏比"] == 3.3
    assert a["区间位置%"] == 47.0 and a["突破"] == "放量突破"
    # 结构位缺失(仅 5日 ATR)→ 回退
    r2 = {"prediction": _pred_full()}
    assert da.structure_view(r2) is None
    a2 = da.anchor_stops(r2)
    assert a2["source"] == "5日ATR" and a2["止损位"] == 11.50 and a2["盈亏比"] == 1.5
    assert a2["最大亏损%"] == 6.8 and a2["情景"] is None
    # error / 缺失 prediction → 全 None、None 视图
    assert da.structure_view({"prediction": {"error": "数据不足", "n": 5}}) is None
    err = da.anchor_stops({"prediction": {"error": "数据不足"}})
    assert err["止损位"] is None and err["source"] is None
    assert da.anchor_stops({})["source"] is None


def test_selection_row_carries_anchor_and_renders_structure(monkeypatch):
    """区块①:结构位存在→行带 anchor(锚定优先),路由渲染情景/突破/区间位置。"""
    r = _rec("000001", bull=True)
    r["prediction"] = _pred_with_structure()
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {"000001": r})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    row = page["rows"][0]
    assert row["anchor"]["source"] == "结构位" and row["anchor"]["盈亏比"] == 3.3
    resp = client.get("/selection")
    assert resp.status_code == 200
    assert "突破回踩企稳" in resp.text and "放量突破" in resp.text and "47.0%" in resp.text


def test_selection_row_anchor_falls_back_when_no_structure(monkeypatch):
    """区块①:无结构位(老数据)→ anchor 回退 5日ATR,渲染止损/止盈点位不炸。"""
    r = _rec("000001", bull=True)
    r["prediction"] = _pred_full()               # 无结构位
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {"000001": r})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["rows"][0]["anchor"]["source"] == "5日ATR"
    resp = client.get("/selection")
    assert resp.status_code == 200 and "11.5" in resp.text and "13.6" in resp.text


def test_selection_error_prediction_anchor_dash(monkeypatch):
    """区块①:次新股 error-prediction → anchor 全 None,渲染「—」不抛。"""
    r = _rec("301583", bull=True)
    r["prediction"] = {"error": "数据不足", "n": 12}
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {"301583": r})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["rows"][0]["anchor"]["source"] is None
    resp = client.get("/selection")
    assert resp.status_code == 200 and "—" in resp.text


def test_top_picks_ranks_and_dedup(monkeypatch):
    """区块③ Top N:达标∪接近达标∪自选池去重,按合议综合分降序;结构位存在带锚定。"""
    r1 = _rec("000001", bull=True); r1["prediction"] = _pred_with_structure()
    r2 = _rec("000002", bull=False)                      # 看空 → 综合分低
    recs = {"000001": r1, "000002": r2}
    _patch_records(monkeypatch, recs)
    view = {"扫描数": 2, "达标数": 1,
            "达标清单": [{"code": "000001", "命中形态": "杯柄", "正向确认依据": []}],
            "接近达标": [{"code": "000009", "行业": "电力", "最接近形态": "旗形",
                        "差距说明": "x", "合议分": 5.0}]}    # 全A票无中心记录,取契约合议分
    monkeypatch.setattr(da.store, "get_view", lambda name, date="latest": view)
    tp = da.selection_page()["top_picks"]
    codes = [x["code"] for x in tp]
    assert codes.count("000001") == 1                    # 去重(既在达标清单又在自选池)
    assert "000009" in codes                             # 接近达标票并入
    # 降序:综合分非空在前;000001(看多+结构位)在 000002(看空)前
    assert codes.index("000001") < codes.index("000002")
    pick1 = next(x for x in tp if x["code"] == "000001")
    assert pick1["情景"] == "突破回踩企稳" and pick1["盈亏比"] == 3.3 and pick1["experts"]
    pick9 = next(x for x in tp if x["code"] == "000009")
    assert pick9["council_score"] == 5.0 and pick9["止损位"] is None   # 无记录→无锚定


def test_top_picks_fallback_no_view(monkeypatch):
    """区块③ 兜底:无达标池 view → 仅用自选池仍能出 Top N。"""
    recs = {"000001": _rec("000001", bull=True), "000002": _rec("000002", bull=False)}
    _patch_records(monkeypatch, recs)
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    tp = da.selection_page()["top_picks"]
    assert {x["code"] for x in tp} == {"000001", "000002"}
    assert tp[0]["code"] == "000001"                     # 综合分降序(看多在前)


def test_top_picks_caps_at_15(monkeypatch):
    """区块③:候选超过 15 只 → 只取 Top 15。"""
    recs = {f"0001{i:02d}": _rec(f"0001{i:02d}", bull=True) for i in range(20)}
    _patch_records(monkeypatch, recs)
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    tp = da.selection_page()["top_picks"]
    assert len(tp) == 15


def test_top_picks_empty_day_no_crash(monkeypatch):
    """区块③:完全无数据 → 空列表,路由渲染友好占位不空页。"""
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    assert da.selection_page()["top_picks"] == []
    resp = client.get("/selection")
    assert resp.status_code == 200 and "今日精选" in resp.text


def test_selection_route_renders_topn_section_and_disclaimer(monkeypatch):
    """区块③ 路由:标题 + 数据策略说明(纯数据、未用新闻/大模型)渲染。"""
    r = _rec("000001", bull=True); r["prediction"] = _pred_with_structure()
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {"000001": r})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-08-08"])
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    resp = client.get("/selection")
    assert resp.status_code == 200
    assert "今日精选" in resp.text and 'id="topBody"' in resp.text
    assert "未用新闻" in resp.text and "非投资建议" in resp.text


def test_selection_empty_day_no_crash(monkeypatch):
    monkeypatch.setattr(da, "_load_all", lambda date="latest": {})
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-08-08")
    monkeypatch.setattr(da.store, "get_view",
                        lambda name, date="latest": (_ for _ in ()).throw(FileNotFoundError()))
    page = da.selection_page()
    assert page["total"] == 0 and page["rows"] == []
