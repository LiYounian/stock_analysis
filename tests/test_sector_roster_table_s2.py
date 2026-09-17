"""S2 角色关系表维护 测试——锁"为什么改"的语义:

1. 主力(LHB proxy):净买累计>0 的票才入,按净买降序;净卖出/中性排除;诚实覆盖度计数对。
2. honest degrade:无 LHB 快照 → 主力空 + 覆盖度如实(不编造大资金)。
3. 关系表五角色:龙头/中军/补涨先锋/弹性 + 主力 齐备。
4. 周度 diff 留痕:新进/退出/角色变动 能检出;同周重跑不误报(基线仅取不同 ISO 周)。
5. 变更 md 渲染:有变更板块进正文,无变更不虚报。
6. 防未来函数:主力只吃 lhb_asof(上榜日<date)结果。
"""
import json

import pytest

from tools.analysis.sector_forecast import sector_roster_table as ST


class _FakeLHB:
    """按 code 返回龙虎榜事件;模拟部分覆盖(有的票无快照)。"""
    FileNotFoundError = FileNotFoundError

    def __init__(self, data):
        self._data = data

    def lhb_asof(self, code, date):
        if code not in self._data:
            raise FileNotFoundError(code)
        return self._data[code]


@pytest.fixture
def _patch_lhb(monkeypatch):
    def _apply(data):
        import tools.collectors.lhb as real
        fake = _FakeLHB(data)
        monkeypatch.setattr(real, "lhb_asof", fake.lhb_asof)
    return _apply


def test_mainforce_net_buy_leaders_only(_patch_lhb):
    _patch_lhb({
        "A": [{"list_date": "2026-09-10", "net_buy": 3e8}],   # 净买+3亿 → 入
        "B": [{"list_date": "2026-09-11", "net_buy": 1e8},
              {"list_date": "2026-09-12", "net_buy": 2e8}],   # 累计+3亿 → 入
        "C": [{"list_date": "2026-09-10", "net_buy": -5e8}],  # 净卖出 → 排除(但计入有事件)
        # D 无快照 → FileNotFound
    })
    top, cov = ST._mainforce("电子", ["A", "B", "C", "D"], "2026-09-16")
    codes = [x["code"] for x in top]
    assert "C" not in codes and "D" not in codes      # 净卖出/无数据不入主力
    assert set(codes) == {"A", "B"}
    assert cov["板块成分数"] == 4
    assert cov["有LHB快照数"] == 3                     # A/B/C 有快照,D 无
    assert cov["净买为正数"] == 2
    assert cov["主力入选数"] == 2


def test_mainforce_sorted_and_labeled(_patch_lhb):
    _patch_lhb({
        "A": [{"list_date": "2026-09-10", "net_buy": 1e8}],
        "B": [{"list_date": "2026-09-11", "net_buy": 5e8}],
        "C": [{"list_date": "2026-09-11", "net_buy": 3e8}],
    })
    top, _ = ST._mainforce("电子", ["A", "B", "C"], "2026-09-16")
    assert [x["code"] for x in top] == ["B", "C", "A"]   # 净买降序
    assert top[0]["选级"] == "主选" and top[2]["选级"] == "备选"
    assert "龙虎榜净买" in top[0]["理由"]


def test_mainforce_window_excludes_old(_patch_lhb):
    # 窗口 30 日:2026-09-16 之前 30 日 = 2026-08-17;更早的上榜不计入
    _patch_lhb({"A": [{"list_date": "2026-07-01", "net_buy": 9e8}]})
    top, cov = ST._mainforce("电子", ["A"], "2026-09-16")
    assert top == []
    assert cov["窗口内上榜数"] == 0                     # 有快照但窗口内无上榜


def test_mainforce_no_coverage_degrade(_patch_lhb):
    _patch_lhb({})                                     # 全无快照
    top, cov = ST._mainforce("冷门板块", ["X", "Y"], "2026-09-16")
    assert top == []
    assert cov["有LHB快照数"] == 0 and cov["覆盖率"] == 0.0
    assert "无此数据源" in cov["诚实说明"]


def test_diff_table_detects_moves():
    prev = {"ISO周": "2026-W37", "roles": {
        "龙头": [{"code": "A"}], "中军": [{"code": "B"}],
        "补涨先锋": [{"code": "C"}], "弹性股": [], "主力": [{"code": "B"}]}}
    cur = {"ISO周": "2026-W38", "roles": {
        "龙头": [{"code": "A"}, {"code": "C"}],          # C 补涨→龙头(角色变动)
        "中军": [{"code": "B"}], "补涨先锋": [{"code": "D"}],  # D 新进
        "弹性股": [], "主力": []}}                        # B 退出主力(仍在中军→非退出)
    d = ST._diff_table(cur, prev)
    assert d["基线周"] == "2026-W37"
    assert any(x["code"] == "D" for x in d["新进"])
    assert d["退出"] == []                               # A/B/C 仍在,无整体退出
    assert any(x["code"] == "C" for x in d["角色变动"])


def test_diff_table_first_time():
    d = ST._diff_table({"roles": {}}, None)
    assert d["基线周"] is None


def test_changes_md_renders(tmp_path):
    changes = [
        {"板块": "电子", "变更": {"基线周": "2026-W37",
            "新进": [{"code": "D", "角色": ["补涨先锋"]}], "退出": [],
            "角色变动": [{"code": "C", "上周": ["补涨先锋"], "本周": ["龙头"]}],
            "变更计数": {"新进": 1, "退出": 0, "角色变动": 1}}},
        {"板块": "银行", "变更": {"基线周": "2026-W37", "新进": [], "退出": [],
            "角色变动": [], "变更计数": {"新进": 0, "退出": 0, "角色变动": 0}}},
    ]
    p = ST.write_changes_md("2026-09-16", changes, out_root=str(tmp_path))
    txt = p.read_text(encoding="utf-8")
    assert "电子" in txt and "新进" in txt and "补涨先锋→龙头" in txt
    assert "银行" not in txt                             # 无变更板块不进正文


def test_write_table_same_week_no_false_diff(tmp_path):
    """同 ISO 周重跑:基线不取自身,变更应为首次/无基线,不误报。"""
    base = tmp_path
    payload = {"板块": "电子", "roles": {k: [] for k in ST.ROLE_KEYS}}
    p1 = ST.write_roster_table(dict(payload), "2026-09-16", out_root=str(base))
    t1 = json.loads(p1.read_text(encoding="utf-8"))
    assert t1["变更"]["基线周"] is None                  # 首次
    # 同周再跑一次:上一版 ISO 周相同 → 仍不取作基线
    p2 = ST.write_roster_table(dict(payload), "2026-09-16", out_root=str(base))
    t2 = json.loads(p2.read_text(encoding="utf-8"))
    assert t2["变更"]["基线周"] is None
