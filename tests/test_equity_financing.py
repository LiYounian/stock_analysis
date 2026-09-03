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
# 定增过程公告:**两笔**。笔① 2025 年那笔已走到「发行结果」→ 整笔已实施;
# 笔② 2026-08 起在推进(两条过程公告 → 只能算 1 笔,不能按条数算 2 笔)。
PLAN_ANN = [
    # —— 笔①:预案 → 受理 → 发行结果(终局)——
    {"代码": "603270", "公告标题": "关于<em>向特定对象发行</em>股票预案的公告",
     "公告时间": "2025-02-10", "公告链接": "http://x/a1"},
    {"代码": "603270", "公告标题": "关于向特定对象发行股票申请获得上海证券交易所受理的公告",
     "公告时间": "2025-03-05", "公告链接": "http://x/a2"},
    {"代码": "603270", "公告标题": "向特定对象发行股票发行结果公告",
     "公告时间": "2025-06-20", "公告链接": "http://x/a3"},
    # —— 笔②:在推进(无任何终局信号)——
    {"代码": "603270", "公告标题": "关于<em>向特定对象发行</em>股票预案的公告",
     "公告时间": "2026-08-20", "公告链接": "http://x/b1"},
    {"代码": "603270", "公告标题": "关于向特定对象发行股票申请文件审核问询函回复的提示性公告",
     "公告时间": "2026-08-22", "公告链接": "http://x/b2"},
    # 可转债类公告(被 NEG 正则排除,不能误判成定增)
    {"代码": "603270", "公告标题": "向不特定对象发行可转换公司债券第一次临时受托管理事务报告",
     "公告时间": "2026-09-02", "公告链接": "http://x/2"},
    # 披露日晚于 as_of 的在途定增 → 必须剔
    {"代码": "603270", "公告标题": "关于非公开发行股票方案的公告",
     "公告时间": "2026-10-10", "公告链接": "http://x/4"},
]

# 603161 科华控股实测形状:**一笔**已完成的定增拖出十几条过程公告
# (预案→受理→问询回复→同意注册批复→募集说明书→发行过程和认购对象合规性审核报告)。
# 旧口径按公告条数计数 → 报「推进中 12」;真相是同一笔、且已于 2026-08 发行完毕。
PLAN_ANN_603161 = [
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票预案披露的提示性公告",
     "公告时间": "2025-08-23"},
    {"公告标题": "科华控股股份有限公司2025年度向特定对象发行A股股票方案论证分析报告",
     "公告时间": "2025-08-23"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票申请获得上海证券交易所受理的公告",
     "公告时间": "2026-02-27"},
    {"公告标题": "科华控股股份有限公司向特定对象发行股票证券募集说明书(申报稿)",
     "公告时间": "2026-02-27"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行股票申请文件审核问询函回复的提示性公告",
     "公告时间": "2026-03-25"},
    {"公告标题": "关于科华控股股份有限公司向特定对象发行股票申请文件审核问询函的回复",
     "公告时间": "2026-03-25"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票申请收到上海证券交易所"
                 "审核意见通知的公告", "公告时间": "2026-05-30"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票申请获得中国证券监督管理委员会"
                 "同意注册批复的公告", "公告时间": "2026-07-17"},
    {"公告标题": "科华控股股份有限公司向特定对象发行股票证券募集说明书(注册稿)",
     "公告时间": "2026-07-17"},
    # ↓ 发行**完成后**的配套/程序文件:标题带「审核报告」,但只可能出现在发行之后
    {"公告标题": "东海证券股份有限公司关于科华控股股份有限公司2025年度向特定对象发行股票"
                 "发行过程和认购对象合规性审核报告", "公告时间": "2026-08-08"},
]
# 同一笔的**收尾文件串**(实测:发行结果那几天连出 7 条终局类文件)。
# 不做尾部合并就会把**一笔**报成「已实施 7 笔」—— 和「推进中 12」是同一种荒谬。
PLAN_ANN_603161_TAIL = [
    {"公告标题": "北京德恒律师事务所关于科华控股股份有限公司向特定对象发行A股股票"
                 "发行过程和认购对象合规性的法律意见", "公告时间": "2026-08-08"},
    {"公告标题": "科华控股股份有限公司2025年度向特定对象发行A股股票发行情况报告书",
     "公告时间": "2026-08-08"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票发行情况报告书披露的提示性公告",
     "公告时间": "2026-08-08"},
    {"公告标题": "科华控股股份有限公司2025年度向特定对象发行A股股票上市公告书",
     "公告时间": "2026-08-11"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票上市公告书披露的提示性公告",
     "公告时间": "2026-08-11"},
    {"公告标题": "科华控股股份有限公司关于向特定对象发行A股股票发行结果暨股本变动公告",
     "公告时间": "2026-08-11"},
]
# 科华控股那笔的结构化已实施记录(qbzf):2026-08-03 发行、锁 3 年 → 解锁 2029-08-07
SEO_DONE_603161 = [
    {"股票代码": "603161", "发行方式": "定向增发", "发行总数": 4.2e7, "发行价格": 8.5,
     "发行日期": "2026-08-03", "增发上市日期": "2026-08-07", "锁定期": "3年"},
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
    # 定增:已实施只剩 2025-01-10 那条;2026-10-10 那条在途公告(披露日在未来)必须不可见
    assert [x.get("发行日期") for x in s["定增"]["明细"] if x["阶段"] == "已实施"] == ["2025-01-10"]
    assert "2026-10-10" not in [x.get("披露日") for x in s["定增"]["明细"]]
    # 推进中按**笔**计:只有笔②(2026-08 起)在推进;笔①已走到发行结果 → 不算
    assert s["定增"]["推进中"] == 1, s["定增"]["笔"]
    assert s["定增"]["最近推进中披露日"] == "2026-08-22"
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


def test_build_financing_block无缓存返回假值且记显式降级台账(mocked):
    """无缓存 → 必须返回**假值**(不能返回真值空块,否则 serialize 的
    `provenance.financing = bool(block)` 会从「静默缺失」升级成「谎报有数据」);
    同时必须留下**可读的降级台账**,把静默变成有声(闭环里能查出哪些票其实没采)。"""
    efin.reset_missing_financing()
    assert not efin.build_financing_block("000001", as_of=AS_OF)
    miss = efin.missing_financing()
    assert [m["code"] for m in miss] == ["000001"], miss
    assert miss[0]["as_of"] == AS_OF and "缓存" in miss[0]["原因"]
    assert "fetch_financing" in miss[0]["补救"]        # 台账要带补救办法,不只是报错
    efin.reset_missing_financing()
    assert efin.missing_financing() == []


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


# ————————————————————————————————————————————————
# 7. 定增阶段判定与「按笔聚合」(高 severity bug 回归锁)
#
# 背景(为什么有这一节):旧口径把定增「推进中」按**公告条数**计,且阶段判定只看
# LIVE 白名单。603161 科华控股那笔定增 2026-08-07 已完成登记托管、08-11 公告发行结果、
# 锁定至 2029-08-07,却被报成 `定增.推进中 = 12` —— 12 条全是**同一笔已完成定增**的
# 过程公告(其中「发行过程和认购对象合规性的审核报告」是发行**完成后**的配套文件,
# 因标题带「审核」被 LIVE 正则命中)。消费侧若读 `推进中 > 0` 就打「有再融资摊薄压力」
# 标签,对这只票**完全反向**(摊薄已经发生、且锁 36 个月)。
#
# 本节断言锁住四件事,任何一条被未来重写破坏都必须红:
#   ① 公告条数 ≠ 笔数:同一笔的多条过程公告不得按条数累加;
#   ② 序列里出现终局信号 → 整笔关帐,更早的在途公告不得再让它算推进中;
#   ③ 发行后配套文件(合规性审核报告/验资报告/三方监管协议/股本变动)不得判成推进中;
#   ④ 真在推进的定增(只有预案/问询/批复、无终局)**仍要**被判出来——别修过头灭了真信号;
#   ⑤ 已实施 与 已终止 **不同桶**(摊薄已发生 vs 摊薄永不发生,含义相反);
#   ⑥ 关帐必须过 as-of 闸门:终局公告披露之前的时点,该笔仍算推进中(防未来函数不得回退)。
# ————————————————————————————————————————————————
def _plans(*items) -> list[dict]:
    """(标题, 公告时间) → 归一后的定增公告记录列表(过滤掉不成阶段的)。"""
    out = []
    for title, when in items:
        rec = efin.normalize_seo_plan({"公告标题": title, "公告时间": when})
        if rec:
            out.append(rec)
    return out


def _summarize_plans(plan_rows, done_rows=None, as_of=AS_OF, code="603161"):
    """只走定增维度的 summarize(payload 里其余维度留空),返回 定增 块。"""
    seo = [efin.normalize_seo_done(r) for r in (done_rows or [])]
    for r in plan_rows:
        rec = efin.normalize_seo_plan(r) if "公告标题" in r else r
        if rec:
            seo.append(rec)
    payload = {"code": code, "可转债": [], "定增": seo, "解禁": [],
               "解禁统计": {}, "源状态": {"定增": "qbzf+公告检索"}, "降级": []}
    return efin.summarize_asof(payload, as_of=as_of)["定增"]


def test_同一笔定增的多条过程公告不得按条数累加():
    """① 12 条过程公告是**一笔**定增。`推进中` 报笔数;公告条数另有字段承载,不混淆。"""
    # 去掉最后那条发行后配套文件 → 这笔在 as_of 当天看还没有终局证据 → 推进中 = 1 笔
    live_only = [r for r in PLAN_ANN_603161 if "合规性审核报告" not in r["公告标题"]]
    blk = _summarize_plans(live_only)
    assert blk["推进中"] == 1, blk["笔"]                      # ← 不是 9、不是 12
    assert blk["推进中公告条数"] == len(live_only) == 9        # 条数照实报,只是不当笔数用
    assert blk["笔数"] == {"推进中": 1, "已实施": 0, "已终止": 0}
    assert blk["笔"][0]["起始披露日"] == "2025-08-23"
    assert blk["笔"][0]["最新披露日"] == "2026-07-17"


def test_公告序列含终局信号则整笔判已实施不再计推进中():
    """② 603161 真实形状:9 条在途 + 1 条发行后配套文件 → 整笔已实施,推进中必须为 0。"""
    blk = _summarize_plans(PLAN_ANN_603161)
    assert blk["推进中"] == 0, blk["笔"]
    assert blk["笔数"]["已实施"] == 1
    assert blk["笔"][0]["终结披露日"] == "2026-08-08"
    assert blk["笔"][0]["终结依据"] == "公告标题"
    # 明细里的在途公告也要能看出「它属于哪一笔、那笔已经完了」——否则直读明细会重犯反向误判
    live_anns = [x for x in blk["明细"] if x.get("阶段") == "推进中"]
    assert live_anns and all(x["笔状态"] == "已实施" for x in live_anns), live_anns


def test_603161实测形状_已实施锁定中而非推进中():
    """③+②:结构化已实施(2026-08-03 发行、锁 3 年)在场时,正确分类是「已实施_锁定中」。

    这是本 bug 的**实票回归锁**:as_of=2026-09-03 时 603161 必须
    推进中 = 0 / 已实施_锁定中 = 1 / 解锁日 = 2029-08-07 / 固定一问「有推进中定增」= False。
    """
    blk = _summarize_plans(PLAN_ANN_603161, done_rows=SEO_DONE_603161)
    assert blk["推进中"] == 0, blk["笔"]
    assert blk["已实施_锁定中"] == 1
    done = [x for x in blk["明细"] if x["阶段"] == "已实施"][0]
    assert done["解锁日"] == "2029-08-07" and done["锁定月数"] == 36
    s = _summarize_plans(PLAN_ANN_603161, done_rows=SEO_DONE_603161)
    assert s is not None


def test_已实施的固定一问与约束提示不得说成待摊薄压力():
    """消费侧后果直锁:已完成的定增必须被描述成「摊薄已发生」,不能进「有推进中定增」。"""
    payload = {"code": "603161", "可转债": [], "解禁": [], "解禁统计": {},
               "源状态": {}, "降级": [],
               "定增": [efin.normalize_seo_done(SEO_DONE_603161[0])]
                       + [r for r in (efin.normalize_seo_plan(x) for x in PLAN_ANN_603161) if r]}
    s = efin.summarize_asof(payload, as_of=AS_OF)
    assert s["固定一问"]["有推进中定增"] is False
    assert any("摊薄**已发生**" in n for n in s["约束提示"]), s["约束提示"]
    assert not any("摊薄尚未发生" in n for n in s["约束提示"]), s["约束提示"]


def test_qbzf已实施记录可独立关帐():
    """③ 兜底:发行结果公告标题没被命中,但结构化源已收录发行 → 那笔也必须关帐。"""
    live_only = [r for r in PLAN_ANN_603161 if "合规性审核报告" not in r["公告标题"]]
    blk = _summarize_plans(live_only, done_rows=SEO_DONE_603161)
    assert blk["推进中"] == 0, blk["笔"]
    assert blk["笔"][0]["终结依据"] == "已实施增发(qbzf 发行日期)"
    # 发行日期早于起笔日的老定增**不得**关掉后来新开的那笔(别把兜底修成万能关帐)
    old_done = [{"股票代码": "603161", "发行方式": "定向增发", "发行日期": "2019-01-10",
                 "增发上市日期": "2019-01-20", "锁定期": "1年"}]
    blk2 = _summarize_plans(live_only, done_rows=old_done)
    assert blk2["推进中"] == 1, blk2["笔"]


@pytest.mark.parametrize("title", [
    "东海证券股份有限公司关于科华控股股份有限公司2025年度向特定对象发行股票"
    "发行过程和认购对象合规性审核报告",
    "关于向特定对象发行股票募集资金到账的验资报告",
    "关于向特定对象发行股票募集资金专户存储三方监管协议的公告",
    "关于向特定对象发行股票新增股份上市及股本变动的公告",
    "关于非公开发行股票完成后变更注册资本的公告",
    "向特定对象发行股票发行情况报告书暨上市公告书",
    "向特定对象发行股票发行结果公告",
])
def test_发行后配套文件不得判为推进中(title):
    """③ 这些文件只可能出现在**发行完成之后**,标题却带「审核/方案」等在途词。

    判定顺序(终止→已实施→进行)是语义的一部分:先看终局信号,才不会把发完的当在途。
    """
    assert efin.plan_stage_signal(title) == "完成", title
    rec = efin.normalize_seo_plan({"公告标题": title, "公告时间": "2026-08-08"})
    assert rec is not None and rec["阶段"] != "推进中" and rec["阶段信号"] == "完成"
    # 单条配套文件自己也不能撑起一个「推进中」的笔
    blk = _summarize_plans([{"公告标题": title, "公告时间": "2026-08-08"}])
    assert blk["推进中"] == 0 and blk["笔数"]["已实施"] == 1, blk["笔"]


@pytest.mark.parametrize("title,sig", [
    ("关于向特定对象发行A股股票预案披露的提示性公告", "进行"),
    ("关于向特定对象发行A股股票申请获得上海证券交易所受理的公告", "进行"),
    ("关于向特定对象发行股票申请文件审核问询函回复的提示性公告", "进行"),
    ("关于向特定对象发行A股股票申请获得中国证监会同意注册批复的公告", "进行"),
    ("向特定对象发行股票证券募集说明书(注册稿)", "进行"),
    ("关于终止向特定对象发行股票并撤回申请文件的公告", "终止"),
    ("关于向特定对象发行股票决议有效期及授权有效期届满自动失效的公告", "终止"),
    ("关于向特定对象发行股票申请文件不予注册的公告", "终止"),
    # 发行落地后的收尾公告会出现「协议终止」——那是协议终止、不是发行终止:
    # 已实施的证据强于终止词,故必须判「完成」(否则会说成「摊薄不会发生」,方向照样是反的)
    ("关于向特定对象发行股票募集资金专户三方监管协议终止的公告", "完成"),
    # 「中止」是可恢复的暂停,不是终局 → 有意仍算在途(宁可保留真信号)
    ("关于向特定对象发行股票审核中止的公告", "进行"),
    ("关于向特定对象发行股票摊薄即期回报及填补措施的公告", None),
])
def test_阶段信号分类(title, sig):
    assert efin.plan_stage_signal(title) == sig, title


def test_真正在推进的定增仍要被判出来():
    """④ 别修过头:只有预案/问询/批复、无任何终局信号 → 必须仍判「推进中」。"""
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2026-05-08"),
                  ("关于向特定对象发行股票申请文件审核问询函回复的提示性公告", "2026-07-01"),
                  ("关于向特定对象发行A股股票申请获得中国证监会同意注册批复的公告", "2026-08-15"))
    blk = _summarize_plans(rows)
    assert blk["推进中"] == 1 and blk["推进中公告条数"] == 3, blk["笔"]
    assert blk["最近推进中披露日"] == "2026-08-15"
    payload = {"code": "600000", "可转债": [], "定增": rows, "解禁": [], "解禁统计": {},
               "源状态": {}, "降级": []}
    s = efin.summarize_asof(payload, as_of=AS_OF)
    assert s["固定一问"]["有推进中定增"] is True
    assert any("摊薄尚未发生" in n for n in s["约束提示"]), s["约束提示"]


def test_已终止与已实施不同桶():
    """⑤ 终止=股份从未发出(摊薄永不发生);已实施=股份已发出(摊薄已发生)。含义相反,不可合并。"""
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2026-01-08"),
                  ("关于终止向特定对象发行股票并撤回申请文件的公告", "2026-06-30"))
    blk = _summarize_plans(rows)
    assert blk["推进中"] == 0
    assert blk["笔数"] == {"推进中": 0, "已实施": 0, "已终止": 1}, blk["笔"]
    assert blk["笔"][0]["状态"] == "已终止"
    payload = {"code": "600000", "可转债": [], "定增": rows, "解禁": [], "解禁统计": {},
               "源状态": {}, "降级": []}
    s = efin.summarize_asof(payload, as_of=AS_OF)
    assert s["固定一问"]["有推进中定增"] is False
    assert any("不会发生" in n for n in s["约束提示"]), s["约束提示"]
    assert not any("已发生" in n for n in s["约束提示"]), s["约束提示"]


def test_终止后新开的一笔算新的推进中():
    """关帐后再出现在途公告 → 是**新的一笔**,必须重新算推进中(不能被前一笔的终局压住)。"""
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2025-01-08"),
                  ("关于终止向特定对象发行股票并撤回申请文件的公告", "2025-06-30"),
                  ("关于向特定对象发行A股股票预案披露的提示性公告", "2026-08-01"))
    blk = _summarize_plans(rows)
    assert blk["笔数"] == {"推进中": 1, "已实施": 0, "已终止": 1}, blk["笔"]
    assert blk["笔"][1]["起始披露日"] == "2026-08-01"


def test_关帐必须过asof闸门_终局公告披露前该笔仍算推进中():
    """⑥ 防未来函数不得回退:用**未来**的终局公告去关掉一笔,等于给历史时点注入未来信息。

    同一份 payload,as_of 落在发行结果公告之前 → 那笔当时确实还在推进,必须判推进中;
    且被剔掉的未来公告要计入 `剔除.披露日晚于as_of`(显式,不静默)。
    """
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2026-03-02"),
                  ("向特定对象发行股票发行结果公告", "2026-08-11"))
    payload = {"code": "603161", "可转债": [], "定增": rows, "解禁": [], "解禁统计": {},
               "源状态": {}, "降级": []}
    早 = efin.summarize_asof(payload, as_of="2026-07-31")
    assert 早["定增"]["推进中"] == 1, 早["定增"]["笔"]
    assert 早["剔除"]["披露日晚于as_of"] == 1, 早["剔除"]
    晚 = efin.summarize_asof(payload, as_of="2026-08-31")
    assert 晚["定增"]["推进中"] == 0, 晚["定增"]["笔"]
    assert 晚["剔除"]["披露日晚于as_of"] == 0


def test_无披露日的定增公告不得进聚合():
    """防未来函数的同一条红线:无披露日 → 不可见、不进笔(宁缺勿滥,不猜)。"""
    rows = [{"阶段": "推进中", "阶段信号": "进行", "披露日": None, "标题": "无日期的预案"}]
    assert efin.aggregate_plan_rounds(rows) == []
    payload = {"code": "600000", "可转债": [], "定增": rows, "解禁": [], "解禁统计": {},
               "源状态": {}, "降级": []}
    s = efin.summarize_asof(payload, as_of=AS_OF)
    assert s["定增"]["推进中"] == 0 and s["剔除"]["无披露日"] == 1


def test_计数口径写进产物_防口径被下游误读():
    """`推进中` 是笔数这件事必须写在产物里(下游读到 12 时才会怀疑口径,而不是当 12 笔用)。"""
    blk = _summarize_plans(PLAN_ANN_603161)
    assert "笔数" in blk["计数口径"] and "公告条数" in blk["计数口径"]
    assert "已实施" in blk["计数口径"] and "已终止" in blk["计数口径"]


def test_收尾文件串归入同一笔而不是各开一笔():
    """实票回归锁(603161 实跑逮到的第二个计数缺陷):发行落地那几天连出 7 条终局类文件
    (法律意见/发行情况报告书/上市公告书/发行结果暨股本变动…)。若每条终局公告各开一笔,
    就会把**一笔**已实施定增报成「已实施 7 笔」—— 与「推进中 12」是同一种「条数当笔数」的病。
    """
    blk = _summarize_plans(PLAN_ANN_603161 + PLAN_ANN_603161_TAIL,
                           done_rows=SEO_DONE_603161)
    assert blk["笔数"] == {"推进中": 0, "已实施": 1, "已终止": 0}, blk["笔"]
    r = blk["笔"][0]
    assert r["公告条数"] == len(PLAN_ANN_603161) + len(PLAN_ANN_603161_TAIL)
    assert r["起始披露日"] == "2025-08-23" and r["终结披露日"] == "2026-08-11"
    assert blk["已实施_锁定中"] == 1 and blk["推进中"] == 0


def test_收尾窗口之外的终局公告仍算新的一笔():
    """尾部合并不能变成「万能吞并」:隔了远超收尾窗口的终局公告,是另一笔(只看到尾巴)。"""
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2020-01-08"),
                  ("向特定对象发行股票发行结果公告", "2020-06-30"),
                  ("向特定对象发行股票发行结果公告", "2026-06-30"))
    blk = _summarize_plans(rows)
    assert blk["笔数"]["已实施"] == 2, blk["笔"]


def test_收尾的协议终止公告不得把已实施翻成已终止():
    """已实施的证据强于终止词:一笔股份已发出的定增,不因后续「专户/协议终止」类公告变回没发出。"""
    rows = _plans(("关于向特定对象发行A股股票预案披露的提示性公告", "2026-01-08"),
                  ("向特定对象发行股票发行结果公告", "2026-06-30"),
                  ("关于终止向特定对象发行股票募集资金投资项目的公告", "2026-07-20"))
    # 第三条确实被判成「终止」信号(是**项目**终止,不是发行终止)——正是这个守卫要挡的输入
    assert efin.plan_stage_signal("关于终止向特定对象发行股票募集资金投资项目的公告") == "终止"
    blk = _summarize_plans(rows)
    assert blk["笔数"] == {"推进中": 0, "已实施": 1, "已终止": 0}, blk["笔"]
    assert blk["笔"][0]["公告条数"] == 3      # 收尾公告确实进了这一笔(不是被丢掉才「碰巧对」)
