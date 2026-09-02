"""反转否决层(策略10 专属·基本面/消息面否决/降级)语义锁。

锁死"为什么这么设计"的语义,防未来 prompt/代码重写误删规则:
  1. 壳股全轴命中 → 否决/降级触发(dose 单调、罚分封顶)。
  2. 有基本面支撑的真超跌票 → 不误杀(全轴不命中)。
  3. kill-switch(启用=False)→ 纯量价 no-op(veto_verdict 不触发、apply_to_score 恒等)。
  4. 分轴开关:关某轴 → 该轴不发声。
  5. 缺数据保守:特征 None/空 → 不误否决。
  6. 防未来函数:veto_verdict 纯函数,同入同出;event 抽取剔除 date>as_of 公告。
  7. combo 集成:挂 风险特征 时降级沉底 / 否决剔除;无特征不回归。

⚠️ 非投资建议:否决层只改选股展示/入选。
"""
from __future__ import annotations

import pytest

from tools.strategy import reversal_turnover as rt
from tools.strategy import reversal_veto as rv


# —— 测试固定 config(免受默认漂移)——
CFG_DOWN = {
    "启用": True, "模式": "降级", "每轴罚分": 1.5, "罚分上限": 4.0, "否决沉底保留展示": True,
    "轴": {"基本面空心": {"启用": True, "高危红旗数门槛": 1, "扣非大幅下滑%": -50.0},
          "事件博弈": {"启用": True, "窗口天数": 30},
          "治理风险": {"启用": True},
          "重组未完成": {"启用": True, "窗口天数": 90}},
    "停复牌重组关键词": ["停牌", "复牌", "重大资产重组", "资产重组", "发行股份购买资产"],
    "异常波动关键词": ["异常波动", "交易异常"],
    "重组在途关键词": ["重大资产重组", "资产重组", "发行股份购买资产", "筹划重组"],
    "重组落地关键词": ["重组完成", "过会", "实施完毕"],
}
CFG_VETO = {**CFG_DOWN, "模式": "否决", "否决沉底保留展示": True}
CFG_VETO_DROP = {**CFG_DOWN, "模式": "否决", "否决沉底保留展示": False}
CFG_OFF = {**CFG_DOWN, "启用": False}


def _feat(**kw):
    f = dict(rv._EMPTY_FEATURES)
    f.update(kw)
    return f


# ———————————————— 1. 壳股全轴命中 ————————————————
def test_shell_stock_all_axes_trigger():
    """壳股:高危红旗+扣非为负+龙虎榜+停复牌重组+ST+重组未完成 → 四轴全中,降级罚分封顶。"""
    f = _feat(fund_high_flags=2, 扣非为负=True, lhb={"triggered": True, "reason": "净买上榜"},
              is_st=True, 停复牌重组=True, 重组未完成=True)
    v = rv.veto_verdict(f, CFG_DOWN)
    assert v["触发"] is True
    assert v["命中轴数"] == 4                         # 四轴全中
    assert set(k for k, hit in v["轴"].items() if hit) == {
        "基本面空心", "事件博弈", "治理风险", "重组未完成"}
    assert v["罚分"] == pytest.approx(4.0)            # 1.5×4=6 → 封顶 4.0
    assert v["动作"] == "降级" and v["否决"] is False


def test_single_axis_fundamental_hollow():
    """仅基本面空心(扣非增速大幅下滑)→ 触发一轴。"""
    v = rv.veto_verdict(_feat(扣非增速=-80.0), CFG_DOWN)
    assert v["触发"] and v["命中轴数"] == 1 and v["轴"]["基本面空心"]
    assert v["罚分"] == pytest.approx(1.5)


def test_dose_monotone_penalty():
    """命中轴数越多罚分越大(dose 单调),直到封顶。"""
    v1 = rv.veto_verdict(_feat(is_st=True), CFG_DOWN)
    v2 = rv.veto_verdict(_feat(is_st=True, 扣非为负=True), CFG_DOWN)
    v3 = rv.veto_verdict(_feat(is_st=True, 扣非为负=True, 异常波动=True), CFG_DOWN)
    assert v1["罚分"] < v2["罚分"] < v3["罚分"]


# ———————————————— 2. 不误杀真超跌优质票 ————————————————
def test_healthy_oversold_not_killed():
    """有基本面支撑的真超跌票(无红旗/扣非正增长/非ST/无事件)→ 全轴不命中,不误杀。"""
    f = _feat(fund_high_flags=0, 扣非为负=False, 扣非增速=25.0, lhb=None, is_st=False,
              停复牌重组=False, 异常波动=False, 重组未完成=False)
    v = rv.veto_verdict(f, CFG_DOWN)
    assert v["触发"] is False and v["命中轴数"] == 0 and v["罚分"] == 0.0


def test_lhb_not_triggered_no_event_axis():
    """龙虎榜裁决存在但 triggered=False → 事件轴不因龙虎榜发声。"""
    v = rv.veto_verdict(_feat(lhb={"triggered": False, "reason": "近7日无净买上榜"}), CFG_DOWN)
    assert v["触发"] is False


# ———————————————— 3. kill-switch ————————————————
def test_kill_switch_off_no_op():
    """启用=False → 即便全轴特征命中也不触发(纯量价 no-op)。"""
    f = _feat(fund_high_flags=5, is_st=True, 停复牌重组=True, 重组未完成=True)
    v = rv.veto_verdict(f, CFG_OFF)
    assert v["应用"] is False and v["触发"] is False and v["罚分"] == 0.0


def test_apply_to_score_identity_when_not_triggered():
    """未触发 → apply_to_score 恒等(降级沉底口径向后兼容)。"""
    v = rv.veto_verdict(_feat(), CFG_DOWN)
    assert rv.apply_to_score(1.23, v) == pytest.approx(1.23)
    assert rv.apply_to_score(-0.5, v) == pytest.approx(-0.5)


# ———————————————— 4. 分轴开关 ————————————————
def test_axis_toggle_off_silences_axis():
    """关治理风险轴 → ST 不再触发。"""
    cfg = {**CFG_DOWN, "轴": {**CFG_DOWN["轴"], "治理风险": {"启用": False}}}
    v = rv.veto_verdict(_feat(is_st=True), cfg)
    assert v["触发"] is False


# ———————————————— 5. 缺数据保守 ————————————————
def test_none_features_conservative():
    """特征 None → 不误否决(缺数据保守降级)。"""
    v = rv.veto_verdict(None, CFG_DOWN)
    assert v["触发"] is False and v["命中轴数"] == 0


# ———————————————— 6. 否决模式 + 符号安全 + 纯函数 ————————————————
def test_veto_mode_sinks_and_drops():
    f = _feat(is_st=True)
    v_keep = rv.veto_verdict(f, CFG_VETO)
    assert v_keep["否决"] is True and v_keep["剔除"] is False
    assert rv.apply_to_score(0.9, v_keep) < -1e5             # 强制沉底
    v_drop = rv.veto_verdict(f, CFG_VETO_DROP)
    assert v_drop["否决"] is True and v_drop["剔除"] is True


def test_downgrade_sign_safe_negative_not_promoted():
    """降级=减正罚分,对负分票也单调下沉,绝不被抬高。"""
    v = rv.veto_verdict(_feat(is_st=True), CFG_DOWN)
    assert rv.apply_to_score(-0.30, v) < -0.30


def test_pure_same_input_same_output():
    f = _feat(is_st=True, 扣非为负=True)
    assert rv.veto_verdict(f, CFG_DOWN) == rv.veto_verdict(f, CFG_DOWN)


# ———————————————— 防未来函数:event 抽取剔除 date>as_of ————————————————
def test_extract_events_drops_future_announcements():
    """公告 date>as_of 视为未来函数被剔;date<=as_of 且窗口内的停复牌重组命中。"""
    ann = [
        {"date": "2026-09-10", "title": "关于重大资产重组停牌公告"},   # 未来(>as_of)→ 剔
        {"date": "2026-08-20", "title": "关于股票交易异常波动的公告"},  # 过去且窗口内 → 命中异常波动
    ]
    ev = rv._event_features("000001", "2026-09-01", ann=ann, c=CFG_DOWN)
    assert ev["异常波动"] is True
    assert ev["停复牌重组"] is False                         # 未来的重组公告不算


def test_extract_events_reorg_incomplete_vs_done():
    """重组在途且窗口内无落地词 → 未完成;出现落地词 → 解除。"""
    in_prog = [{"date": "2026-08-15", "title": "筹划重大资产重组的提示性公告"}]
    ev1 = rv._event_features("000002", "2026-09-01", ann=in_prog, c=CFG_DOWN)
    assert ev1["重组未完成"] is True
    done = in_prog + [{"date": "2026-08-28", "title": "重大资产重组过会公告"}]
    ev2 = rv._event_features("000002", "2026-09-01", ann=done, c=CFG_DOWN)
    assert ev2["重组未完成"] is False


# ———————————————— 7. combo 集成:降级沉底 / 否决剔除 / 无特征不回归 ————————————————
def _rec(rev, turn, feat=None):
    r = {"meta": {"code": "x"}, "反转低换手": {"rev": rev, "turn": turn, "amount_wan": 99999},
         "snapshot": {"pct_chg": 0.0, "close": 10.0}}
    if feat is not None:
        r["风险特征"] = feat
    return r


def test_combo_downgrade_sinks_shell(monkeypatch):
    """挂 风险特征(壳股)→ 降级后从榜首沉到干净票之下(apply_veto=True)。"""
    monkeypatch.setattr(rv, "cfg", lambda: CFG_DOWN)
    # SHELL 原始反转分最高(跌最多),但命中 ST+扣非为负 → 降级沉底
    records = {
        "SHELL": _rec(0.30, -0.5, _feat(is_st=True, 扣非为负=True)),
        "CLEAN1": _rec(0.10, -0.4, _feat()),
        "CLEAN2": _rec(0.05, -0.3, _feat()),
    }
    out = rt.combo_reversal_turnover_screen(records, top_k=3, apply_veto=True)
    assert out["codes"][0] != "SHELL"                        # 不再是榜首
    assert out["codes"][-1] == "SHELL"                       # 沉底
    assert out["否决层"]["命中数"]["降级"] == 1


def test_combo_veto_drop_removes_shell(monkeypatch):
    """否决·不保留展示 → 壳股从入选清单剔除。"""
    monkeypatch.setattr(rv, "cfg", lambda: CFG_VETO_DROP)
    records = {
        "SHELL": _rec(0.30, -0.5, _feat(is_st=True)),
        "CLEAN1": _rec(0.10, -0.4, _feat()),
        "CLEAN2": _rec(0.05, -0.3, _feat()),
    }
    out = rt.combo_reversal_turnover_screen(records, top_k=3, apply_veto=True)
    assert "SHELL" not in out["codes"]
    assert out["否决层"]["命中数"]["剔除"] == 1


def test_combo_no_features_no_regression(monkeypatch):
    """apply_veto=True 但无风险特征 → 排序与纯量价一致(不回归)。"""
    monkeypatch.setattr(rv, "cfg", lambda: CFG_DOWN)
    records = {"A": _rec(0.30, -0.5), "B": _rec(0.10, -0.4), "C": _rec(0.05, -0.3)}
    with_veto = rt.combo_reversal_turnover_screen(records, top_k=3, apply_veto=True)
    no_veto = rt.combo_reversal_turnover_screen(records, top_k=3, apply_veto=False)
    assert with_veto["codes"] == no_veto["codes"]


def test_combo_apply_veto_false_ignores_features(monkeypatch):
    """apply_veto=False 显式关 → 即便挂了风险特征也不否决(A/B 回测的 A 腿)。"""
    monkeypatch.setattr(rv, "cfg", lambda: CFG_DOWN)
    records = {
        "SHELL": _rec(0.30, -0.5, _feat(is_st=True, 扣非为负=True)),
        "CLEAN1": _rec(0.10, -0.4, _feat()),
    }
    out = rt.combo_reversal_turnover_screen(records, top_k=2, apply_veto=False)
    assert out["codes"][0] == "SHELL"                        # 未否决,壳股仍居首
    assert "否决层" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
