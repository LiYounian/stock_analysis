"""W1 · market_forecast 语义锁测试（守则6）。

锁死"为什么改"：
  · notes 效力 caveat **原样**输出（含"多空收益价差≈0"等原文·锁"不洗白成高概率能赚钱"）；
  · 个股 β 基准默认 proxy + 分歧标记 surface（勿被 hs300 偏多带偏）；
  · factor_contrib 标清 horizon（1日+5日两档都 surface，不漏 1日）；
  · 两融维 kill-switch/权重=0 caveat 在（不参与判别）；
  · 缺文件 → missing 不编造；防未来断言文件内部 as_of ≤ 查询 as_of；
  · 浓缩块 ≤8 行（G3）；面=None（市场级不挂个股四面卡）。
"""
import json
import os

import pytest

from tools.pyramid.registry import get, all_names
import tools.pyramid.tools  # noqa: F401 触发注册

AS_OF = "2026-09-18"

_CAVEAT = ("整体方向命中~55%,多空收益价差≈0(无经济alpha);勿把高概率读成能赚钱。"
           "资金流权重=0(kill-switch);消息面覆盖率≈1%被降权≈0。")


def _mf(as_of=AS_OF, notes=_CAVEAT):
    """构造一份最小 schema market_forecast/v1（两 target × 两 horizon）。"""
    def tgt(name, p1, d1, b1, p5, d5, b5):
        return {"name": name, "as_of": as_of, "horizons": {
            "1": {"p_up": p1, "direction": d1, "prob_bucket": b1,
                  "factor_contrib": {"技术": 0.06, "广度": -0.28, "消息面": -0.14, "资金流": 0.0}},
            "5": {"p_up": p5, "direction": d5, "prob_bucket": b5,
                  "factor_contrib": {"技术": 0.06, "广度": 0.24, "消息面": -0.13, "资金流": 0.0}},
        }}
    return {
        "schema": "market_forecast/v1", "as_of": as_of, "model": "composite",
        "选股用β基准": {"默认": "proxy", "背景": "hs300"},
        "分歧标记": {"触发": True, "口径": "二八分化/风格背离", "维度": {
            "1": {"类型": "中小盘领跑权重滞后"}, "5": {"类型": "权重搭台中小盘偏弱"}}},
        "targets": {
            "proxy": tgt("全A等权代理", 0.55, "偏多", "上行概率偏高", 0.54, "震荡", "方向中性"),
            "hs300": tgt("沪深300", 0.47, "震荡", "方向中性", 0.60, "偏多", "上行概率偏高"),
        },
        "breadth_snapshot": {"net_adv": 0.55, "above_ma20_ratio": 0.39,
                             "limit_up": 83.0, "limit_down": 1.0, "median_pct": 1.07},
        "sentiment_snapshot": {"se_ratio": 0.93, "se_bull": 67.0, "se_bear": 2.0, "se_n": 73.0},
        "fundflow_snapshot": {"margin_date": "2026-09-17", "融资余额": 1334104866205.0},
        "notes": notes,
    }


def _write_mf(root, obj, date=AS_OF):
    p = os.path.join(root, "data", "analysis", date, "market_forecast.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


# ── 注册契约：④宏观 / 面=None ──
def test_已注册_面为None():
    assert "market_forecast" in all_names()
    t = get("market_forecast")
    assert t.塔层 == "④宏观"
    assert t.面 is None                     # 市场级背景不挂个股四面卡


# ── notes 效力 caveat 原样（不洗白）──
def test_notes效力caveat原样(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf())
    r = get("market_forecast").run(AS_OF, None, root=root)
    assert "多空收益价差≈0" in r.浓缩块      # 原文关键片段进浓缩块（锁"不洗白"）
    assert "勿把高概率读成能赚钱" in r.浓缩块
    assert r.fields["notes"] == _CAVEAT      # fields 留原样全量


# ── proxy 默认 β + 分歧标记 surface ──
def test_proxy默认β_分歧surface(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf())
    r = get("market_forecast").run(AS_OF, None, root=root)
    assert "β基准=proxy" in r.浓缩块
    assert "分歧标记: 触发" in r.浓缩块
    assert "中小盘领跑权重滞后" in r.浓缩块   # 维度类型 surface


# ── factor_contrib 两档 horizon 都 surface（不漏 1日）──
def test_factor_contrib两档horizon(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf())
    r = get("market_forecast").run(AS_OF, None, root=root)
    contrib = [ln for ln in r.浓缩块.splitlines() if "维度贡献" in ln][0]
    assert "1日" in contrib and "5日" in contrib     # 两档都在
    assert "技" in contrib and "广" in contrib and "消" in contrib and "资" in contrib


# ── 两融 kill-switch/权重0 caveat ──
def test_kill_switch_权重0(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf())
    r = get("market_forecast").run(AS_OF, None, root=root)
    assert "权重=0" in r.浓缩块 and "kill-switch" in r.浓缩块


# ── 缺文件 → missing 不编 ──
def test_缺文件missing(tmp_path):
    r = get("market_forecast").run(AS_OF, None, root=str(tmp_path))
    assert r.freshness == "missing"
    assert r.fields.get("present") is False
    assert "待补" in r.浓缩块 or "无" in r.浓缩块


# ── 回退最近 ≤as_of 一日 → stale ──
def test_回退stale(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf(as_of="2026-09-15"), date="2026-09-15")  # 只有更早一日
    r = get("market_forecast").run(AS_OF, None, root=root)        # 查 09-18
    assert r.freshness == "stale"
    assert r.fields["file_as_of"] == "2026-09-15"


# ── 防未来：文件内部 as_of 晚于查询 → 断言拦截 ──
def test_防未来_内部as_of不得晚于查询(tmp_path):
    root = str(tmp_path)
    # 文件落在 2026-09-18 目录但内部 as_of 谎报未来 2026-10-01
    _write_mf(root, _mf(as_of="2026-10-01"))
    with pytest.raises(AssertionError):
        get("market_forecast").run(AS_OF, None, root=root)


# ── G3：浓缩块 ≤8 行 ──
def test_浓缩块_le8行(tmp_path):
    root = str(tmp_path)
    _write_mf(root, _mf())
    r = get("market_forecast").run(AS_OF, None, root=root)
    assert len([ln for ln in r.浓缩块.splitlines() if ln.strip()]) <= 8
    assert r.max_浓缩块_行 == 8              # 未偷抬 G3 上限


# ── 真实数据 smoke ──
def test_真实数据_smoke():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    real = os.path.join(root, "data", "analysis", AS_OF, "market_forecast.json")
    if not os.path.exists(real):
        pytest.skip("真实 market_forecast 未落盘")
    r = get("market_forecast").run(AS_OF, None, root=root)
    assert r.freshness == "fresh" and r.fields["present"] is True
    assert r.fields["notes"]                 # 真实 notes 非空且原样留存
