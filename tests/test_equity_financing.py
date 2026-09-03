"""存量融资与解禁(D·固定一问)单测 —— 全部 mock 网络,锁语义不锁实现。

锁住的语义(对应 docs/计划/09-03复盘反哺排期.md §5 与任务硬红线):
1. **防未来函数(硬红线)**:披露日晚于 as_of 的记录必须被剔除,且剔除条数显式计数;
   解禁日/到期日**可以**是未来日期(已披露的未来安排,不是未来价格)——不能被误剔。
2. **缺数据显式降级不静默**:源失败 → 源状态标"源不可得" + 降级[] 有条目,
   不能拿空列表冒充"确实没有"。
3. **skip-if-cached 幂等**:缓存新鲜时不再发请求,重跑产物一致。
4. **字段进 record 且 provenance 标记正确**:record["financing"] 有值 → provenance.financing=True。
5. 前瞻收益字段(em 的 解禁后20日涨跌幅)不得进落盘 payload。
"""
from __future__ import annotations

import pandas as pd
import pytest

from tools.collectors import equity_financing as efin
from tools.store import repo as store

# ————————————————————————————————————————————————
# 固件:mock 的源返回(照实调过的真实列名/量纲构造)
# ————————————————————————————————————————————————
CB_UNI = [
    {"债券代码": "113706", "债券简称": "金帝转债", "申购日期": "2026-06-15",
     "正股代码": "603270", "正股简称": "金帝股份", "转股价": 30.62, "债现价": 193.788,
     "转股溢价率": 69.78, "发行规模": 9.7, "上市时间": "2026-07-10", "信用评级": "AA"},
    # 未来才申购/上市的新债:披露日 2026-12-01 > as_of → 必须被剔
    {"债券代码": "113999", "债券简称": "未来转债", "申购日期": "2026-12-01",
     "正股代码": "603270", "转股价": 20.0, "发行规模": 5.0, "上市时间": "2026-12-20",
     "信用评级": "AA-"},
]
CB_DETAIL = {
    "113706": {"SECURITY_CODE": "113706", "SECURITY_NAME_ABBR": "金帝转债",
               "CONVERT_STOCK_CODE": "603270", "LISTING_DATE": "2026-07-10 00:00:00",
               "DELIST_DATE": None, "EXPIRE_DATE": "2032-06-15 00:00:00",
               "TRANSFER_START_DATE": "2026-12-22 00:00:00",
               "TRANSFER_END_DATE": "2032-06-14 00:00:00", "ACTUAL_ISSUE_SCALE": 9.7,
               "INITIAL_TRANSFER_PRICE": 30.62, "TRANSFER_PRICE": 30.62,
               "REDEEM_TRIG_PRICE": 39.81, "RESALE_TRIG_PRICE": 21.43,
               "IS_REDEEM": "是", "IS_SELLBACK": "是", "RATING": "AA",
               "CURRENT_BOND_PRICE": 193.788, "TRANSFER_PREMIUM_RATIO": 69.78},
    "113999": {"SECURITY_CODE": "113999", "LISTING_DATE": "2026-12-20 00:00:00",
               "DELIST_DATE": None, "EXPIRE_DATE": "2032-12-20 00:00:00",
               "TRANSFER_START_DATE": "2027-06-20 00:00:00", "ACTUAL_ISSUE_SCALE": 5.0,
               "TRANSFER_PRICE": 20.0, "IS_REDEEM": "是", "IS_SELLBACK": "是"},
}
SEO_UNI = [
    # 已实施定增(2025-01-10 发行 → 披露日在 as_of 之前),1年锁定 → 解锁 2026-01-20
    {"股票代码": "603270", "发行方式": "定向增发", "发行总数": 1e7, "发行价格": 12.0,
     "发行日期": "2025-01-10", "增发上市日期": "2025-01-20", "锁定期": "1年"},
    # 披露日晚于 as_of 的定增 → 必须剔
    {"股票代码": "603270", "发行方式": "定向增发", "发行总数": 2e7, "发行价格": 30.0,
     "发行日期": "2026-11-01", "增发上市日期": "2026-11-10", "锁定期": "3年"},
]
PLAN_ANN = [
    # 在推进的定增:公告时间 2026-08-20 ≤ as_of → 保留
    {"代码": "603270", "公告标题": "关于<em>向特定对象发行</em>股票预案的公告",
     "公告时间": "2026-08-20", "公告链接": "http://x/1"},
    # 可转债类公告(被 NEG 正则排除,不能误判成定增)
    {"代码": "603270", "公告标题": "向不特定对象发行可转换公司债券第一次临时受托管理事务报告",
     "公告时间": "2026-09-02", "公告链接": "http://x/2"},
    # 已完结阶段词 → 不算在途
    {"代码": "603270", "公告标题": "向特定对象发行股票发行结果公告",
     "公告时间": "2026-08-25", "公告链接": "http://x/3"},
    # 披露日晚于 as_of 的在途定增 → 必须剔
    {"代码": "603270", "公告标题": "关于非公开发行股票方案的公告",
     "公告时间": "2026-10-10", "公告链接": "http://x/4"},
]
UNLOCK_SINA = [
    # 未来解禁,但 2023 年就已披露 → **必须保留**(已披露的未来安排,不是未来函数)
    {"代码": "603270", "名称": "金帝股份", "解禁日期": "2027-08-31", "解禁数量": 11539.0,
     "解禁股流通市值": 33.3246, "上市批次": 6, "公告日期": "2023-08-31"},
    # 历史解禁,已披露 → 保留
    {"代码": "603270", "名称": "金帝股份", "解禁日期": "2024-12-23", "解禁数量": 1594.0,
     "解禁股流通市值": 3.40, "上市批次": 4, "公告日期": "2023-08-31"},
    # 未来解禁 + 披露日也在未来 → **必须剔**(as_of 当天还不知道这事)
    {"代码": "603270", "名称": "金帝股份", "解禁日期": "2027-01-15", "解禁数量": 500.0,
     "解禁股流通市值": 1.0, "上市批次": 7, "公告日期": "2026-10-01"},
]
UNLOCK_EM = [
    {"序号": 1, "解禁时间": "2027-09-01", "解禁股东数": 4, "解禁数量": 1.1539e8,
     "实际解禁数量": 1.1539e8, "未解禁数量": 3.3e7, "实际解禁数量市值": 4.03288e9,
     "占总市值比例": 0.526638, "占流通市值比例": 1.631723,
     "解禁前一交易日收盘价": 34.95, "限售股类型": "追加承诺限售股份上市流通",
     "解禁前20日涨跌幅": None, "解禁后20日涨跌幅": 12.34},   # ← 前瞻收益,不得落盘
    {"序号": 2, "解禁时间": "2024-12-23", "解禁股东数": 4, "解禁数量": 1.594e7,
     "实际解禁数量": 1.594e7, "未解禁数量": 1.4839e8, "实际解禁数量市值": 3.286828e8,
     "占总市值比例": 0.07275, "占流通市值比例": 0.225407,
     "解禁前一交易日收盘价": 20.62, "限售股类型": "首发原股东限售股份",
     "解禁前20日涨跌幅": 3.94, "解禁后20日涨跌幅": -1.71},
]

AS_OF = "2026-09-03"
SHARES = 1.896e8            # 总股本(股),用于摊薄%:9.7亿/30.62 ≈ 3168万股 → ≈16.7%


@pytest.fixture()
def mocked(monkeypatch, tmp_path):
    """把三维度所有网络出口 mock 掉,raw 根指到 tmp;返回调用计数器。"""
    calls: dict[str, int] = {}

    def _bump(k):
        calls[k] = calls.get(k, 0) + 1

    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    monkeypatch.setattr(efin, "_FETCH_SLEEP", 0.0)
    efin.reset_market_cache()

    monkeypatch.setattr(efin, "_fetch_cb_universe_raw",
                        lambda: (_bump("cb_uni"), CB_UNI)[1])
    monkeypatch.setattr(efin, "_fetch_seo_universe_raw",
                        lambda: (_bump("seo_uni"), SEO_UNI)[1])
    monkeypatch.setattr(efin, "_fetch_cb_detail_raw",
                        lambda bc: (_bump("cb_detail"), CB_DETAIL[str(bc)])[1])
    monkeypatch.setattr(efin, "_fetch_unlock_sina_raw",
                        lambda code: (_bump("sina"), UNLOCK_SINA)[1])
    monkeypatch.setattr(efin, "_fetch_unlock_em_raw",
                        lambda code: (_bump("em"), UNLOCK_EM)[1])
    monkeypatch.setattr(efin, "_fetch_plan_disclosures_raw",
                        lambda code, s, e: (_bump("plan"), PLAN_ANN)[1])
    yield calls
    efin.reset_market_cache()


# ————————————————————————————————————————————————
# 1. 防未来函数(硬红线)
# ————————————————————————————————————————————————
def test_披露日晚于asof被剔除(mocked):
    """披露日 > as_of 的可转债/定增/解禁记录必须全部剔掉,且剔除数显式计数。"""
    payload = efin.fetch_one("603270")
    # 落盘 payload 是**未过滤**的全量(便于回测任意 as_of 复用)
    assert len(payload["可转债"]) == 2
    s = efin.summarize_asof(payload, as_of=AS_OF, 总股本=SHARES)

    # 可转债:只剩 113706;未来转债(申购 2026-12-01)被剔
    codes = [c["债券代码"] for c in s["可转债"]["明细"]]
    assert codes == ["113706"], codes
    # 定增:已实施只剩 2025-01-10 那条;推进中只剩 2026-08-20 那条
    assert [x.get("发行日期") for x in s["定增"]["明细"] if x["阶段"] == "已实施"] == ["2025-01-10"]
    plans = [x for x in s["定增"]["明细"] if x["阶段"] == "推进中"]
    assert [p["披露日"] for p in plans] == ["2026-08-20"], plans
    # 解禁:公告日期 2026-10-01(未来披露)那条被剔
    assert "2027-01-15" not in [u["解禁日"] for u in s["解禁"]["明细"]]
    # 剔除计数显式(4 条:未来转债 / 未来已实施定增 / 未来推进中定增 / 未来披露的解禁)
    assert s["剔除"]["披露日晚于as_of"] == 4, s["剔除"]


def test_已披露的未来解禁日不被误剔(mocked):
    """解禁日在未来但**早已披露**(2023-08-31)→ 合法,必须保留。这是本维度的核心价值。"""
    s = efin.summarize_asof(efin.fetch_one("603270"), as_of=AS_OF, 总股本=SHARES)
    days = [u["解禁日"] for u in s["解禁"]["明细"]]
    assert "2027-08-31" in days, days
    assert s["解禁"]["下一次"]["解禁日"] == "2027-08-31"
    assert s["解禁"]["下一次"]["披露日"] == "2023-08-31"
    # 未来 90 日内无解禁 → 固定一问为 False(2027-08-31 距 2026-09-03 远超 90 天)
    assert s["固定一问"][f"有临近解禁_{efin.NEAR_UNLOCK_DAYS}日"] is False


def test_无披露日记录被剔且单独计数(mocked):
    """披露日缺失 → 不可见(宁缺勿滥,不猜),且计入 剔除.无披露日 而不是混进未来函数计数。"""
    payload = efin.fetch_one("603270")
    payload["解禁"][0]["披露日"] = None
    s = efin.summarize_asof(payload, as_of=AS_OF)
    assert s["剔除"]["无披露日"] == 1, s["剔除"]


def test_asof更早时点看不到当时未披露的转债(mocked):
    """同一份 payload、更早的 as_of → 转债尚未申购(2026-06-15)→ 固定一问必须答"无"。"""
    payload = efin.fetch_one("603270")
    s = efin.summarize_asof(payload, as_of="2026-05-01", 总股本=SHARES)
    assert s["固定一问"]["有存续可转债"] is False
    assert s["可转债"]["明细"] == []
    assert s["剔除"]["披露日晚于as_of"] >= 2


def test_前瞻收益字段不落盘(mocked):
    """em 的 解禁后20日涨跌幅 = 前瞻收益,归一阶段必须丢弃,不得出现在 payload 任何一层。"""
    payload = efin.fetch_one("603270")
    for u in payload["解禁"]:
        assert "解禁后20日涨跌幅" not in u
        assert "解禁前20日涨跌幅" not in u
    # 按采集日价折算的市值要改名 + 标口径,防被当历史价用
    matched = [u for u in payload["解禁"] if u.get("增强源") == "em"]
    assert matched and all(u["市值口径"] == "采集日价折算" for u in matched)


# ————————————————————————————————————————————————
# 2. 字段设计:有无 / 规模 / 时点 / 约束 四问可直读
# ————————————————————————————————————————————————
def test_固定一问与四维可直读(mocked):
    s = efin.summarize_asof(efin.fetch_one("603270"), as_of=AS_OF, 总股本=SHARES)
    q = s["固定一问"]
    assert q["有存续可转债"] is True                      # 有无
    assert q["有推进中定增"] is True
    cb = s["可转债"]
    assert cb["只数"] == 1 and cb["存续规模_亿"] == 9.7    # 规模
    assert 15.0 < cb["潜在摊薄_pct"] < 18.0, cb["潜在摊薄_pct"]
    d = cb["明细"][0]
    assert d["状态"] == "存续"
    assert d["转股起始日"] == "2026-12-22" and d["已进入转股期"] is False   # 时点
    assert d["到期日"] == "2032-06-15"
    assert d["强赎触发价"] == 39.81 and d["回售触发价"] == 21.43            # 约束
    assert any("未进入转股期" in n for n in s["约束提示"])
    assert any("强赎触发价" in n for n in s["约束提示"])


def test_解禁双源匹配增强(mocked):
    """sina(带披露日)为主 + em(占比/类型)就近匹配:2027-08-31 ↔ em 2027-09-01 差1天应匹配上。"""
    payload = efin.fetch_one("603270")
    u = [x for x in payload["解禁"] if x["解禁日"] == "2027-08-31"][0]
    assert u["增强源"] == "em" and u["解禁日_em"] == "2027-09-01"
    assert u["占流通市值_pct"] == pytest.approx(1.631723)
    assert u["限售股类型"] == "追加承诺限售股份上市流通"
    assert payload["源状态"]["解禁"] == "sina+em"


def test_解禁双源日期完全相同也要匹配上():
    """回归:日期差 == 0 是**完全匹配**,不能被 `gap or 默认值` 的假值兜底吃掉(真跑时踩过)。"""
    sina = [{"解禁日期": "2024-12-23", "解禁数量": 1594.0, "公告日期": "2023-08-31"}]
    em = [{"解禁时间": "2024-12-23", "实际解禁数量": 1.594e7,
           "占流通市值比例": 0.225407, "限售股类型": "首发原股东限售股份"}]
    rows, stat = efin.normalize_unlocks(sina, em)
    assert stat["em_未匹配"] == 0, stat
    assert rows[0]["增强源"] == "em"
    assert rows[0]["限售股类型"] == "首发原股东限售股份"


def test_em缺该批次时用总股本折算补规模(mocked):
    """真跑发现 em 解禁队列可能不含某批次(688569 的 2026-11-06)→ 占流通为 None,
    此时必须用 解禁数量/总股本 本地折算出 `占总股本_pct` 补上「规模」维度,不能只留 None。"""
    payload = efin.fetch_one("603270")
    for u in payload["解禁"]:                      # 模拟 em 全未匹配
        u["占流通市值_pct"] = None
        u["占总市值_pct"] = None
    s = efin.summarize_asof(payload, as_of=AS_OF, 总股本=SHARES)
    nxt = s["解禁"]["下一次"]
    assert nxt["占流通市值_pct"] is None
    assert nxt["占总股本_pct"] == pytest.approx(60.86, abs=0.5)   # 1.1539e8 / 1.896e8
    assert "占比口径" in s["解禁"]
    # 总股本也缺 → 老实给 None,不猜
    s2 = efin.summarize_asof(payload, as_of=AS_OF, 总股本=None)
    assert s2["解禁"]["下一次"]["占总股本_pct"] is None


def test_已到期与摘牌状态按asof判(mocked):
    payload = efin.fetch_one("603270")
    s = efin.summarize_asof(payload, as_of="2033-01-01", 总股本=SHARES)
    assert [c["状态"] for c in s["可转债"]["明细"]] == ["已到期", "已到期"]
    assert s["固定一问"]["有存续可转债"] is False


# ————————————————————————————————————————————————
# 3. 缺数据显式降级,不静默
# ————————————————————————————————————————————————
def test_可转债源失败标源不可得而非静默无转债(monkeypatch, mocked):
    def boom():
        raise ValueError("bond_zh_cov 返回空")
    monkeypatch.setattr(efin, "_fetch_cb_universe_raw", boom)
    efin.reset_market_cache()
    payload = efin.fetch_one("603270")
    assert payload["源状态"]["可转债"] == "源不可得"       # ≠ "ok_无转债"
    assert any("可转债" in x for x in payload["降级"])
    assert payload["可转债"] == []


def test_解禁sina失败则整维不可判(monkeypatch, mocked):
    """sina 挂了就没有披露日锚点 → em-only 记录全部剔除,维度标源不可得(不许无锚点混入)。"""
    def boom(code):
        raise ConnectionError("connection reset")
    monkeypatch.setattr(efin, "_fetch_unlock_sina_raw", boom)
    payload = efin.fetch_one("603270")
    assert payload["源状态"]["解禁"] == "源不可得"
    assert payload["解禁"] == []
    assert payload["解禁统计"]["em_未匹配"] == len(UNLOCK_EM)
    assert any("防未来函数" in x for x in payload["降级"])


def test_转债详情失败降级为一览字段(monkeypatch, mocked):
    def boom(bc):
        raise TimeoutError("timed out")
    monkeypatch.setattr(efin, "_fetch_cb_detail_raw", boom)
    payload = efin.fetch_one("603270")
    assert payload["源状态"]["可转债"] == "ok_详情降级"
    cb = payload["可转债"][0]
    assert cb["详情已取"] is False and cb["转股价"] == 30.62   # 一览字段仍在
    assert cb["强赎触发价"] is None                            # 条款缺 → None,不猜


def test_定增公告检索失败标不可得(monkeypatch, mocked):
    def boom(code, s, e):
        raise ValueError("cninfo 挂了")
    monkeypatch.setattr(efin, "_fetch_plan_disclosures_raw", boom)
    payload = efin.fetch_one("603270")
    assert "公告检索不可得" in payload["源状态"]["定增"]
    assert any("推进中定增不可判" in x for x in payload["降级"])


def test_summarize_对非dict返回None():
    assert efin.summarize_asof(None) is None
    assert efin.summarize_asof([]) is None


# ————————————————————————————————————————————————
# 4. skip-if-cached 幂等
# ————————————————————————————————————————————————
def test_skip_if_cached幂等不重发请求(mocked):
    store.set_active_date(AS_OF)
    try:
        first = efin.fetch_financing(["603270"])
        n_sina = mocked["sina"]
        assert n_sina == 1
        second = efin.fetch_financing(["603270"])       # 缓存新鲜 → 不再拉
        assert mocked["sina"] == n_sina, "缓存新鲜仍重发请求"
        assert second["603270"] == first["603270"]      # 幂等:产物一致
        efin.fetch_financing(["603270"], force=True)    # force 强刷
        assert mocked["sina"] == n_sina + 1
    finally:
        store.set_active_date(None)


def test_市场级名单一次运行只拉一次(mocked):
    store.set_active_date(AS_OF)
    try:
        efin.fetch_financing(["603270", "002811"])
        assert mocked["cb_uni"] == 1 and mocked["seo_uni"] == 1
    finally:
        store.set_active_date(None)


def test_load_financing按asof不读未来分区(mocked):
    store.set_active_date("2026-09-03")
    try:
        efin.fetch_financing(["603270"])
    finally:
        store.set_active_date(None)
    with pytest.raises(FileNotFoundError):
        efin.load_financing("603270", as_of="2026-08-01")   # 只有 09-03 分区 → 不许回读
    assert efin.load_financing("603270", as_of="2026-09-10")["code"] == "603270"


def test_build_financing_block无缓存返回None(mocked):
    assert efin.build_financing_block("000001", as_of=AS_OF) is None


# ————————————————————————————————————————————————
# 5. 进 record + provenance 标记 + 契约校验
# ————————————————————————————————————————————————
def test_进record且provenance标记(mocked, monkeypatch):
    from tools.analysis import serialize as sz
    from tools.contracts import record as rc

    store.set_active_date(AS_OF)
    try:
        efin.fetch_financing(["603270"])
    finally:
        store.set_active_date(None)

    # 只让 financing 这条链路真跑,其余数据块靠 _safe 降级为 None
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental",
                        lambda c: {"总市值": 66.28, "PE_TTM": 47.7, "PB": 2.9})
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    rec = sz.build_record("603270", AS_OF)

    assert rec["financing"] is not None
    assert rec["provenance"]["financing"] is True
    assert rec["financing"]["固定一问"]["有存续可转债"] is True
    assert rec["meta"]["code"] == "603270"
    # 契约层复查:披露日不得晚于 as_of
    assert rc.validate_record(rec) == []


def test_契约层拦住披露日晚于asof的记录():
    from tools.contracts import record as rc
    rec = {k: None for k in rc.REQUIRED_TOP}
    rec.update({"schema_version": "1.0",
                "meta": {"code": "603270", "name": "金帝股份", "as_of": "2026-09-03"},
                "events": [], "timeseries_refs": {}, "provenance": {},
                "financing": {"可转债": {"明细": [{"债券代码": "113999",
                                                  "披露日": "2026-12-01"}]}}})
    errs = rc.validate_record(rec)
    assert any("未来函数" in e for e in errs), errs
    rec["financing"]["可转债"]["明细"][0]["披露日"] = "2026-06-15"
    assert rc.validate_record(rec) == []


def test_provenance缺financing时为False(monkeypatch, tmp_path):
    from tools.analysis import serialize as sz
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(sz.market, "load_kline_recent", lambda c: None)
    monkeypatch.setattr(sz.fd, "load_fundamental", lambda c: {})
    monkeypatch.setattr(sz.an, "load_announcements", lambda c: [])
    rec = sz.build_record("999999", AS_OF)
    assert rec["financing"] is None
    assert rec["provenance"]["financing"] is False


# ————————————————————————————————————————————————
# 6. 归一小函数(纯函数,防重写时改坏量纲/正则)
# ————————————————————————————————————————————————
@pytest.mark.parametrize("txt,months", [("3年", 36), ("1年", 12), ("12个月", 12),
                                        ("36个月", 36), ("", None), (None, None)])
def test_锁定期解析(txt, months):
    assert efin._lock_months(txt) == months


def test_解锁日按月加(mocked):
    assert efin._add_months("2025-01-20", 12) == "2026-01-20"
    assert efin._add_months("2025-01-31", 1) == "2025-02-28"
    assert efin._add_months(None, 12) is None


def test_sina解禁数量单位为万股(mocked):
    payload = efin.fetch_one("603270")
    u = [x for x in payload["解禁"] if x["解禁日"] == "2027-08-31"][0]
    assert u["解禁数量_股"] == pytest.approx(1.1539e8)   # 11539 万股


def test_代码零填充():
    assert efin._norm_code(2811) == "002811"      # sina 会返 '2811'
    assert efin._norm_code("603270") == "603270"
    assert efin._norm_code(None) == ""


def test_定增标题正则不把可转债误判为定增():
    assert efin.normalize_seo_plan(
        {"公告标题": "向不特定对象发行可转换公司债券预案", "公告时间": "2026-01-01"}) is None
    assert efin.normalize_seo_plan(
        {"公告标题": "向特定对象发行股票预案", "公告时间": "2026-01-01"})["阶段"] == "推进中"


def test_normalize_unlocks空输入():
    rows, stat = efin.normalize_unlocks([], [])
    assert rows == [] and stat == {"sina_条数": 0, "em_条数": 0, "em_未匹配": 0}


def test_fetch原始出口空返回是可判定的():
    """源返回空 → 抛 ValueError(可判定),不返回空 DataFrame 静默成"没有"。"""
    import types
    fake = types.SimpleNamespace(
        bond_zh_cov=lambda: pd.DataFrame(),
        stock_qbzf_em=lambda: pd.DataFrame(),
        bond_zh_cov_info=lambda **kw: pd.DataFrame(),
        stock_restricted_release_queue_sina=lambda **kw: pd.DataFrame(),
    )
    import sys
    old = sys.modules.get("akshare")
    sys.modules["akshare"] = fake
    try:
        with pytest.raises(ValueError):
            efin._fetch_cb_universe_raw()
        with pytest.raises(ValueError):
            efin._fetch_seo_universe_raw()
        with pytest.raises(ValueError):
            efin._fetch_cb_detail_raw("113706")
        # 解禁为空是**正常业务态**(该票从未有限售股)→ [] 而非抛
        assert efin._fetch_unlock_sina_raw("600000") == []
    finally:
        if old is not None:
            sys.modules["akshare"] = old
        else:
            sys.modules.pop("akshare", None)
