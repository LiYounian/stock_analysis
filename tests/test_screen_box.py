"""策略 3「箱体形态」全A screener 单测(hermetic,不触网)。

⚠️ 本文件锁 **v1(detect_box)** 路径(重构后默认改 v2,v1 仍保留供 A/B,显式 use_v2=False 触达)。
   v2(提供者规格:振幅带/触碰/缩量/横盘/趋势门/结构化输出)覆盖见 tests/test_box_rewrite.py。

锁死红线(v1):
  · 复用 detect_box(不重写几何):命中票进入选清单、不达标不入选;
  · view schema 完整(as_of/策略/扫描数/有效样本/跳过数/入选数/入选清单);
  · 空池 / 历史不足不崩;
  · 防未来函数:signal_at 按 t 截 [0,t] 识别,尾部追加未来根不改变截至当日结论。
"""
import pandas as pd

from tools.pipeline import screen_box as box
from tools.store import repo as store


# ———————————————————— 构造器 ————————————————————
def _box_df(win: int = 30, breakout: bool = True):
    """win 根窄幅箱体 + 末根(放量)突破。breakout=False → 末根不突破(不达标)。"""
    n = win + 1
    dates = pd.bdate_range("2024-01-01", periods=n)
    opens = [100.0] * win
    closes = [100.0] * win
    highs = [100.5] * win
    lows = [99.5] * win
    vols = [1000.0] * win
    # 末根
    last_close = 105.0 if breakout else 100.2   # 105 > 100.5×1.03=103.5 → 突破;100.2 → 不突破
    opens.append(100.0)
    closes.append(last_close)
    highs.append(last_close + 0.1)
    lows.append(99.9)
    vols.append(2000.0)                          # > 前30根均量1000×1.5
    return pd.DataFrame({"date": dates, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vols})


# ———————————————————— 复用 detect_box:命中/不命中 ————————————————————
def test_real_detect_box_hit():
    r = box.screen_latest(_box_df(breakout=True), use_v2=False)
    assert r["SELECT"] is True
    assert r["特征"]["突破"] is True and r["特征"]["放量"] is True and r["特征"]["窄幅"] is True


def test_real_detect_box_miss_when_no_breakout():
    r = box.screen_latest(_box_df(breakout=False))
    assert r["SELECT"] is False


# ———————————————————— mock detect_box:验筛选逻辑与 filter ————————————————————
def test_run_box_screen_with_mocked_detect(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    good = _box_df(breakout=True)                # 末根收 105
    bad = _box_df(breakout=False)                # 末根收 100.2
    kl = {"HIT": good, "MISS": bad}
    monkeypatch.setattr(box.market, "load_kline", lambda c: kl[c])

    # mock detect_box:收盘 > 104 视为达标(HIT 达标 / MISS 不达标)
    def dispatch(df, cfg=None):
        last = float(df["close"].iloc[-1])
        return {"达标": last > 104.0, "特征": {"箱高%": 1.0, "收盘": last}}

    monkeypatch.setattr(box, "detect_box", dispatch)
    v = box.run_box_screen(["HIT", "MISS"], as_of="2024-06-01", fetch=False, use_v2=False)
    assert v["入选数"] == 1
    assert [x["code"] for x in v["入选清单"]] == ["HIT"]
    assert v["有效样本"] == 2 and v["跳过数(历史不足)"] == 0


# ———————————————————— view schema 完整 + 落盘可读 ————————————————————
def test_run_box_screen_writes_view(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    good = _box_df(breakout=True)
    short = _box_df(win=10)                       # 11 根 < 窗口+1=31 → 历史不足跳过
    kl = {"GOOD": good, "SHORT": short}
    monkeypatch.setattr(box.market, "load_kline", lambda c: kl[c])
    v = box.run_box_screen(["GOOD", "SHORT"], as_of="2024-06-01", fetch=False, use_v2=False)
    for key in ("as_of", "策略", "扫描数", "有效样本", "跳过数(历史不足)",
                "入选数", "入选清单", "规则", "防未来函数"):
        assert key in v
    assert v["as_of"] == "2024-06-01"
    assert v["入选数"] == 1 and [x["code"] for x in v["入选清单"]] == ["GOOD"]
    assert v["跳过数(历史不足)"] == 1 and v["有效样本"] == 1
    got = store.get_view("箱体形态", date="2024-06-01")
    assert got["入选数"] == 1 and got["策略"].startswith("箱体形态")


# ———————————————————— 空池 / 历史不足不崩 ————————————————————
def test_empty_pool_no_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    v = box.run_box_screen([], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 0 and v["扫描数"] == 0 and v["有效样本"] == 0


def test_insufficient_history_skipped():
    r = box.screen_latest(_box_df(win=10))        # 11 根 < 31
    assert r["SELECT"] is False


def test_empty_kline_no_crash():
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    r = box.screen_latest(empty)
    assert r["SELECT"] is False


# ———————————————————— 防未来函数:尾部追加未来根不改变截至当日结论 ————————————————————
def test_no_lookahead_truncation_invariance():
    df = _box_df(breakout=True)
    t = len(df) - 1
    base = box.signal_at(df, t)
    # 追加极端未来行
    extra = df.iloc[[-1]].copy()
    extra["close"] = 9e9
    extra["volume"] = 9e9
    extra["high"] = 9e9
    future = pd.concat([df, extra], ignore_index=True)
    assert box.signal_at(future, t) == base
    # 与「先截断到 [0,t] 再判」一致
    truncated = box.signal_at(df.iloc[: t + 1].reset_index(drop=True), t)
    assert truncated == base
