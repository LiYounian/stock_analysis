"""fund_flow 语义锁：档位表锁死 + 字段拒空 + 缺数据NA + 净占比折算口径 +
竞品板块内相对计算 + 无roster NA + 防未来 + G3 行数。

全用 tmp_path 构造脱敏合成 data-root（per-stock json + 主档K线parquet + sector_roster +
code_industry），不依赖主仓真数据；档位边界锁住"为什么改"的语义（防未来重写误删规则）。
"""
import json
import os

import pandas as pd
import pytest

from tools.pyramid.registry import get
from tools.pyramid._common import 格档, 字段
from tools.pyramid.tools.fund_flow_tool import (
    _净占比档, _连续天数档, _主买占比档, _户数环比档, _连减期数档, _换手率档, _竞品档,
    _f_竞品,
)
import tools.pyramid.tools  # noqa: F401  触发注册

_AS_OF = "2026-09-18"


# ── 合成 data-root 装配 ───────────────────────────────────────────────
def _write_kline(root, code, rows):
    """rows = [(date, close, amount, turnover), ...]；补齐 OHLCV 列。含 >as_of 行验防未来。"""
    d = os.path.join(root, "data", "master", "kline")
    os.makedirs(d, exist_ok=True)
    df = pd.DataFrame([
        {"date": dt, "open": c, "high": c, "low": c, "close": c,
         "volume": 1000.0, "amount": amt, "turnover": tn, "pct_chg": 0.0}
        for dt, c, amt, tn in rows
    ])
    df.to_parquet(os.path.join(d, f"{code}.parquet"))


def _write_pstock(root, code, payload):
    d = os.path.join(root, "data", "analysis", _AS_OF)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{code}.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _write_code_industry(root, mapping):
    d = os.path.join(root, "config")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "code_industry.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False)


def _write_roster(root, sector, roles, 更新日=_AS_OF):
    d = os.path.join(root, "data", "sector_roster")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sector}.json"), "w", encoding="utf-8") as f:
        json.dump({"板块": sector, "更新日": 更新日, "roles": roles}, f, ensure_ascii=False)


# ── 档位表锁（核心语义锁：阈值/档名一字不改）──────────────────────────
def test_档位表常量锁():
    assert _净占比档 == [
        (-5.0, "强流出", "主力大幅净流出"),
        (-1.0, "流出", "主力净流出"),
        (1.0, "中性", "主力进出均衡"),
        (5.0, "流入", "主力净流入"),
        (float("inf"), "强流入", "主力大幅净流入"),
    ]
    assert [(u, n) for u, n, _ in _主买占比档] == [
        (0.45, "弱"), (0.52, "均衡"), (0.56, "偏强"), (float("inf"), "强")]
    assert [(u, n) for u, n, _ in _户数环比档] == [
        (-10.0, "明显集中"), (-3.0, "集中"), (3.0, "平稳"),
        (10.0, "分散"), (float("inf"), "明显分散")]
    assert [(u, n) for u, n, _ in _换手率档] == [
        (1.0, "过低"), (3.0, "正常"), (7.0, "活跃"), (float("inf"), "过热炒作")]
    assert [(u, n) for u, n, _ in _连续天数档] == [
        (0, "无"), (2, "短"), (5, "持续"), (float("inf"), "强持续")]
    assert [(u, n) for u, n, _ in _连减期数档] == [
        (0, "无"), (2, "初步集中"), (5, "持续集中"), (float("inf"), "强持续集中")]
    assert [(u, n) for u, n, _ in _竞品档] == [
        (0.2, "龙头级"), (0.4, "前排"), (0.7, "中游"), (float("inf"), "尾部")]


def test_档位边界():
    # 净占比：±1 中性带、±5 强档
    assert 格档(-5.0, _净占比档)[0] == "强流出"
    assert 格档(-4.99, _净占比档)[0] == "流出"
    assert 格档(-1.0, _净占比档)[0] == "流出"
    assert 格档(-0.99, _净占比档)[0] == "中性"
    assert 格档(1.0, _净占比档)[0] == "中性"
    assert 格档(1.01, _净占比档)[0] == "流入"
    assert 格档(5.0, _净占比档)[0] == "流入"
    assert 格档(5.01, _净占比档)[0] == "强流入"
    # 主买占比
    assert 格档(0.45, _主买占比档)[0] == "弱"
    assert 格档(0.451, _主买占比档)[0] == "均衡"
    assert 格档(0.56, _主买占比档)[0] == "偏强"
    assert 格档(0.561, _主买占比档)[0] == "强"
    # 户数环比（负=集中）
    assert 格档(-10.0, _户数环比档)[0] == "明显集中"
    assert 格档(-9.99, _户数环比档)[0] == "集中"
    assert 格档(3.0, _户数环比档)[0] == "平稳"
    assert 格档(10.01, _户数环比档)[0] == "明显分散"
    # 换手率
    assert 格档(1.0, _换手率档)[0] == "过低"
    assert 格档(3.0, _换手率档)[0] == "正常"
    assert 格档(7.0, _换手率档)[0] == "活跃"
    assert 格档(7.01, _换手率档)[0] == "过热炒作"


# ── 字段() 契约锁：四段任一空即 raise（口径不空编）──────────────────
def test_字段拒空编():
    with pytest.raises(ValueError):
        字段(名="x", 值="1", 口径="", 意味="y")   # 口径空
    with pytest.raises(ValueError):
        字段(名="x", 值="1", 口径="k", 意味="")   # 意味空
    with pytest.raises(ValueError):
        字段(名="", 值="1", 口径="k", 意味="y")    # 名空
    # 值 None → "NA" 合法（缺数据标记）
    assert 字段(名="x", 值=None, 口径="k", 意味="y")["值"] == "NA"


# ── 缺数据：无 json → missing；子字段缺 → 值NA 不编 ─────────────────
def test_缺json_missing(tmp_path):
    root = str(tmp_path)
    r = get("fund_flow").run(_AS_OF, code="000001", root=root)
    assert r.freshness == "missing"
    assert r.面 == "资金面"
    assert "数据缺失" in r.浓缩块


def test_子字段缺_值NA不编(tmp_path):
    root = str(tmp_path)
    _write_pstock(root, "000002", {"fundflow": {}, "tick": {}, "holder": {}})
    _write_code_industry(root, {})  # 无板块 → 竞品 NA
    r = get("fund_flow").run(_AS_OF, code="000002", root=root)
    names = {it["名"]: it for it in r.字段解读}
    assert names["主力资金"]["值"].startswith("净占比NA")
    assert names["主买盘"]["值"] == "主买占比NA"
    assert names["竞品·板块内相对"]["值"] == "NA"
    # 缺数据也不空编：四段仍齐（__post_init__ 会校验，能构造即证不空）
    for it in r.字段解读:
        assert it["口径"] and it["意味"] and it["名"]


# ── 净占比折算：净占比None + K线amount → 同定义折算，口径明标 ─────────
def test_净占比折算(tmp_path):
    root = str(tmp_path)
    # 净流入 1e6 / 口径日成交额 1e7 = 10% → 强流入(>5)
    _write_pstock(root, "000003", {
        "fundflow": {"今日主力净占比": None, "今日主力净流入": 1_000_000.0,
                     "主力连续净流入天数": 3, "口径日期": _AS_OF, "新鲜度": "新鲜"},
    })
    _write_kline(root, "000003", [("2026-09-17", 10.0, 5e6, 4.0),
                                  ("2026-09-18", 10.0, 1e7, 5.0)])
    _write_code_industry(root, {})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000003", root=root).字段解读}["主力资金"]
    assert "净占比+10.00%" in it["值"]
    assert "折算(净流入/当日成交额)" in it["口径"]
    assert "主力强流入" in it["意味"]
    assert "连续净流入持续" in it["意味"]  # 3天=持续


def test_净占比有值不折算(tmp_path):
    root = str(tmp_path)
    _write_pstock(root, "000004", {
        "fundflow": {"今日主力净占比": 2.0, "今日主力净流入": 5e7,
                     "主力连续净流入天数": 1, "口径日期": _AS_OF, "新鲜度": "新鲜"}})
    _write_code_industry(root, {})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000004", root=root).字段解读}["主力资金"]
    assert "净占比+2.00%" in it["值"] and "折算" not in it["口径"]
    assert "主力流入" in it["意味"]


# ── 竞品·板块内相对：rank/档/vs龙头/vs板均 计算 ──────────────────────
def _roster_电子():
    return {
        "龙头": [{"code": "600001", "换手": 10.0}, {"code": "600002", "换手": 20.0}],
        "中军": [{"code": "600003", "换手": 5.0}, {"code": "600004", "换手": 1.0}],
    }


def test_竞品相对计算_成分内(tmp_path):
    root = str(tmp_path)
    _write_roster(root, "电子", _roster_电子())
    _write_code_industry(root, {"600003": "电子"})
    # 目标 600003 换手5.0，池 [10,20,5,1] → 高于它的有2个 → 第3/4，pr=0.75→尾部
    it = _f_竞品("600003", "电子", None, root, _AS_OF)
    assert "第3/4" in it["值"] and "尾部" in it["值"]
    # vs龙头均值 (10+20)/2=15 → 5/15=0.33x；vs板均 (10+20+5+1)/4=9 → 5/9=0.56x
    assert "0.33x龙头" in it["值"] and "0.56x板均" in it["值"]
    assert "非全行业" in it["口径"] and "roster快照" in it["口径"]


def test_竞品相对_非成分用as_of换手(tmp_path):
    root = str(tmp_path)
    _write_roster(root, "电子", _roster_电子())
    _write_code_industry(root, {"600099": "电子"})
    # 目标不在roster，as_of换手=25 → 插入后池[10,20,5,1,25]，最高 → 第1/5→龙头级
    it = _f_竞品("600099", "电子", 25.0, root, _AS_OF)
    assert "第1/5" in it["值"] and "龙头级" in it["值"]
    assert "as_of当日K线" in it["口径"]


def test_竞品_无roster_NA(tmp_path):
    root = str(tmp_path)
    _write_code_industry(root, {"600026": "交通运输"})  # 无该板块roster文件
    it = _f_竞品("600026", "交通运输", 3.0, root, _AS_OF)
    assert it["值"] == "NA" and "无 sector_roster" in it["口径"]


def test_竞品_未含code_NA(tmp_path):
    root = str(tmp_path)
    it = _f_竞品("999999", None, 3.0, root, _AS_OF)
    assert it["值"] == "NA" and "无法解析申万一级" in it["口径"]


# ── 防未来：换手率取 as_of 当日行，不取 >as_of 未来行 ─────────────────
def test_防未来_换手不取未来行(tmp_path):
    root = str(tmp_path)
    _write_pstock(root, "000005", {"fundflow": {}, "tick": {}, "holder": {}})
    _write_kline(root, "000005", [("2026-09-18", 10.0, 1e7, 5.0),
                                  ("2026-09-19", 10.0, 1e7, 99.0)])  # 未来行须被截
    _write_code_industry(root, {})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000005", root=root).字段解读}["换手率"]
    assert it["值"] == "5.00%" and "活跃" in it["意味"]


# ── 两融恒 NA·待补落盘（文档化缺口）─────────────────────────────────
def test_两融待补落盘(tmp_path):
    root = str(tmp_path)
    _write_pstock(root, "000006", {"fundflow": {}, "tick": {}, "holder": {}})
    _write_code_industry(root, {})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000006", root=root).字段解读}["两融"]
    assert it["值"] == "NA" and "待补落盘" in it["意味"]


# ── 龙虎榜只读状态档 ─────────────────────────────────────────────────
def test_龙虎榜状态档(tmp_path):
    root = str(tmp_path)
    _write_code_industry(root, {})
    # 净买上榜
    _write_pstock(root, "000007", {"fundflow": {}, "tick": {}, "holder": {},
                                   "lhb_veto": {"direction": 1, "n_recent": 2, "net_buy_ratio": 0.3}})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000007", root=root).字段解读}["龙虎榜"]
    assert "净买上榜" in it["值"]
    # 触发 veto → 否决候选
    _write_pstock(root, "000008", {"fundflow": {}, "tick": {}, "holder": {},
                                   "lhb_veto": {"triggered": True, "reason": "大额净卖"}})
    it = {x["名"]: x for x in get("fund_flow").run(_AS_OF, code="000008", root=root).字段解读}["龙虎榜"]
    assert "龙虎榜否决候选" in it["值"]


# ── 契约：面/字段数/G3 行数/浓缩块自动派生 ──────────────────────────
def test_契约_面与G3(tmp_path):
    root = str(tmp_path)
    _write_pstock(root, "000009", {"fundflow": {"今日主力净占比": 1.0, "今日主力净流入": 1e7,
                                                "主力连续净流入天数": 1, "新鲜度": "新鲜"},
                                   "tick": {"主买占比": 0.5, "大单笔数": 10},
                                   "holder": {"户数环比": -5.0, "连续减少期数": 2}})
    _write_kline(root, "000009", [("2026-09-18", 10.0, 1e7, 4.0)])
    _write_code_industry(root, {})
    r = get("fund_flow").run(_AS_OF, code="000009", root=root)
    assert r.面 == "资金面" and r.塔层 == "①塔基"
    assert len(r.字段解读) == 7
    assert 1 <= len(r.浓缩块.splitlines()) <= 8  # G3
    assert r.浓缩块.strip()  # 自动派生非空
