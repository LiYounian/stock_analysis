"""窗3 · fake_good_news 语义锁测试（守则6）。

锁死：写死阈值不被无意改动 / 档位枚举 / 三判据命中逻辑（合成 K 线确定性触发）/
无 events 标 missing 不编 / 无近期利好=无嫌疑 / 真实 601872 涨停=低嫌疑。
"""
import json
import os

import pandas as pd
import pytest

from tools.pyramid.registry import get, all_names
import tools.pyramid.tools  # noqa: F401 触发注册
from tools.pyramid.tools import fake_good_news_tool as fg

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AS_OF = "2026-09-17"


def test_已注册():
    assert "fake_good_news" in all_names()
    assert get("fake_good_news").塔层 == "②消息"


# ── 写死阈值语义锁（改这些即改变"为什么判假利好"）──
def test_写死阈值锁定():
    assert fg.RECENT_CAL_DAYS == 7
    assert fg.GAP_UP == 3.0
    assert fg.FADE == 2.0
    assert fg.UPPER_SHADOW == 0.5
    assert fg.VOL_SURGE == 1.5
    assert fg.DROP_NEXT == -2.0
    assert fg.嫌疑档枚举 == ("无嫌疑", "低", "中", "高")


# ── 判据单元：合成 K 线确定性触发 ──
def _mk_df(rows):
    return pd.DataFrame(rows)


def test_gap_fade_命中():
    # 前收 10.0 → 高开 10.6(+6%) 收 10.1(+1%)：divergence 5pp ≥ FADE
    df = _mk_df([
        {"date": "2026-09-15", "open": 10, "high": 10, "low": 10, "close": 10.0, "pct_chg": 0},
        {"date": "2026-09-16", "open": 10.6, "high": 10.9, "low": 10.0, "close": 10.1, "pct_chg": 1.0},
    ])
    hit, desc = fg.FakeGoodNewsTool._gap_fade(df, 1)
    assert hit is True, desc


def test_gap_fade_健康不命中():
    # 低开高走涨停型（601872 那类）：不算高开走弱
    df = _mk_df([
        {"date": "2026-09-15", "open": 10, "high": 10, "low": 10, "close": 10.0, "pct_chg": 0},
        {"date": "2026-09-16", "open": 10.1, "high": 11.0, "low": 10.0, "close": 11.0, "pct_chg": 10.0},
    ])
    hit, _ = fg.FakeGoodNewsTool._gap_fade(df, 1)
    assert hit is False


def test_long_upper_shadow_命中():
    # 放量长上影：上影占比大 + 量比≥1.5
    rows = [{"date": f"2026-09-0{d}", "open": 10, "high": 10.2, "low": 9.9, "close": 10,
             "pct_chg": 0, "volume": 100, "amount": 1000} for d in range(1, 7)]
    rows.append({"date": "2026-09-08", "open": 10, "high": 11.0, "low": 9.9, "close": 10.1,
                 "pct_chg": 1.0, "volume": 300, "amount": 3000})  # 上影(11-10.1)/(11-9.9)=0.82, 量比3x
    df = _mk_df(rows)
    hit, desc = fg.FakeGoodNewsTool._long_upper_shadow(df, len(df) - 1)
    assert hit is True, desc


def test_一字板振幅0不误判上影():
    df = _mk_df([{"date": "2026-09-08", "open": 11, "high": 11, "low": 11, "close": 11,
                  "pct_chg": 10, "volume": 100, "amount": 1100}])
    hit, desc = fg.FakeGoodNewsTool._long_upper_shadow(df, 0)
    assert hit is False and "一字" in desc


# ── run() 全路径：合成 data-root ──
def _write_fixture(tmp_path, code, kline_rows, events):
    kdir = tmp_path / "data" / "master" / "kline"
    kdir.mkdir(parents=True)
    pd.DataFrame(kline_rows).to_parquet(kdir / f"{code}.parquet")
    adir = tmp_path / "data" / "analysis" / AS_OF
    adir.mkdir(parents=True)
    with open(adir / f"{code}.json", "w", encoding="utf-8") as f:
        json.dump({"events": events}, f, ensure_ascii=False)


def _base_kline():
    # 6 根平静 bar 供量比基线
    rows = [{"date": f"2026-09-{d:02d}", "open": 10, "high": 10.2, "low": 9.8, "close": 10.0,
             "pct_chg": 0.0, "volume": 100, "amount": 1000} for d in range(9, 15)]
    return rows


def test_run_高嫌疑_两判据命中(tmp_path):
    rows = _base_kline()
    # D0=09-16 兑现日：高开走弱 + 放量长上影
    rows.append({"date": "2026-09-16", "open": 10.6, "high": 11.2, "low": 10.0, "close": 10.1,
                 "pct_chg": 1.0, "volume": 400, "amount": 4200})
    # D0+1=09-17：利好后回吐 -4%
    rows.append({"date": "2026-09-17", "open": 10.0, "high": 10.1, "low": 9.6, "close": 9.7,
                 "pct_chg": -3.96, "volume": 200, "amount": 1940})
    events = [{"date": "2026-09-16", "type": "重组", "impact": "利好", "title": "重大合作落地"}]
    _write_fixture(tmp_path, "300001", rows, events)
    res = get("fake_good_news").run(AS_OF, "300001", root=str(tmp_path))
    assert res.fields["嫌疑档"] == "高"
    assert res.fields["命中数"] == 2
    assert res.fields["高开走弱"] is True
    assert res.fields["利好后回吐"] is True
    assert res.防未来 is True
    assert len([l for l in res.浓缩块.splitlines() if l.strip()]) <= 8


def test_run_无近期利好_无嫌疑(tmp_path):
    rows = _base_kline()
    rows.append({"date": "2026-09-16", "open": 10, "high": 10.2, "low": 9.9, "close": 10.1,
                 "pct_chg": 1.0, "volume": 100, "amount": 1010})
    rows.append({"date": "2026-09-17", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2,
                 "pct_chg": 0.99, "volume": 100, "amount": 1020})
    # 利好在窗口外（>7 天）
    events = [{"date": "2026-09-01", "type": "回购", "impact": "利好", "title": "回购"}]
    _write_fixture(tmp_path, "300002", rows, events)
    res = get("fake_good_news").run(AS_OF, "300002", root=str(tmp_path))
    assert res.fields["嫌疑档"] == "无嫌疑"
    assert res.fields["命中数"] == 0


def test_run_无events标missing不编(tmp_path):
    rows = _base_kline()
    rows.append({"date": "2026-09-16", "open": 10, "high": 10.2, "low": 9.9, "close": 10.1,
                 "pct_chg": 1.0, "volume": 100, "amount": 1010})
    rows.append({"date": "2026-09-17", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2,
                 "pct_chg": 0.99, "volume": 100, "amount": 1020})
    # 只写 K 线、不写 events 文件
    kdir = tmp_path / "data" / "master" / "kline"
    kdir.mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(kdir / "300003.parquet")
    res = get("fake_good_news").run(AS_OF, "300003", root=str(tmp_path))
    assert res.freshness == "missing"
    assert res.fields["events"] == "missing"
    assert res.fields["嫌疑档"] in fg.嫌疑档枚举
    assert "不编造利好" in res.浓缩块


# ── 真实数据集成（无数据则跳过）──
def test_601872_涨停低嫌疑():
    df = fg.load_kline("601872", AS_OF, root=ROOT, min_bars=6)
    if df is None:
        pytest.skip("无 601872 主档 K 线（worktree 需 --data-root 指主仓）")
    res = get("fake_good_news").run(AS_OF, "601872", root=ROOT)
    # 09-17 低开高走涨停：无高开走弱 → 嫌疑档为低/无
    assert res.fields["嫌疑档"] in ("低", "无嫌疑")
    assert res.fields.get("高开走弱") in (False, None)
    assert res.防未来 is True
