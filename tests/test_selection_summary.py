"""选股/复盘 markdown 精简摘要抽取单测(tools.selection_summary)。

锁住的语义(为什么改):
  - 摘要只抽"买入建议排序表(代码/名称/表态/一句话)+ 市场环境一句话 + 记分表(方向/α)+ 经验标题",
    **绝不搬逐票深度全文** —— 用一段带唯一深度句的 md 反证:深度句不得进摘要。
  - 表头版式不统一(代码/名称分列 vs 代码嵌在"票"单元;表态 vs 明确建议)均能解析。
  - 视图结构固定为三区两项 {date, 盘后:{选股,复盘}, 盘中:None, 盘尾:None}。
  - 脏输入/缺段不抛错,降级为空。
"""
import pytest

from tools import selection_summary as ss


class _MemP:
    """内存版 Path:read_text 返回预置文本;None 表示文件不存在。"""
    def __init__(self, text): self._t = text
    def is_file(self): return self._t is not None
    def read_text(self, encoding="utf-8"): return self._t


class _MemDir:
    """内存版目录:`dir / name` → _MemP(mp[name])。用于把 build_selection_view 的
    选股/复盘目录替换成内存映射,单测不落磁盘。"""
    def __init__(self, mp): self._mp = mp
    def __truediv__(self, name): return _MemP(self._mp.get(name))

# 版式 A:代码/名称分列、表态/一句话列(近期版式,如 2026-09-07)
_SEL_A = """# 每日选股分析 · 2026-09-07（收盘后选股，预测 09-08）

## 1. 市场环境判断（β 背景）

**定性：偏多，但典型的"成长/科技领涨的结构性行情"。** 等权 +0.926% 高于中位数。

## 3. 逐票深度分析

### 3.1 五洲特纸 605007
UNIQUE_DEEP_DIVE_SENTENCE_不应进摘要 —— 收 14.78，bias20 −3.18，获利比例 0.362。

## 4. 买入建议排序（明确表态·不对冲）

| 排序 | 代码 | 名称 | 表态 | 一句话 |
|---|---|---|---|---|
| 1 | **605007** | 五洲特纸 | **可参与偏多（条件式）** | 合议#1 ∩ 反转，净利+88.95% |
| 2 | 688262 | 国芯科技 | 观望 | 半导体因子极值但当期亏损 |
"""

# 版式 B:代码嵌在"票"单元、列名为"明确建议/一句话理由"(旧版式,如 2026-09-01)
_SEL_B = """# 每日选股 · 2026-09-01

### 1.3 我的定性结论
> **市场环境：震荡偏多（弱）**，主线是风格切换。

## 一·补、买入建议排序（明确表态）

| 排序 | 票 | **明确建议** | 一句话理由 |
|---|---|---|---|
| ① 首选 | **瑞芯微 603893** | **买入/偏多** | Q2净利+62%+端侧AI龙头 |
| ⑤ 规避 | 中南文化 002445 | 规避 | 壳股+游资博弈 |
"""

_REVIEW = """# 每日复盘 · 2026-09-07

## 盘尾复盘（收盘确定性节点）

### 二、逐票收盘记分（α = 收盘涨跌 − 全A等权 +0.926%）

| 票 | 09-04 表态 | 09-07收 | **α vs 等权** | **子链α** | 收盘判定 |
|---|---|---|---|---|---|
| **002234 民和** | 买入 | 8.52 +2.16% | **+1.23pp** | +1.48pp | ✅ 偏多兑现 |
| **603162 海通** | 规避 | 14.13 −3.48% | **−4.41pp** | −2.96pp | ✅ 规避正确 |

### 三、方向记分 + 组合口径（收盘定稿）

**1 日方向命中：1 / 1**。本批 5 只中仅 002234 给"偏多"。UNIQUE_REVIEW_DEEP_不应进摘要。

### 五、经验确认与新增

**#21（板块β隔周衰减）· 收盘确认成立。** 农林牧渔跨周末退潮。
**#23（新增）· "逆板块α"是板块方向的镜像、不可线性外推。** 详见经验沉淀。
"""


def test_selection_ranking_layout_a():
    s = ss.extract_selection_summary(_SEL_A)
    assert "偏多" in s["env_note"]
    assert len(s["ranking"]) == 2
    r0 = s["ranking"][0]
    assert r0["code"] == "605007" and r0["name"] == "五洲特纸"
    assert "可参与偏多" in r0["stance"]
    assert "净利+88.95%" in r0["note"]


def test_selection_ranking_layout_b_code_in_ticket_cell():
    """代码嵌在"票"单元、列名"明确建议/一句话理由"也能解析。"""
    s = ss.extract_selection_summary(_SEL_B)
    assert "震荡偏多" in s["env_note"]
    codes = {r["code"] for r in s["ranking"]}
    assert codes == {"603893", "002445"}
    top = s["ranking"][0]
    assert top["code"] == "603893" and "瑞芯微" in top["name"]
    assert "买入" in top["stance"]


def test_selection_summary_excludes_deep_dive():
    """精简≠全文:逐票深度分析的唯一句不得出现在摘要任何字段。"""
    s = ss.extract_selection_summary(_SEL_A)
    blob = repr(s)
    assert "UNIQUE_DEEP_DIVE_SENTENCE" not in blob
    assert "获利比例" not in blob and "bias20" not in blob


def test_review_scorecard_and_direction():
    r = ss.extract_review_summary(_REVIEW)
    assert "1 / 1" in r["direction"]
    assert len(r["scorecard"]) == 2
    a = {row["code"]: row["alpha"] for row in r["scorecard"]}
    assert a["002234"] == "+1.23pp"        # 取 α vs 等权 列,非子链α
    assert a["603162"] == "−4.41pp"


def test_review_experiences_titles_and_new_flag():
    r = ss.extract_review_summary(_REVIEW)
    joined = " ".join(r["experiences"])
    assert "#21" in joined and "板块β隔周衰减" in joined
    assert any(e.startswith("#23（新增）") for e in r["experiences"])


def test_review_summary_excludes_deep_text():
    r = ss.extract_review_summary(_REVIEW)
    assert "UNIQUE_REVIEW_DEEP" not in repr(r)


def test_build_view_structure():
    """三区两项结构固定;盘中/盘尾恒为占位 None。"""
    sel_dir = _MemDir({"2026-09-07.md": _SEL_A})
    rev_dir = _MemDir({"2026-09-07.md": _REVIEW})
    v = ss.build_selection_view("2026-09-07", selection_dir=sel_dir, review_dir=rev_dir)
    assert set(v) == {"date", "盘后", "盘中", "盘尾"}
    assert v["date"] == "2026-09-07"
    assert v["盘中"] is None and v["盘尾"] is None
    assert v["盘后"]["选股"]["ranking"][0]["code"] == "605007"
    assert v["盘后"]["复盘"]["scorecard"][0]["code"] == "002234"


def test_review_scorecard_captures_stance():
    """记分表新增 stance 字段(复盘里记录的原始表态列),供排序表「复盘结果」列取用。"""
    r = ss.extract_review_summary(_REVIEW)
    st = {row["code"]: row.get("stance") for row in r["scorecard"]}
    assert st["002234"] == "买入"
    assert st["603162"] == "规避"


# 版式:选股票与复盘票有重叠(605007 同时出现在选股排序与当日复盘记分),验证按 code 匹配
_REVIEW_OVERLAP = """# 每日复盘 · 2026-09-07

### 二、逐票收盘记分（α = 收盘涨跌 − 全A等权）

| 票 | 表态 | 收盘 | **α vs 等权** | 收盘判定 |
|---|---|---|---|---|
| **605007 五洲特纸** | 买入(条件式) | 15.30 +3.5% | **+2.57pp** | ✅ 兑现 |
| **002234 民和** | 规避 | 8.52 −1.0% | **−1.90pp** | ✅ |
"""


def test_ranking_review_column_cross_day_three_states():
    """排序表「复盘结果」列 = 跨日反查:选股日 D 的票取其 D+1(次一交易日)复盘的回看结果。
    三态齐锁:命中→表态+α;D+1 复盘存在但票不在→空串(渲染「—」);D+1 复盘不存在→"待复盘"。
    交易日推进走 calendar.next_trading_day(与被测同一日历口径,不硬算),测试对日历实现无耦合。"""
    from tools.collectors import calendar as cal
    D = "2026-09-07"
    D1 = cal.next_trading_day(D, allow_fetch=False)          # 次一交易日(D+1),不触网
    assert D1 > D
    # D+1 复盘存在:605007 命中(表态+α),688262 不在其中(→ 空串)
    view = ss.build_selection_view(
        D,
        selection_dir=_MemDir({f"{D}.md": _SEL_A}),
        review_dir=_MemDir({f"{D1}.md": _REVIEW_OVERLAP}),
    )
    rows = {r["code"]: r for r in view["盘后"]["选股"]["ranking"]}
    assert "买入" in rows["605007"]["review"] and "+2.57pp" in rows["605007"]["review"]
    assert rows["688262"]["review"] == ""                   # D+1 复盘里没有该票 → 「—」
    # 午盘无数据源 → 恒为空串占位
    assert rows["605007"]["midday"] == "" and rows["688262"]["midday"] == ""


def test_ranking_review_pending_when_next_day_review_absent():
    """边界:选股日 D 的 D+1 复盘尚未产出(如今天刚选的票)→ 排序表「复盘结果」列显示"待复盘",
    绝不取"当日 D 复盘"(那评的是前一日票、与今日选出的票不重叠)、不报错、不渲染「—」。"""
    # review_dir 里只有"当日 D"复盘、没有 D+1 → 跨日反查落空 → 待复盘
    view = ss.build_selection_view(
        "2026-09-07",
        selection_dir=_MemDir({"2026-09-07.md": _SEL_A}),
        review_dir=_MemDir({"2026-09-07.md": _REVIEW_OVERLAP}),   # 同日复盘,非 D+1
    )
    rows = {r["code"]: r for r in view["盘后"]["选股"]["ranking"]}
    assert rows["605007"]["review"] == "待复盘"
    assert rows["688262"]["review"] == "待复盘"


def test_review_takes_same_date_not_previous_day():
    """A-1:复盘取"当日"复盘,不取前一天。给两天不同复盘,build_selection_view(D)只应
    命中 D 的复盘(605007 命中),绝不落到 D-1 的复盘(其记分是别的票)。"""
    _REVIEW_PREV = """# 每日复盘 · 2026-09-04

### 二、逐票收盘记分

| 票 | 表态 | **α vs 等权** |
|---|---|---|
| **999999 别的票** | 规避 | **−9.99pp** |
"""
    rev_dir = _MemDir({"2026-09-07.md": _REVIEW_OVERLAP, "2026-09-04.md": _REVIEW_PREV})
    view = ss.build_selection_view(
        "2026-09-07", selection_dir=_MemDir({"2026-09-07.md": _SEL_A}), review_dir=rev_dir)
    sc_codes = {row["code"] for row in view["盘后"]["复盘"]["scorecard"]}
    assert sc_codes == {"605007", "002234"}          # 当日(09-07)复盘
    assert "999999" not in sc_codes                  # 绝不串到前一天(09-04)


def test_page_no_experience_block_rendered(monkeypatch):
    """A-3:/selection-analysis 页不再出现「新增/确认经验」区块(摘要仍抽取,仅不渲染)。"""
    from fastapi.testclient import TestClient

    import web.app as webapp
    from web import data_access as da

    view = ss.build_selection_view(
        "2026-09-07",
        selection_dir=_MemDir({"2026-09-07.md": _SEL_A}),
        review_dir=_MemDir({"2026-09-07.md": _REVIEW}),
    )
    # 摘要里经验仍被抽出(不破坏抽取能力)
    assert view["盘后"]["复盘"]["experiences"], "经验抽取应保留"
    monkeypatch.setattr(da, "selection_analysis_view", lambda date="latest": view)
    monkeypatch.setattr(da, "available_dates", lambda: ["2026-09-07"])
    monkeypatch.setattr(da, "as_of", lambda date="latest": "2026-09-07")
    monkeypatch.setattr(da, "current_data_source", lambda sample=20: {})
    html = TestClient(webapp.app).get("/selection-analysis").text
    assert "新增/确认经验" not in html                 # 经验区块标题不出现
    assert "板块β隔周衰减" not in html                 # 经验条目文本不出现
    assert "午盘分析" in html and "复盘结果" in html    # A-2 两列表头在


def test_build_view_none_when_both_missing():
    class _Missing:
        def __truediv__(self, name):
            class _P:
                def is_file(self): return False
            return _P()
    assert ss.build_selection_view("2000-01-01", selection_dir=_Missing(), review_dir=_Missing()) is None


@pytest.mark.parametrize("bad", ["", "no tables here", "# 标题\n\n随便一段文字。", "| a | b |\n| broken"])
def test_robust_on_garbage(bad):
    s = ss.extract_selection_summary(bad)
    r = ss.extract_review_summary(bad)
    assert s["ranking"] == [] and isinstance(s["env_note"], str)
    assert r["scorecard"] == [] and r["experiences"] == []


def test_collect_date_injects_selection_view(tmp_path, monkeypatch):
    """上传/入库枚举口径(collect_date)应把 selection_analysis 视图纳入 views,
    从而随 __view__:selection_analysis 分片走既有上传管线。"""
    import json

    from tools.sync import import_to_db

    sel_dir = tmp_path / "选股"; rev_dir = tmp_path / "复盘"
    sel_dir.mkdir(); rev_dir.mkdir()
    (sel_dir / "2026-09-07.md").write_text(_SEL_A, encoding="utf-8")
    (rev_dir / "2026-09-07.md").write_text(_REVIEW, encoding="utf-8")
    monkeypatch.setattr(ss, "SELECTION_DIR", sel_dir)
    monkeypatch.setattr(ss, "REVIEW_DIR", rev_dir)

    analysis = tmp_path / "analysis"
    day = analysis / "2026-09-07"; day.mkdir(parents=True)
    (day / "600519.json").write_text(json.dumps({"meta": {"code": "600519"}}), encoding="utf-8")

    payload = import_to_db.collect_date(analysis, "2026-09-07")
    assert "selection_analysis" in payload["views"]
    view = payload["views"]["selection_analysis"]
    assert view["盘后"]["选股"]["ranking"][0]["code"] == "605007"

    # 走 build_shards 后独立成 __view__:selection_analysis 分片
    from tools.sync import upload
    shards = upload.build_shards(payload)
    assert "__view__:selection_analysis" in shards


# ————————————————————————————————————————————————
# 盘后两表(今日选股 D + 对昨日 D-1 选股复盘,α 由代码算)—— 锁新口径语义
# 为什么改(2026-09-09):旧实现复盘侧抽的是"自选盯盘池/盘中核实"表(与选出票对不上、名称丢),
# 且无"D 选股 ↔ D-1 选股复盘"的日期对应。新口径:复盘对象=选股 md **实际选出票**、α 代码算
# (该票次日涨跌% − 次日全A等权 mean_pct)、名称回退不空、选股/复盘跳过时显式占位。
# ————————————————————————————————————————————————
class _PickP:
    """内存版选股 md 文件:text(None=不存在) + 预置 picks(fake parse_picks 直接取)。"""
    def __init__(self, text, picks): self._t = text; self.picks = picks
    def is_file(self): return self._t is not None
    def read_text(self, encoding="utf-8"): return self._t


class _PickDir:
    def __init__(self, mp): self._mp = mp
    def __truediv__(self, name): return self._mp.get(name) or _PickP(None, [])


def _fake_parse(p):
    return list(getattr(p, "picks", []) or [])


# 09-08 选出票(买入排序,非自选池);09-07 起承接
_SEL_0908 = """# 每日选股分析 · 2026-09-08

## 4. 买入建议排序（明确表态·不对冲）

| 排序 | 代码 | 名称 | 表态 | 一句话 |
|---|---|---|---|---|
| 1 | **601061** | 中信金属 | **买入候选** | 数据面强+真利好双击 |
| 2 | 600356 | 恒丰纸业 | 观望偏多 | 数据面最高被利空压低 |
"""


def test_parse_equal_weight_mean_pct_both_signs():
    """市场表「全A等权 mean_pct」抽取:兼容 +0.665% 与 Unicode 负号 −0.4674%。"""
    assert ss.parse_equal_weight_mean_pct("| **全A等权 mean_pct** | **+0.665%** | α 记分主基准 |") == 0.665
    assert ss.parse_equal_weight_mean_pct("| **全A等权 mean_pct** | **−0.4674%** |") == -0.4674
    assert ss.parse_equal_weight_mean_pct("无关文本") is None


def test_alpha_scorecard_formula_and_name_fallback():
    """α = 该票衡量日涨跌% − 全A等权基准;名称走回退(不空);pct/基准任一缺 → α=None、显示空串。"""
    quotes = {"601061": 4.91, "000019": 10.0, "600356": None}   # 600356 无报价
    names = {"601061": "中信金属", "000019": "深粮控股"}         # 600356 无名 → 回退 code
    rows = ss.alpha_scorecard(
        ["601061", "000019", "600356"],
        quote_of=lambda c: quotes.get(c),
        benchmark=-0.4674,
        name_of=lambda c: names.get(c) or c)
    by = {r["code"]: r for r in rows}
    assert by["601061"]["alpha_val"] == round(4.91 - (-0.4674), 2)   # 正 α
    assert by["601061"]["alpha_val"] > 0
    assert by["000019"]["alpha_val"] == round(10.0 - (-0.4674), 2)   # 涨停 → α 大正
    assert by["600356"]["alpha_val"] is None and by["600356"]["alpha"] == ""
    assert all(r["name"] for r in rows)                              # 名称非空
    assert by["600356"]["name"] == "600356"                          # 无名回退 code
    # 基准缺失 → 全票 α=None
    none_bench = ss.alpha_scorecard(["601061"], quote_of=lambda c: 4.91,
                                    benchmark=None, name_of=lambda c: "x")
    assert none_bench[0]["alpha_val"] is None


def test_postmarket_review_is_prior_day_actual_picks_with_code_alpha():
    """复盘侧 = 昨日(D-1)**实际选出票** + 代码算 α(D 日衡量),**不是**自选盯盘池、不 parse 复盘 md。"""
    D, D1 = "2026-09-09", "2026-09-08"
    sel_dir = _PickDir({
        f"{D}.md": _PickP("# D 选股\n", ["601061"]),
        f"{D1}.md": _PickP(_SEL_0908, ["601061", "600356"]),
    })
    quotes = {("601061", D): 4.91, ("600356", D): 2.0}
    v = ss.build_postmarket_view(
        D, selection_dir=sel_dir, parse_picks=_fake_parse,
        prev_trading_day=lambda x: D1, next_trading_day=lambda x: "2026-09-10",
        quote_at=lambda c, md: quotes.get((c, md)),
        benchmark_at=lambda md: -0.4674 if md == D else None,   # D+1(09-10)基准缺 → 今日票待复盘
        name_of=lambda c: {"601061": "中信金属", "600356": "恒丰纸业"}.get(c, c))
    rev = v["复盘"]
    assert v["prior_sel_date"] == D1 and rev["measure_date"] == D
    assert [r["code"] for r in rev["scorecard"]] == ["601061", "600356"]   # D-1 实际选出票
    by = {r["code"]: r for r in rev["scorecard"]}
    assert by["601061"]["alpha_val"] == round(4.91 - (-0.4674), 2) > 0     # 代码算 α
    assert all(r["name"] for r in rev["scorecard"])                        # 名称非空
    assert rev["skipped"] is False and rev["benchmark"] == -0.4674
    # 今日选股(D)票的「复盘结果」列:D+1 基准缺 → 待复盘
    assert v["选股"]["skipped"] is False
    assert all(r["review"] == "待复盘" for r in v["选股"]["ranking"])


def test_postmarket_today_review_column_next_day_alpha():
    """今日票「复盘结果」列 = 次日 D+1 α:D+1 基准已产出 → 命中票显示 α、无报价票渲染空(「—」)。"""
    D, D1 = "2026-09-07", "2026-09-08"
    sel_dir = _PickDir({
        f"{D}.md": _PickP(_SEL_A, ["605007", "688262"]),
        f"{D1}.md": _PickP(None, []),   # D-1(09-04)选股缺,复盘侧另测,这里不关注
    })
    q = {("605007", D1): 3.5}            # 688262 次日无报价
    v = ss.build_postmarket_view(
        D, selection_dir=sel_dir, parse_picks=_fake_parse,
        prev_trading_day=lambda x: "2026-09-04", next_trading_day=lambda x: D1,
        quote_at=lambda c, md: q.get((c, md)),
        benchmark_at=lambda md: 0.926 if md == D1 else None,
        name_of=lambda c: c)
    rows = {r["code"]: r for r in v["选股"]["ranking"]}
    assert rows["605007"]["review"] == ss._fmt_alpha(round(3.5 - 0.926, 2))  # 命中 → α
    assert rows["688262"]["review"] == ""                                    # 次日无报价 → 「—」


def test_postmarket_skip_states():
    """跳过态占位:D 选股跳过 → 选股.skipped(整表结果未出);D-1 选股跳过 → 复盘.skipped(昨日结果未出)。"""
    D = "2026-09-09"
    sel_dir = _PickDir({
        f"{D}.md": _PickP("# 跳过留痕\n本日无选股。", []),        # D 跳过
        "2026-09-08.md": _PickP("# 跳过留痕\n本日无选股。", []),   # D-1 跳过
    })
    v = ss.build_postmarket_view(
        D, selection_dir=sel_dir, parse_picks=_fake_parse,
        prev_trading_day=lambda x: "2026-09-08", next_trading_day=lambda x: "2026-09-10",
        quote_at=lambda c, md: None, benchmark_at=lambda md: None, name_of=lambda c: c)
    assert v["选股"]["skipped"] is True
    assert v["复盘"]["skipped"] is True and v["复盘"]["scorecard"] == []


def test_web_selection_analysis_prior_day_picks_real_parse(monkeypatch):
    """web 端到端(真实 parse_pick_codes + 真实 09-08 选股 md + 合成记录/基准):
    date=2026-09-09 复盘表 = 09-08 **实际选出票**(8 只),名称非空,α 代码算且 601061 为正;
    绝不出现自选盯盘池(300209 等)。"""
    from web import data_access as da
    from tools.store import repo as store
    from tools.analysis import equal_weight_index as ewi

    expect_0908 = ["600356", "601061", "601339", "688712",
                   "601000", "000035", "688262", "000019"]
    pct_0909 = {"601061": 4.91, "000019": 10.0, "600356": 1.2, "601339": -0.5,
                "688712": 2.3, "601000": 0.8, "000035": 3.1, "688262": -1.0}

    def fake_get_record(code, date="latest"):
        if date == "2026-09-09" and code in pct_0909:
            return {"meta": {"code": code, "name": f"名{code}"},
                    "snapshot": {"pct_chg": pct_0909[code]}}
        raise FileNotFoundError(code)

    monkeypatch.setattr(store, "get_record", fake_get_record)
    monkeypatch.setattr(ewi, "load_daily_mean_pct", lambda *a, **k: {"2026-09-09": -0.4674})
    v = da.selection_analysis_view("2026-09-09")
    rev = v["盘后"]["复盘"]
    codes = [r["code"] for r in rev["scorecard"]]
    assert codes == expect_0908                         # D-1 实际选出票,顺序一致
    assert "300209" not in codes and "300476" not in codes   # 不再是自选盯盘池
    assert all(r["name"] for r in rev["scorecard"])     # 名称非空
    by = {r["code"]: r for r in rev["scorecard"]}
    assert by["601061"]["alpha_val"] == round(4.91 - (-0.4674), 2) > 0
    assert by["000019"]["alpha_val"] > 9                # 涨停 → α 大正
    assert rev["prior_sel_date"] == "2026-09-08"
    # 今日选股(09-09)票的复盘列:次日 09-10 基准缺 → 待复盘
    assert v["盘后"]["选股"]["skipped"] is False
    assert all(r["review"] == "待复盘" for r in v["盘后"]["选股"]["ranking"])


# ————————————————————————————————————————————————
# web 取「该票 D 日涨跌%」多级回退(_selection_pct_at):record → 主档 K线 → 复盘 md → None
# 锁:回退优先级、α 口径(下游 α=pct−基准)、防未来函数(只取 md_date 当日/之前数据)。
# 注:_scorecard 用 6 位代码正则抽票,故测试代码须为 6 位数字。
# ————————————————————————————————————————————————
_REVIEW_MD = (
    "### 二、逐票收盘记分（α = 收盘涨跌 − 全A等权 +0.926%）\n\n"
    "| 票 | 09-08收 | **09-09收** | **α vs 等权** | 收盘判定 |\n"
    "|---|---|---|---|---|\n"
    "| **600003 测试C** | 11.20 | **11.75 +4.91%**、换手2.7% | **+3.98pp** | ✅ 强 α |\n"
    "| **600005 测试E** | 9.50 | **9.70 +2.00%** | **+1.07pp** | ◻️ |\n"
)
# 收盘列无带符号百分数(只有价) → 走 α + 表头基准还原兜底
_REVIEW_MD_NOPCT = (
    "### 二、逐票收盘记分（α = 收盘涨跌 − 全A等权 +0.926%）\n\n"
    "| 票 | **09-09收** | **α vs 等权** | 收盘判定 |\n"
    "|---|---|---|---|\n"
    "| **600007 测试F** | 12.30 | **+5.38pp** | ✅ |\n"
)


def test_parse_review_close_pct_direct_and_reconstruct():
    """主路:直接取 D 日收盘列的带符号涨跌%(+4.91% / +2.00%),忽略"收盘判定"列的"收"字干扰;
    兜底:收盘列无 % 时用 α + 表头声明基准还原(pct = 0.926 + 5.38);
    找不到票 / 无表 → None(不臆造)。"""
    assert ss.parse_review_close_pct(_REVIEW_MD, "600003") == 4.91          # 主路直接取
    assert ss.parse_review_close_pct(_REVIEW_MD, "600005") == 2.00
    assert ss.parse_review_close_pct(_REVIEW_MD_NOPCT, "600007") == round(0.926 + 5.38, 4)  # 兜底
    assert ss.parse_review_close_pct(_REVIEW_MD, "999999") is None          # 票不在表
    assert ss.parse_review_close_pct("无记分表的正文", "600003") is None
    assert ss.parse_review_close_pct("", "600003") is None


def _fake_master_df(rows):
    """构造最小主档 K线 DataFrame(date/close/pct_chg 列),rows=[(date, close, pct_chg)]。"""
    import pandas as pd
    return pd.DataFrame(rows, columns=["date", "close", "pct_chg"])


def test_selection_pct_at_fallback_priority(monkeypatch, tmp_path):
    """多级回退优先级 + 防未来:
      A) record 有 → 用 record.snapshot.pct_chg;
      B) record 无、主档 K线有当日 → 用主档 pct_chg;
      C) record/主档均无、复盘 md 有 → 用 md 还原 pct;
      D) 全无 → None;
      E) 防未来:主档最新日 < md_date(当日无行)→ 主档不冒充,退到 md 级。"""
    from web import data_access as da
    from tools.store import repo as store
    D = "2026-09-09"

    rec_pct = {"600001": 4.91}                          # A:仅此票有 record
    master = {                                          # B/E:主档
        "600002": _fake_master_df([("2026-09-08", 10.0, 1.0), (D, 10.8, 8.0)]),
        "600005": _fake_master_df([("2026-09-07", 9.0, 1.0), ("2026-09-08", 9.5, 5.0)]),  # 无 D 当日行
    }

    def fake_get_record(code, date="latest"):
        if date == D and code in rec_pct:
            return {"meta": {"code": code}, "snapshot": {"pct_chg": rec_pct[code]}}
        raise FileNotFoundError(code)

    def fake_get_master_kline(code):
        if code in master:
            return master[code]
        raise FileNotFoundError(code)

    monkeypatch.setattr(store, "get_record", fake_get_record)
    monkeypatch.setattr(store, "get_master_kline", fake_get_master_kline)
    rev_dir = tmp_path / "复盘"                          # C/E:复盘 md(含 600003/600005 行)
    rev_dir.mkdir()
    (rev_dir / f"{D}.md").write_text(_REVIEW_MD, encoding="utf-8")
    monkeypatch.setattr(ss, "REVIEW_DIR", rev_dir)

    assert da._selection_pct_at("600001", D) == 4.91                       # A) record 优先
    assert da._selection_pct_at("600002", D) == 8.0                        # B) 主档当日 pct_chg
    assert da._selection_pct_at("600003", D) == 4.91                       # C) md 直接取收盘%
    assert da._selection_pct_at("600009", D) is None                       # D) 全无 → None
    assert da._selection_pct_at("600005", D) == 2.00                       # E) 主档无当日行→退 md


def test_selection_pct_at_master_computes_from_close_when_pct_nan(monkeypatch):
    """主档 pct_chg 缺/NaN 时由 close/prev_close−1 现算(单位 %),口径正确。"""
    import math as _m
    from web import data_access as da
    from tools.store import repo as store
    D = "2026-09-09"
    df = _fake_master_df([("2026-09-08", 10.0, 1.0), (D, 11.0, float("nan"))])

    monkeypatch.setattr(store, "get_record",
                        lambda code, date="latest": (_ for _ in ()).throw(FileNotFoundError(code)))
    monkeypatch.setattr(store, "get_master_kline", lambda code: df)
    v = da._selection_pct_at("600002", D)
    assert v is not None and abs(v - 10.0) < 1e-6      # (11/10−1)*100 = 10.0%
    assert not _m.isnan(v)


def test_selection_pct_at_alpha_caliber_end_to_end(monkeypatch):
    """口径锁:下游 α = pct(多级回退取到) − 全A等权基准,与项目定义一致。
    用主档源取 pct=8.0,基准=−0.4674 → α=round(8.0−(−0.4674),2)。"""
    from web import data_access as da
    from tools.store import repo as store
    D = "2026-09-09"
    df = _fake_master_df([("2026-09-08", 10.0, 1.0), (D, 10.8, 8.0)])
    monkeypatch.setattr(store, "get_record",
                        lambda code, date="latest": (_ for _ in ()).throw(FileNotFoundError(code)))
    monkeypatch.setattr(store, "get_master_kline", lambda code: df)
    pct = da._selection_pct_at("600002", D)
    bench = -0.4674
    assert round(pct - bench, 2) == round(8.0 - (-0.4674), 2)
