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


def test_ranking_rows_carry_midday_and_review_columns():
    """排序表每行补 midday(午盘·当前无源→空) 与 review(同日复盘按 code 匹配)两列。"""
    view = ss.build_selection_view(
        "2026-09-07",
        selection_dir=_MemDir({"2026-09-07.md": _SEL_A}),
        review_dir=_MemDir({"2026-09-07.md": _REVIEW_OVERLAP}),
    )
    rows = {r["code"]: r for r in view["盘后"]["选股"]["ranking"]}
    # 605007 在当日复盘里 → 复盘结果 = 主表态词 + α
    assert "买入" in rows["605007"]["review"] and "+2.57pp" in rows["605007"]["review"]
    # 688262 不在当日复盘 → 复盘结果空串(展示层渲染「—」)
    assert rows["688262"]["review"] == ""
    # 午盘无数据源 → 恒为空串占位
    assert rows["605007"]["midday"] == "" and rows["688262"]["midday"] == ""


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
