"""策略 S02「放量后缩量回踩」入场 Screener 单测。

锁死规格红线(改坏即挂):
  · C1 周线近期放量:REF(周量,1) 或 REF(周量,2) > MA(周量,8)×1.6(OR 语义、strict >);
  · C2 MA5>MA10(strict);C3 C<O 且 C<REF(C,1)(收阴且收跌,两条都要);
  · C4 V≤MA(V,10)×0.70(含等号);C5 |C−MA10|/MA10≤0.03(含等号);
  · SELECT = C1..C5 全 AND;完整周<8 或 日线<41「不足不选」。
  · **日→周聚合**:ISO 自然周求和、**当周在途不计**(整周剔除,含 Friday 亦剔当周)、
    REF(周量,1/2) 取对上一/上上完整周;防未来函数(只用 t 及之前,截断后结果不变)。
"""
import pandas as pd
import pytest

from tools.collectors import market
from tools.pipeline import screen_s02 as s02
from tools.store import repo as store


# ———————————————————— 构造器 ————————————————————
def _vols(week7=5000.0, week6=1000.0, cur=500.0, base=1000.0):
    """9 个自然周 × 5 交易日的日成交量。wk7=最后一个完整周(REF1)、wk6=REF2、wk8=在途周。"""
    out = []
    for wk in range(9):
        if wk == 8:
            v = cur
        elif wk == 7:
            v = week7
        elif wk == 6:
            v = week6
        else:
            v = base
        out += [float(v)] * 5
    return out


def _closes(slope=0.1, drop=0.2, n=45):
    """温和趋势收盘,末根较昨收回落 drop(制造收跌)。"""
    cl = [round(100.0 + slope * i, 4) for i in range(n)]
    cl[-1] = round(cl[-2] - drop, 4)
    return cl


def _df(vols, closes, opens=None, start="2024-01-01"):
    n = len(vols)
    assert len(closes) == n
    if opens is None:
        opens = list(closes)
        opens[-1] = round(closes[-1] + 0.3, 4)              # 末根收阴(open>close)
    dates = pd.bdate_range(start, periods=n)                 # 2024-01-01 = 周一,连续工作日
    highs = [round(max(o, c) + 0.1, 4) for o, c in zip(opens, closes)]
    lows = [round(min(o, c) - 0.1, 4) for o, c in zip(opens, closes)]
    return pd.DataFrame({"date": dates, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vols})


def _base_df():
    """全条件满足的基准场景(SELECT=True)。"""
    return _df(_vols(), _closes())


# ———————————————————— SELECT 全满足 ————————————————————
def test_select_all_conditions_true():
    r = s02.screen_latest(_base_df())
    assert r["C1_周线放量"] and r["C2_短均多头"] and r["C3_缩量回踩"]
    assert r["C4_接近地量"] and r["C5_贴10日线"]
    assert r["SELECT"] is True


# ———————————————————— C1 周线放量(OR + 边界)————————————————————
def test_c1_true_via_ref1():
    r = s02.screen_latest(_df(_vols(week7=5000.0, week6=1000.0), _closes()))
    assert r["C1_周线放量"] is True                          # REF1 放量路径


def test_c1_true_via_ref2_only():
    """REF1 不放量、REF2 放量 → OR 成立 → C1 真。"""
    r = s02.screen_latest(_df(_vols(week7=1000.0, week6=5000.0), _closes()))
    assert r["明细"]["周量REF1"] < r["明细"]["周量MA8"] * 1.6  # REF1 未达标
    assert r["C1_周线放量"] is True                          # 靠 REF2


def test_c1_false_when_all_flat():
    """各完整周等量 → REF1=REF2=MA8 → 均不 > 1.6×MA8 → C1 假 → 不入选。"""
    r = s02.screen_latest(_df(_vols(week7=1000.0, week6=1000.0), _closes()))
    assert r["C1_周线放量"] is False and r["SELECT"] is False


def test_c1_boundary_strict_gt():
    """边界:REF1 恰等于 1.6×MA8 → 不满足(strict >);略高一档 → 满足。"""
    # week7 日量 1750 → REF1=8750,MA8=(7×5000+8750)/8=5468.75,×1.6=8750 → 恰等,C1 假
    r_eq = s02.screen_latest(_df(_vols(week7=1750.0, week6=1000.0), _closes()))
    assert r_eq["C1_周线放量"] is False
    # week7 日量 1751 → REF1=8755 > 8751 → C1 真
    r_gt = s02.screen_latest(_df(_vols(week7=1751.0, week6=1000.0), _closes()))
    assert r_gt["C1_周线放量"] is True


# ———————————————————— C2 短均线多头 ————————————————————
def test_c2_false_when_flat_trend():
    """收盘全平、仅末根微跌 → MA5 略低于 MA10 → C2 假(strict >)。"""
    closes = [100.0] * 44 + [99.8]
    r = s02.screen_latest(_df(_vols(), closes))
    assert r["C2_短均多头"] is False and r["SELECT"] is False


def test_c2_false_when_downtrend():
    r = s02.screen_latest(_df(_vols(), _closes(slope=-0.1)))
    assert r["C2_短均多头"] is False


# ———————————————————— C3 缩量回踩(收阴且收跌,两条都要)————————————————————
def test_c3_false_when_green():
    """末根收阳(C>O)→ C3 假(即便收跌)。"""
    closes = _closes()
    opens = list(closes)
    opens[-1] = round(closes[-1] - 0.3, 4)                   # open<close → 阳
    r = s02.screen_latest(_df(_vols(), closes, opens))
    assert r["明细"]["close"] < r["明细"]["prev_close"]       # 确实收跌
    assert r["C3_缩量回踩"] is False and r["SELECT"] is False


def test_c3_false_when_not_lower_than_prev():
    """末根收阴但收盘 ≥ 昨收(未回踩)→ C3 假。"""
    closes = _closes()
    closes[-1] = round(closes[-2] + 0.2, 4)                  # 较昨收上涨
    opens = list(closes)
    opens[-1] = round(closes[-1] + 0.3, 4)                   # 仍收阴
    r = s02.screen_latest(_df(_vols(), closes, opens))
    assert r["C3_缩量回踩"] is False


# ———————————————————— C4 接近地量(边界含等号)————————————————————
def test_c4_boundary_leq():
    """V[t] 恰等于 MA(V,10)×0.70 → 满足(≤);略高一档 → 不满足。"""
    # bars35-44 = week7(5000×5) + week8前4(500×4) + V[t];解 Vt≤0.7×(27000+Vt)/10 → Vt≤2032.26
    base_vols = _vols()
    v_pass = list(base_vols); v_pass[-1] = 2032.0
    r_pass = s02.screen_latest(_df(v_pass, _closes()))
    assert r_pass["C4_接近地量"] is True
    v_fail = list(base_vols); v_fail[-1] = 2033.0
    r_fail = s02.screen_latest(_df(v_fail, _closes()))
    assert r_fail["C4_接近地量"] is False and r_fail["SELECT"] is False


# ———————————————————— C5 贴 10 日线 ————————————————————
def test_c5_false_when_far_from_ma10():
    """陡升趋势 + 末根深回踩,远离 MA10(>3%)→ C5 假,但 C1..C4 仍真。"""
    closes = _closes(slope=1.0)
    closes[-1] = 133.0                                        # MA10≈138.4,偏离≈3.9%>3%
    opens = list(closes); opens[-1] = round(closes[-1] + 0.3, 4)
    r = s02.screen_latest(_df(_vols(), closes, opens))
    assert r["C1_周线放量"] and r["C2_短均多头"] and r["C3_缩量回踩"] and r["C4_接近地量"]
    assert r["C5_贴10日线"] is False and r["SELECT"] is False


def test_c5_true_when_hugging_ma10():
    r = s02.screen_latest(_base_df())
    assert r["C5_贴10日线"] is True
    assert r["明细"]["贴线偏离"] <= 0.03


# ———————————————————— 日→周聚合正确性(核心)————————————————————
def _weekly_probe_df():
    """3 个完整周(周量 10/20/30)+ 在途周(周三,3×99)。用于验聚合与在途剔除。"""
    # W0: 5 天各 2 → 10;W1: 5 天各 4 → 20;W2: 5 天各 6 → 30;W3(在途):Mon/Tue/Wed 各 99
    vols = [2.0] * 5 + [4.0] * 5 + [6.0] * 5 + [99.0] * 3
    closes = [100.0 + 0.1 * i for i in range(len(vols))]
    return _df(vols, closes)


def test_weekly_aggregation_excludes_inprogress_and_sums():
    df = _weekly_probe_df()
    t = len(df) - 1                                           # 在途周(W3)周三
    wv = s02.weekly_volumes(df, t)
    assert wv == [10.0, 20.0, 30.0]                           # 只含完整周,在途周(297)被剔除
    assert 99.0 * 3 not in wv


def test_weekly_ref1_ref2_map_to_correct_weeks():
    df = _weekly_probe_df()
    t = len(df) - 1
    wv = s02.weekly_volumes(df, t)
    assert wv[-1] == 30.0                                     # REF(周量,1)=上一完整周(W2)
    assert wv[-2] == 20.0                                     # REF(周量,2)=上上完整周(W1)


def test_weekly_excludes_current_week_even_on_friday():
    """当周即便到周五(整周已收盘),仍按'当周在途不计'整周剔除。"""
    # 3 个完整周 + 第 4 周完整到周五(5 天),t 落在周五
    vols = [2.0] * 5 + [4.0] * 5 + [6.0] * 5 + [9.0] * 5
    closes = [100.0 + 0.1 * i for i in range(len(vols))]
    df = _df(vols, closes)
    t = len(df) - 1                                           # 第 4 周周五
    wv = s02.weekly_volumes(df, t)
    assert wv == [10.0, 20.0, 30.0]                           # 第 4 周(9×5=45)被剔除
    assert 45.0 not in wv


# ———————————————————— 防未来函数(只用 t 及之前)————————————————————
def test_no_lookahead_truncation_invariance():
    """在 t 处判定应与'把 t 之后的数据截断后再判'完全一致。"""
    df = _base_df()
    t = len(df) - 1
    full = s02.signal_at(df, t)
    truncated = s02.signal_at(df.iloc[: t + 1].reset_index(drop=True), t)
    assert full == truncated
    # 追加未来行(极端值)不改变 t 处结论
    future = df.copy()
    extra = df.iloc[[-1]].copy()
    extra["volume"] = 9e9
    extra["close"] = 9e9
    future = pd.concat([df, extra], ignore_index=True)
    assert s02.signal_at(future, t) == full


# ———————————————————— 历史不足:不足不选 ————————————————————
def test_insufficient_history_not_selected():
    vols = _vols()[:30]                                       # 30 根 < 41,亦 <8 完整周
    closes = _closes(n=30)
    r = s02.screen_latest(_df(vols, closes))
    assert r["SELECT"] is False and "历史不足" in r.get("原因", "")


def test_weekly_volumes_short_history_few_weeks():
    """完整周数随历史增长;短历史 → 完整周少。"""
    df = _weekly_probe_df()
    assert len(s02.weekly_volumes(df, len(df) - 1)) == 3


# ———————————————————— pipeline 落 view ————————————————————
def test_run_s02_screen_writes_view(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_ANALYSIS_DIR", tmp_path / "analysis")
    good = _base_df()
    short = _df(_vols()[:30], _closes(n=30))                  # 历史不足 → 跳过
    kl = {"GOOD": good, "SHORT": short}
    monkeypatch.setattr(market, "load_kline", lambda c: kl[c])
    v = s02.run_s02_screen(["GOOD", "SHORT"], as_of="2024-06-01", fetch=False)
    assert v["入选数"] == 1
    assert [x["code"] for x in v["入选清单"]] == ["GOOD"]
    assert v["跳过数(历史不足)"] == 1 and v["有效样本"] == 1
    got = store.get_view("放量后缩量回踩", date="2024-06-01")
    assert got["入选数"] == 1
