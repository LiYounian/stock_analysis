"""D2-1 骨架打分语义锁 + 冒烟。锁死子分映射与权重（防未来重写无意改口径）。"""
import os
import pytest

from tools.pyramid import d2_compose as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


# ── 权重锁 ──
def test_权重和为1():
    assert abs(sum(C.WEIGHTS.values()) - 1.0) < 1e-9


def test_权重顺序锁():
    # 量价自证 0.30 最高，宏观催化 0.10 最低（v1 宏观是代理）
    assert C.WEIGHTS["量价自证"] == 0.30
    assert C.WEIGHTS["宏观催化"] == 0.10


# ── 子分映射锁 ──
def test_量价自证_T1放量低位得高分():
    s = C.子分_量价自证({"量比档": "放量", "pos60档": "低"}, {"层级": "T1"})
    assert s == 100  # 60 + 25 + 15
    s2 = C.子分_量价自证({"量比档": "缩量", "pos60档": "高"}, {"层级": "未入层"})
    assert s2 == 10  # 10 + 0 + 0


def test_量价自证_爆量不加分反低于放量():
    放 = C.子分_量价自证({"量比档": "放量", "pos60档": "中"}, {"层级": "T2"})
    爆 = C.子分_量价自证({"量比档": "爆量", "pos60档": "中"}, {"层级": "T2"})
    assert 放 > 爆  # 爆量一日游风险，不给高分


def test_板块角色_龙头高于无角色():
    # A9：板块角色只按角色（+拥挤降分/focus兜底）打分，龙头=50（不再叠加 RS）。
    龙头 = C.子分_板块角色({"角色": "龙头", "拥挤档": "B"})
    无 = C.子分_板块角色({"角色": None, "focus_score": 0.5})
    assert 龙头 == 50 and 龙头 > 无


def test_板块角色_拥挤A降分():
    a = C.子分_板块角色({"角色": "中军", "拥挤档": "A"})
    b = C.子分_板块角色({"角色": "中军", "拥挤档": "B"})
    assert b - a == 10


def test_板块角色_RS不再影响打分():
    # A9 语义锁：除 RS 档（强 vs 弱）外全同 → 板块角色子分必须相等。
    # RS 与量价自证的 pos60 重复衡量位置且方向相反，骨架不再用 RS 打分。
    强 = C.子分_板块角色({"角色": "中军", "RS档": "强", "拥挤档": "B"})
    弱 = C.子分_板块角色({"角色": "中军", "RS档": "弱", "拥挤档": "B"})
    无RS = C.子分_板块角色({"角色": "中军", "拥挤档": "B"})
    assert 强 == 弱 == 无RS


def test_位置维度只由pos60衡量_高位不因板块角色高于低位():
    # A9 回归锁：位置维度只由量价自证的 pos60 衡量一次（低位好），
    # 板块角色不再因 RS 偏好"已涨强/高位"。两票板块角色全同、仅位置不同时，
    # 低位（pos60低 + RS弱）的量价自证子分必须 ≥ 高位（pos60高 + RS强）——
    # 骨架不再自相矛盾地奖励高位。
    低位_量价 = C.子分_量价自证({"量比档": "平量", "pos60档": "低"}, {"层级": "T2"})
    高位_量价 = C.子分_量价自证({"量比档": "平量", "pos60档": "高"}, {"层级": "T2"})
    assert 低位_量价 > 高位_量价
    # 且板块角色对高/低位 RS 不再有偏好（同角色 → 相等）
    低位_板块 = C.子分_板块角色({"角色": "中军", "RS档": "弱", "拥挤档": "B"})
    高位_板块 = C.子分_板块角色({"角色": "中军", "RS档": "强", "拥挤档": "B"})
    assert 低位_板块 == 高位_板块


def test_策略共识_来源数线性():
    assert C.子分_策略共识(["a", "b"]) == 50
    assert C.子分_策略共识(["a", "b", "c", "d"]) == 100
    assert C.子分_策略共识([]) == 0


def test_排雷_clean满分_高嫌疑最低():
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, {}) == 100
    assert C.子分_排雷({"嫌疑档": "高"}, {}) == 0


def test_排雷_经验硬命中扣分():
    exp = {"命中规则": [{"状态": "现行", "环节": "排雷/择时"}]}
    assert C.子分_排雷({"嫌疑档": "无嫌疑"}, exp) == 85  # 100 - 15


def test_宏观催化_档位():
    assert C.子分_宏观催化({"净催化档": "强正"}) == 100
    assert C.子分_宏观催化({"净催化档": "中性"}) == 40


# ── 否决锁 ──
def test_veto_涨停与高嫌疑():
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {"涨停": True}) == "涨停不可买"
    assert C.veto_reason({"嫌疑档": "高"}, {}) == "假利好高嫌疑"
    assert C.veto_reason({"嫌疑档": "无嫌疑"}, {"极高位": True}) == "极高位抛压"
    assert C.veto_reason({"嫌疑档": "低"}, {"涨停": False, "极高位": False}) is None


# ── compose_one 加权 ──
def test_compose_one_加权与否决():
    row = C.compose_one(
        "300308", ["多策略并集"],
        pv={"量比档": "平量", "pos60档": "低", "现价": 896.0},
        gate={"层级": "未入层"},
        sec={"角色": None, "RS档": "弱", "净催化档": "强正", "focus_score": 0.88, "板块": "电子"},
        fake={"嫌疑档": "无嫌疑"}, exp={"命中规则": []},
    )
    assert row.否决 is None
    assert 0 <= row.骨架分 <= 100
    assert set(row.子分) == set(C.WEIGHTS)


# ── 骨架冒烟（需数据）──
def test_build_skeleton_限量冒烟():
    if not os.path.isdir(os.path.join(ROOT, "data", "master", "kline")):
        pytest.skip("无 kline 数据")
    out = C.build_skeleton(AS_OF, root=ROOT, scan_kline=False, limit=20)
    assert out["计分票数"] <= 20
    # 排序降序
    scores = [r.骨架分 for r in out["排序"]]
    assert scores == sorted(scores, reverse=True)
