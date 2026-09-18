"""shared_pool 合成语义锁：平权并集 / 缺来源标 missing / 来源标签。守则6。

用纯函数 build_result + 各 raw set 直接锁死合成口径，不跑重扫描（K线过闸另有真数据冒烟）。
"""
import json
import os
import pytest

from tools.pyramid.tools.shared_pool_tool import (
    build_result,
    _extract_codes,
    _load_multi_strategy,
    _strategy_view_picks,
    _is_st,
    _SOURCE_ORDER,
    build_raw,
)
from tools.pyramid.registry import get
import tools.pyramid.tools  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 主仓数据根（worktree 里 data/master/kline gitignored；照 unlock/insider 先例用 env 指回主仓）。
DATA_ROOT = os.environ.get("SHARED_POOL_TEST_DATA_ROOT") or ROOT
AS_OF = "2026-09-17"
_NAME, _层, _SRC = "shared_pool", "①塔基", "test"


def _br(raw, code=None):
    return build_result(_NAME, _层, _SRC, AS_OF, code, raw)


# ── 平权并集口径锁（四来源）──
def test_平权并集与共识():
    raw = {
        "多策略并集": {"A", "B"},
        "council": {"B", "C"},
        "板块roster": {"C", "D"},
        "K线过闸": {"A", "F"},
    }
    r = _br(raw)
    # 并集 = A B C D F = 5
    assert r.fields["池规模"] == 5
    # A(多策略/K线=2) B(多策略/council=2) C(council/板块=2) 命中≥2来源 = 3 票
    assert r.fields["共识数"] == 3
    assert r.fields["缺来源"] == []
    assert r.freshness == "fresh"


def test_缺来源标missing不编():
    raw = {k: None for k in _SOURCE_ORDER}
    raw["K线过闸"] = {"X", "Y"}
    r = _br(raw)
    assert set(r.fields["缺来源"]) == {"多策略并集", "council", "板块roster"}
    assert r.fields["池规模"] == 2
    assert "missing" in r.浓缩块


def test_全缺则freshness_missing():
    r = _br({k: None for k in _SOURCE_ORDER})
    assert r.freshness == "missing"
    assert r.fields["池规模"] == 0


def test_来源标签_命中多来源():
    raw = {
        "多策略并集": {"300308"},
        "council": None,
        "板块roster": {"300308", "600995"},
        "K线过闸": {"600995"},
    }
    r = _br(raw, code="300308")
    assert set(r.fields["命中来源"]) == {"多策略并集", "板块roster"}
    assert "300308" in r.浓缩块


def test_来源标签_未命中():
    raw = {"K线过闸": {"600995"}, "多策略并集": None, "council": None,
           "板块roster": None}
    r = _br(raw, code="999999")
    assert r.fields["命中来源"] == []
    assert "命中来源=无" in r.浓缩块


# ── 代码抽取锁 ──
def test_extract_codes():
    obj = {"selections": [{"code": "300308"}, {"code": 600995}],
           "nested": {"picks": [{"代码": "000001"}]}, "list": ["002415"]}
    codes = _extract_codes(obj)
    assert {"300308", "600995", "000001", "002415"} <= codes


# ── agent主线 彻底移除锁：召回层不再消费他人成品选股票（防未来回加）──
def test_agent主线_不再是召回来源():
    """金字塔独立性回归锁：agent主线 从 shared_pool 召回四路里彻底移除。

    历史上 shared_pool 有第五路「agent主线」（读 <date>_agent收盘_*.json 成品选股）。
    用户拍板召回层不消费他人成品——此路已删。锁三点，任一被回加即红：
      ① 固定来源顺序里没有 agent主线（决定分母与展示，回加必在这里）
      ② build_raw 产出的四来源 dict 无 agent主线 键
      ③ _load_agent 加载器已删除（导入即失败 → 用属性存在性断言）
    """
    import tools.pyramid.tools.shared_pool_tool as sp
    assert "agent主线" not in _SOURCE_ORDER
    assert len(_SOURCE_ORDER) == 4
    assert set(_SOURCE_ORDER) == {"多策略并集", "council", "板块roster", "K线过闸"}
    raw = build_raw(AS_OF, root=tmp_no_data(), scan_kline=False)
    assert "agent主线" not in raw
    assert set(raw.keys()) == {"多策略并集", "council", "板块roster", "K线过闸"}
    assert not hasattr(sp, "_load_agent")  # 加载器随该路一并删除


def tmp_no_data():
    """指一个必然无 analysis 数据的目录——build_raw 各加载器返回 None，只验键集不验内容。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "__nonexistent_data_root__")


# ── A5 多策略 ≥2 命中：直接读策略 view json（修退化）──
def _write(adir, fname, payload):
    with open(os.path.join(adir, fname), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def test_strategy_view_picks_三种落法():
    assert _strategy_view_picks({"入选清单": [{"code": "000001"}, {"code": 600000}]}) == {"000001", "600000"}
    assert _strategy_view_picks({"top": [{"code": "300308"}]}) == {"300308"}
    assert _strategy_view_picks({"排行": {"维A": [{"code": "002415"}], "维B": ["600995"]}}) == {"002415", "600995"}
    assert _strategy_view_picks({}) == set()


def test_multi_strategy_ge2命中并集(tmp_path):
    """A5 语义锁：≥2 个策略命中的票才进多策略并集；单策略命中不进。"""
    adir = str(tmp_path)
    _write(adir, "最大范围选股.json", {"入选清单": [{"code": "A"}, {"code": "B"}, {"code": "C"}]})
    _write(adir, "量价放量.json", {"入选清单": [{"code": "B"}, {"code": "C"}]})
    _write(adir, "最强选股.json", {"top": [{"code": "C"}]})
    got = _load_multi_strategy(adir)
    # B(2次) C(3次) 命中≥2；A(1次) 不进
    assert got == {"B", "C"}


def test_multi_strategy_单策略内去重(tmp_path):
    """单个策略里同一票出现多次只计一次——不因单策略重复凑满 ≥2。"""
    adir = str(tmp_path)
    _write(adir, "最大范围选股.json", {"入选清单": [{"code": "A"}, {"code": "A"}]})
    _write(adir, "量价放量.json", {"入选清单": [{"code": "B"}]})
    assert _load_multi_strategy(adir) is None  # A 只被 1 个策略命中


def test_multi_strategy_回退每日选股(tmp_path):
    """策略 view 不足 2 个时回退：每日选股 picks 的 ≥2 strategies。"""
    adir = str(tmp_path)
    _write(adir, "每日选股.json", {"picks": [
        {"code": "X", "strategies": [{"name": "s1"}, {"name": "s2"}]},
        {"code": "Y", "strategies": [{"name": "s1"}]},
    ]})
    assert _load_multi_strategy(adir) == {"X"}


def test_multi_strategy_真数据71(tmp_path):
    """真数据回归：9-17 九个策略 json 的 ≥2 命中应为 71（修前退化只 4）。"""
    adir = os.path.join(DATA_ROOT, "data", "analysis", AS_OF)
    if not os.path.isdir(adir):
        pytest.skip("无 analysis 数据")
    got = _load_multi_strategy(adir)
    if got is None:
        pytest.skip("当日无策略 view json")
    assert len(got) == 71


# ── A2 召回未剔 ST：ST/*ST/退市 不入 K 线召回池 ──
def test_is_st_名称判据():
    assert _is_st("*ST天喻") and _is_st("ST雪莱") and _is_st("退市") and _is_st("XX退")
    assert not _is_st("平安银行") and not _is_st(None) and not _is_st("300308")


def test_ST不入召回池_真数据():
    import glob as _g
    if not os.path.isdir(os.path.join(DATA_ROOT, "data", "master", "kline")):
        pytest.skip("无 K 线数据")
    name_path = os.path.join(DATA_ROOT, "config", "code_name.json")
    if not os.path.exists(name_path):
        pytest.skip("无 code_name.json")
    names = json.load(open(name_path, encoding="utf-8"))
    st_codes = {c for c, n in names.items() if _is_st(n)}
    raw = build_raw(AS_OF, root=DATA_ROOT)
    kpool = raw["K线过闸"] or set()
    assert kpool & st_codes == set(), sorted(kpool & st_codes)


# ── 真数据冒烟（跑真实全A扫描，~15s；无数据则跳过）──
def test_shared_pool_真数据():
    if not os.path.isdir(os.path.join(DATA_ROOT, "data", "master", "kline")):
        pytest.skip("无 K 线数据")
    res = get("shared_pool").run(AS_OF, "300308", root=DATA_ROOT)
    assert res.防未来 is True
    assert res.fields["池规模"] >= 0
    assert isinstance(res.fields["各来源命中数"], dict)
