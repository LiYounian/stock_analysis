"""板块采集单测(mock 数据源,不触网)。

锁语义:申万一级清单解析(去.SI)、板块指数落盘/读回、个股→板块映射「首个命中」+
缺省成分源置空不伪装、board_of 兜底。
"""
import pandas as pd
import pytest

from tools.collectors import board
from tools.store import repo as store


def _sw_hist_df():
    """申万 index_hist_sw 风格(中文列 开盘/收盘/…,命中 market._normalize 映射)。"""
    return pd.DataFrame({
        "代码": ["801010", "801010"], "日期": ["2024-01-01", "2024-01-02"],
        "收盘": [100.5, 102.0], "开盘": [100.0, 101.0],
        "最高": [101.0, 102.5], "最低": [99.5, 100.8],
        "成交量": [1e6, 1.2e6], "成交额": [1e8, 1.2e8],
    })


def test_fetch_board_list_strips_suffix(monkeypatch):
    import akshare as ak
    monkeypatch.setattr(ak, "sw_index_first_info",
                        lambda: pd.DataFrame({"行业代码": ["801010.SI"], "行业名称": ["农林牧渔"]}))
    lst = board.fetch_board_list()
    assert lst == [{"name": "农林牧渔", "code": "801010"}]      # .SI 已去掉


def test_fetch_board_kline_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(board, "fetch_board_list",
                        lambda: [{"name": "农林牧渔", "code": "801010"}])
    monkeypatch.setattr(board, "_fetch_board_hist", lambda *a, **k: _sw_hist_df())
    out = board.fetch_board_kline(["农林牧渔"], start="20240101", end="20240102")
    assert "农林牧渔" in out
    df = board.load_board_kline("农林牧渔")
    assert len(df) == 2 and df["close"].iloc[-1] == 102.0


def test_membership_first_hit_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    cons = {"半导体": ["000001", "000002"], "电池": ["000002", "000003"]}
    boards = [{"name": "半导体", "code": "731"}, {"name": "电池", "code": "732"}]
    m = board.fetch_membership(cons_fetcher=lambda name: cons[name], boards=boards)
    assert m["000002"] == "半导体"          # 首个命中的板块保留(半导体在前)
    assert m["000001"] == "半导体" and m["000003"] == "电池"
    assert board.board_of("000003") == "电池"        # 读回映射
    assert board.load_membership()["000001"] == "半导体"


def test_membership_default_source_empty(monkeypatch, tmp_path):
    """缺省无成分源:落空映射、不伪装、不空跑网络。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    m = board.fetch_membership()
    assert m == {}
    assert store.get_raw_meta("board_membership", "all")["source"] == "n/a"


def test_board_of_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    monkeypatch.setattr(board, "_CODE_INDUSTRY_CACHE", {})   # 回退源也空:两源皆空→None
    assert board.board_of("999999") is None       # 映射未落盘→兜底 None,不抛


def test_board_of_falls_back_to_code_industry(monkeypatch, tmp_path):
    """#24 回归:membership 长期为空时,board_of 回退 code_industry(申万一级)而非全 None。

    锁语义——板块轮动专家「无所属行业」结构性弃火的根因是 board_of 全 None;修复后只要
    code_industry.json 有该票,board_of 必给出申万一级行业名(与 board_kline 键、RRG 口径对齐)。
    """
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)     # membership 未落盘 → FileNotFoundError
    monkeypatch.setattr(board, "_CODE_INDUSTRY_CACHE",
                        {"300750": "电力设备", "000001": "银行"})
    assert board.board_of("300750") == "电力设备"        # 回退命中 code_industry
    assert board.board_of(300750) == "电力设备"           # 入参 int 也归一(str 化)
    assert board.board_of("999999") is None              # 回退源也未收录 → None


def test_board_of_membership_takes_precedence(monkeypatch, tmp_path):
    """membership 若已采集则**优先**(细分口径),回退源仅在 membership 缺该票时生效。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    board.fetch_membership(cons_fetcher=lambda name: {"半导体": ["300750"]}[name],
                           boards=[{"name": "半导体", "code": "731"}])
    monkeypatch.setattr(board, "_CODE_INDUSTRY_CACHE", {"300750": "电力设备"})
    assert board.board_of("300750") == "半导体"           # membership 命中 → 不走回退


def test_rrg_expert_not_abstain_no_industry_after_fix(monkeypatch):
    """#24 回归(专家层):board_of 能给出行业后,板块轮动专家不再以「无所属行业」弃权。

    stub rrg.industry_row 返回有效行,证明行业经 board_of 流入专家并产出方向票(能发声)。
    """
    from tools.analysis import experts, rrg
    monkeypatch.setattr(board, "load_membership",
                        lambda: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(board, "_CODE_INDUSTRY_CACHE", {"300750": "电力设备"})
    monkeypatch.setattr(rrg, "industry_row", lambda name: {
        "方向": "看空", "强度": 0.8, "数据充分度": "充分", "象限": "落后象限",
        "依据": ["落后象限"], "RS_Ratio": 96.8, "RS_Momentum": 97.3})
    v = experts.expert_板块轮动({"meta": {"code": "300750"}})
    assert v.数据充分度 != "缺失" and v.置信度 > 0        # 已发声,非弃权
    assert v.方向 == "看空" and v.原始["行业"] == "电力设备"


class _FakeRS:
    """伪 baostock 结果集:逐行 next()/get_row_data()。"""
    def __init__(self, rows):
        self.error_code = "0"
        self._rows, self._i = rows, -1

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


def test_fetch_membership_baostock(monkeypatch, tmp_path):
    """baostock 证监会行业:去 sh./sz. 前缀、跳过空行业、落盘 source=baostock。"""
    monkeypatch.setattr(store, "_RAW_DIR", tmp_path)
    import baostock as bs
    rows = [
        ["2026-08-03", "sh.600000", "浦发银行", "J66货币金融服务", "证监会行业分类"],
        ["2026-08-03", "sz.000002", "万科A", "K70房地产业", "证监会行业分类"],
        ["2026-08-03", "sh.600001", "邯郸钢铁", "", "证监会行业分类"],   # 空行业→跳过
    ]
    monkeypatch.setattr(bs, "login", lambda *a, **k: None)
    monkeypatch.setattr(bs, "logout", lambda *a, **k: None)
    monkeypatch.setattr(bs, "query_stock_industry", lambda *a, **k: _FakeRS(rows))
    m = board.fetch_membership_baostock()
    assert m == {"600000": "J66货币金融服务", "000002": "K70房地产业"}   # 前缀去掉、空跳过
    assert board.load_membership()["600000"] == "J66货币金融服务"
    assert store.get_raw_meta("board_membership", "all")["source"] == "baostock"
