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


# ════════════════════════════════════════════════════════════════════
# Minervini 趋势模板过滤器(_trend_template)+ signal_at(trend_filter=)
# 锁死红线:8 条全过才 PASS;各条命中/不命中;RS 百分位门;无未来函数;
#          S02 原路径零回归(trend_filter=False 逐字节等价旧版)。
# ════════════════════════════════════════════════════════════════════
def _uptrend_df(n=300, base=10.0, slope=0.1, start="2023-01-02"):
    """稳健线性上升趋势(≥252 根):close>MA50>MA150>MA200、MA200 上升、贴近 52 周高、远离 52 周低。"""
    closes = [round(base + slope * i, 4) for i in range(n)]
    opens = [round(c - 0.02, 4) for c in closes]
    highs = [round(c + 0.05, 4) for c in closes]
    lows = [round(c - 0.05, 4) for c in closes]
    dates = pd.bdate_range(start, periods=n)
    vols = [1000.0] * n
    return pd.DataFrame({"date": dates, "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vols})


def test_trend_all_conditions_pass():
    df = _uptrend_df()
    t = len(df) - 1
    g = s02._trend_template(df, t, rs_rank=85.0)
    assert g["C1_价上150_200"] and g["C2_150上200"] and g["C3_200上升"]
    assert g["C4_50上150_200"] and g["C5_价上50"]
    assert g["C6_距52低≥30%"] and g["C7_距52高≤25%"] and g["C8_RS达标"]
    assert g["PASS"] is True


def test_trend_rs_below_threshold_blocks():
    """RS 百分位 < 门槛(70)→ C8 假 → 趋势门不过(其余 7 条仍真)。"""
    df = _uptrend_df()
    t = len(df) - 1
    g = s02._trend_template(df, t, rs_rank=60.0)
    assert g["C8_RS达标"] is False and g["PASS"] is False
    assert g["C1_价上150_200"] and g["C7_距52高≤25%"]        # 其余仍真 → 只 C8 卡


def test_trend_rs_none_blocks():
    """RS 未提供(横截面缺喂)→ C8 无法判 → 不通过(诚实不放行)。"""
    g = s02._trend_template(_uptrend_df(), len(_uptrend_df()) - 1, rs_rank=None)
    assert g["C8_RS达标"] is False and g["PASS"] is False


def test_trend_rs_boundary_at_threshold():
    """RS 恰等于门槛 70 → 达标(含等号 ≥)。"""
    df = _uptrend_df()
    t = len(df) - 1
    assert s02._trend_template(df, t, rs_rank=70.0)["C8_RS达标"] is True
    assert s02._trend_template(df, t, rs_rank=69.99)["C8_RS达标"] is False


def test_trend_downtrend_fails_ma_conditions():
    """下降趋势:价在均线下、短均线在长均线下、MA200 不升 → 多条假 → 不过。"""
    df = _uptrend_df(slope=-0.05, base=100.0)                # 下行
    t = len(df) - 1
    g = s02._trend_template(df, t, rs_rank=95.0)
    assert g["C1_价上150_200"] is False
    assert g["C2_150上200"] is False and g["C4_50上150_200"] is False
    assert g["C3_200上升"] is False
    assert g["PASS"] is False


def test_trend_near_52w_low_ratio_fails_c6():
    """涨幅太小(现价 < 52周最低×1.30)→ C6 假(即便仍是上升趋势、RS 达标)。"""
    df = _uptrend_df(base=100.0, slope=0.02)                 # 温和上行:252 窗内涨幅 <30%
    t = len(df) - 1
    g = s02._trend_template(df, t, rs_rank=90.0)
    assert g["C6_距52低≥30%"] is False and g["PASS"] is False


def test_trend_far_below_52w_high_fails_c7():
    """窗内某历史高点被顶高(现价 < 52周最高×0.75)→ C7 假,隔离验证(只动 high 列)。"""
    df = _uptrend_df()
    t = len(df) - 1
    df.loc[t - 10, "high"] = df.loc[t, "close"] * 2.0        # 制造远高于现价的 52 周高
    g = s02._trend_template(df, t, rs_rank=90.0)
    assert g["C7_距52高≤25%"] is False and g["PASS"] is False
    assert g["C1_价上150_200"] and g["C6_距52低≥30%"]        # 其余不受影响


def test_trend_flat_series_c3_not_rising():
    """完全走平:MA200[t]==MA200[t−21] → C3(strict >)假;MA 相等 → C2/C4 亦假。"""
    df = _uptrend_df(slope=0.0, base=50.0)
    t = len(df) - 1
    g = s02._trend_template(df, t, rs_rank=90.0)
    assert g["C3_200上升"] is False
    assert g["PASS"] is False


def test_trend_insufficient_history_skips():
    """历史 < 252(次新)→ 趋势门不通过 + 标 跳过(记为 not passed)。"""
    df = _uptrend_df(n=200)
    g = s02._trend_template(df, len(df) - 1, rs_rank=90.0)
    assert g["PASS"] is False and g.get("跳过") is True


def test_trend_no_lookahead_invariance():
    """趋势门在 t 处的判定 = 截断 t 之后再判(只用 ≤ t 数据)。"""
    df = _uptrend_df()
    t = 270
    full = s02._trend_template(df, t, rs_rank=88.0)
    trunc = s02._trend_template(df.iloc[: t + 1].reset_index(drop=True), t, rs_rank=88.0)
    assert full == trunc


# ———————————————————— signal_at(trend_filter=) 向后兼容 + 叠加 ————————————————————
def test_signal_at_trend_filter_off_is_byte_equal_legacy():
    """trend_filter=False(默认)必须与旧版逐字节等价:不加任何键、SELECT 不变。"""
    df = _base_df()
    t = len(df) - 1
    legacy = s02.signal_at(df, t)
    off = s02.signal_at(df, t, trend_filter=False)
    assert off == legacy
    assert "趋势门" not in off


def test_signal_at_trend_filter_on_can_block_select():
    """开趋势门:base 命中的 S02 信号,若趋势门不过(短历史无法判)→ SELECT 翻 False + 带趋势门键。"""
    df = _base_df()                                          # 仅 45 根 → 趋势门历史不足
    t = len(df) - 1
    base = s02.signal_at(df, t)
    assert base["SELECT"] is True                            # base S02 命中
    on = s02.signal_at(df, t, trend_filter=True, rs_rank=90.0)
    assert "趋势门" in on
    assert on["趋势门"]["PASS"] is False                      # 历史不足 → 不过
    assert on["SELECT"] is False                             # 趋势门否决


def test_signal_at_trend_filter_passthrough_when_base_false():
    """base 未命中时,开不开趋势门 SELECT 都是 False(不误放行)。"""
    closes = [100.0] * 44 + [99.8]                           # C2 假 → base False
    df = _df(_vols(), closes)
    t = len(df) - 1
    assert s02.signal_at(df, t)["SELECT"] is False
    on = s02.signal_at(df, t, trend_filter=True, rs_rank=99.0)
    assert on["SELECT"] is False


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
