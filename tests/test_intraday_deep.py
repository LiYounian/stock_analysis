"""headless 午盘深度选股 orchestrator 契约测试（hermetic，桩 client + tmp data-root）。

锁语义（约法6，防未来/13:00预算/候选消费/产物规格/headless不依赖窗口）：
  1. 候选消费：台账按数据面综合分排序取 Top-N；未就绪→轮询/降级不空跑。
  2. 防未来（硬红线）：news ≤11:30 cutoff 剔除含显式时刻的未来条目；record.as_of≤pick_date。
  3. 产物规格：md 第一行 PICKS 锚点（买入票）；逐票深度 + D-0 计划；canonical JSON 独立名过校验。
  4. headless 不依赖窗口：全程注入桩 client、不联网、不采集即跑通。
  5. 不污染：不写 每日选股.json / 日内_<date>.md。
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from tools.analysis import deep_analysis_inputs as di
from tools.pipeline import intraday_deep as idp


# ————————————————————————————————————————————————————————————————
# fixtures：造 tmp data-root（intraday 候选/快照 + analysis 单票 record）
# ————————————————————————————————————————————————————————————————
DATE = "2026-09-14"
CODES = ["000001", "600519", "300750"]


def _write(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    intraday = root / "intraday" / DATE
    analysis = root / "analysis" / DATE

    # stage1 早产候选（台账乱序，测排序）——数据面综合分：600519>000001>300750
    _write(intraday / "noon_candidates_stage1.json", {
        "as_of": DATE, "slot": "noon", "stage": "stage1", "freeze_label": "11:30 午休冻结",
        "买入代码": ["000001", "600519"], "规避代码": [],
        "买入": [], "规避": [],
        "台账": [
            {"候选排名": 2, "code": "000001", "name": "平安银行", "完整分": 1.2,
             "数据面综合分": 1.2, "候选来源": "反转低换手、半导体多因子", "理由": "低bias；放量反包"},
            {"候选排名": 1, "code": "600519", "name": "贵州茅台", "完整分": 1.8,
             "数据面综合分": 1.8, "候选来源": "趋势成长", "理由": "均线多头"},
            {"候选排名": 3, "code": "300750", "name": "宁德时代", "完整分": 0.9,
             "数据面综合分": 0.9, "候选来源": "超跌反抽", "理由": "超卖"},
        ],
    })
    # 11:30 冻结快照（quotes）
    _write(intraday / "noon_screen_snapshot.json", {
        "000001": {"price": 12.0, "open": 11.8, "high": 12.3, "low": 11.7},
        "600519": {"price": 1500.0, "open": 1490.0, "high": 1520.0, "low": 1480.0},
        "300750": {"price": 200.0, "open": 205.0, "high": 206.0, "low": 198.0},
    })
    # 单票 record（客观字段回填源；as_of≤date 防未来合规）
    for c, name, close, pct in [("000001", "平安银行", 12.0, 1.5),
                                 ("600519", "贵州茅台", 1500.0, 2.1),
                                 ("300750", "宁德时代", 200.0, -1.0)]:
        _write(analysis / f"{c}.json", {
            "meta": {"name": name, "industry": "测试行业", "as_of": DATE},
            "snapshot": {"close": close, "pct_chg": pct},
        })
    return root


class _StubClient:
    """桩 DeepSeek client：.extract 按 code 返回既定研判（600519/000001 买入，300750 规避）。"""
    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, instruction=""):
        self.calls += 1
        # 从事实文本里粗取 code（render_facts_text 首行含 code）
        buy = "600519" in text or "000001" in text
        avoid = "300750" in text
        if avoid and not (("600519" in text) or ("000001" in text)):
            return {"type": "规避", "stance": "规避", "dir_1d": "偏空", "dir_1d_conf": "中",
                    "dir_5d": "中性", "dir_5d_conf": "低", "sentiment_quality": "ok",
                    "key_reason": "超跌但无催化", "key_risk": "接飞刀", "alpha_beta": "β主导",
                    "watch_points": ["是否放量反包"]}
        return {"type": "买入候选", "stance": "买入", "dir_1d": "偏多", "dir_1d_conf": "中高",
                "dir_5d": "偏多", "dir_5d_conf": "中", "sentiment_quality": "ok",
                "key_reason": "均线多头+低bias", "key_risk": "大盘β风险", "alpha_beta": "α来自趋势",
                "watch_points": ["是否站上X", "量比≥Y吗"]}


@pytest.fixture(autouse=True)
def _no_real_experience(monkeypatch):
    """屏蔽真实经验库读取，保证 hermetic。"""
    from tools.analysis import deep_analysis as da
    monkeypatch.setattr(da.er, "recall_snippets_for",
                        lambda *a, **k: ([], "v-test"), raising=True)


def _fixed_now(hhmm="11:55"):
    h, m = (int(x) for x in hhmm.split(":"))
    return lambda: _dt.datetime(2026, 9, 14, h, m, 0)


# ————————————————————————————————————————————————————————————————
# 1. 候选消费 + 排序
# ————————————————————————————————————————————————————————————————
def test_topn_codes_sorted_by_score(data_root):
    intraday_root = data_root / "intraday"
    cand = idp.isc.read_noon_candidates(DATE, root=intraday_root, top=12, stage="stage1")
    codes, hint = idp.topn_codes(cand, top=3)
    assert codes == ["600519", "000001", "300750"]         # 数据面综合分降序
    assert hint["600519"] == [{"name": "趋势成长"}]
    assert {"name": "反转低换手"} in hint["000001"]         # 候选来源拆分


def test_poll_candidates_unavailable_then_degrade(tmp_path):
    """无任何候选 → 轮询到 deadline → 降级 auto 仍无 → (None, unavailable)，不空跑。"""
    intraday_root = tmp_path / "data" / "intraday"
    empty_docs = tmp_path / "empty_docs"                     # 隔离 md 台账回退，不读生产 docs
    empty_docs.mkdir()
    cand, note = idp.poll_candidates(
        DATE, intraday_root, top=5, stage="stage1", selection_dir=empty_docs,
        interval_s=0, deadline_hhmm="11:00", now_fn=_fixed_now("11:55"), max_polls=1)
    assert cand is None
    assert "unavailable" in note


def test_poll_candidates_ready(data_root):
    intraday_root = data_root / "intraday"
    cand, note = idp.poll_candidates(
        DATE, intraday_root, top=12, stage="stage1",
        interval_s=0, now_fn=_fixed_now("11:55"))
    assert cand is not None and cand.get("source") == "json"
    assert "stage=stage1" in note


# ————————————————————————————————————————————————————————————————
# 2. 防未来：news ≤11:30 cutoff
# ————————————————————————————————————————————————————————————————
def test_news_cutoff_drops_intraday_future(tmp_path):
    analysis_root = tmp_path / "analysis"
    _write(analysis_root / DATE / "news_ai" / "000001.json", [
        {"time": "2026-09-14 09:30", "title": "早盘公告"},
        {"time": "2026-09-14 11:45", "title": "午后半小时新闻(未来)"},
        {"time": "2026-09-14", "title": "同日纯日期项(保守保留)"},
        {"time": "2026-09-13 15:00", "title": "昨日"},
    ])
    kept, dropped = di.load_news("000001", DATE, analysis_root,
                                 news_time_cutoff=f"{DATE} 11:30:00")
    titles = [k["title"] for k in kept]
    assert "午后半小时新闻(未来)" not in titles     # 显式时刻 >11:30 → 剔
    assert "早盘公告" in titles                     # 9:30 ≤11:30 → 留
    assert "同日纯日期项(保守保留)" in titles       # 纯日期不在 cutoff 判（date 级保留）
    assert dropped == 1


def test_news_cutoff_none_is_date_level(tmp_path):
    """cutoff=None → 原行为：同日 11:45 项保留（仅 date 级剔除）。"""
    analysis_root = tmp_path / "analysis"
    _write(analysis_root / DATE / "news_ai" / "000001.json", [
        {"time": "2026-09-14 11:45", "title": "午后"},
        {"time": "2026-09-15 09:00", "title": "隔日未来"},
    ])
    kept, dropped = di.load_news("000001", DATE, analysis_root)
    titles = [k["title"] for k in kept]
    assert "午后" in titles and "隔日未来" not in titles
    assert dropped == 1


# ————————————————————————————————————————————————————————————————
# 3+4+5. 端到端（headless 桩 client）：产物规格 + 不污染
# ————————————————————————————————————————————————————————————————
def test_end_to_end_headless(data_root, tmp_path):
    out_dir = tmp_path / "out"
    stub = _StubClient()
    rc = idp.run(
        DATE, data_root=data_root, out_dir=out_dir, top=3,
        collect=False, news_cutoff=True, force=True,
        deep_client=stub, now_fn=_fixed_now("11:55"))
    assert rc == 0
    assert stub.calls == 3                              # 3 票各调一次 DeepSeek（headless）

    md_path = out_dir / f"{idp.PRODUCT_MD_PREFIX}_{DATE}.md"
    assert md_path.exists()
    md = md_path.read_text(encoding="utf-8")
    # 产物规格：第一行 PICKS 锚点 = 买入票（600519,000001；300750 规避不进）
    first = md.splitlines()[0]
    assert first.startswith("<!-- PICKS:")
    assert "600519" in first and "000001" in first and "300750" not in first
    # 逐票深度 + D-0 计划渲染
    assert "逐票深度" in md and "D-0 交易计划" in md
    assert "止损" in md and "α/β 拆分" in md
    # 时点自证 + 防未来标注
    assert "时点自证" in md and "11:30" in md

    # canonical JSON 独立名，过校验；买入票 buy_rank 连续
    jpath = data_root / "analysis" / DATE / idp.CANONICAL_JSON_NAME
    assert jpath.exists()
    doc = json.loads(jpath.read_text(encoding="utf-8"))
    assert doc["meta"]["status"] == "ok"
    ranks = sorted(p["buy_rank"] for p in doc["picks"])
    assert ranks == [1, 2]
    assert {p["code"] for p in doc["picks"]} == {"600519", "000001"}
    # 客观字段回填（名称/价）
    p0 = next(p for p in doc["picks"] if p["code"] == "600519")
    assert p0["name"] == "贵州茅台" and p0["close"] == 1500.0

    # 不污染：不写 每日选股.json / 日内_<date>.md
    assert not (data_root / "analysis" / DATE / "每日选股.json").exists()
    assert not (out_dir / f"日内_{DATE}.md").exists()


def test_non_trading_day_exits_0(data_root, tmp_path, monkeypatch):
    monkeypatch.setattr(idp.cal, "is_trading_day", lambda d: False)
    rc = idp.run(DATE, data_root=data_root, out_dir=tmp_path / "out",
                 collect=False, force=False, deep_client=_StubClient(),
                 now_fn=_fixed_now("11:55"))
    assert rc == 0
    assert not (tmp_path / "out" / f"{idp.PRODUCT_MD_PREFIX}_{DATE}.md").exists()


def test_no_candidates_writes_skip(tmp_path):
    """候选不可用 → PICKS:none 跳过留痕 + status=skipped，不空跑。"""
    root = tmp_path / "data"
    (root / "analysis" / DATE).mkdir(parents=True)
    out_dir = tmp_path / "out"
    empty_docs = tmp_path / "empty_docs"                     # 隔离 md 台账回退
    empty_docs.mkdir()
    rc = idp.run(DATE, data_root=root, out_dir=out_dir, top=3,
                 collect=False, force=True, deep_client=_StubClient(), selection_dir=empty_docs,
                 now_fn=_fixed_now("12:30"), poll_deadline="12:20", poll_interval_s=0)
    assert rc == 0
    md = (out_dir / f"{idp.PRODUCT_MD_PREFIX}_{DATE}.md").read_text(encoding="utf-8")
    assert md.splitlines()[0] == "<!-- PICKS: none -->"
    doc = json.loads((root / "analysis" / DATE / idp.CANONICAL_JSON_NAME).read_text(encoding="utf-8"))
    assert doc["meta"]["status"] == "skipped"
