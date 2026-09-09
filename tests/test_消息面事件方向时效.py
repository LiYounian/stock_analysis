"""消息面回灌打分·事件方向/时效缺陷修复单测(2026-09-09)。

锁语义(为什么这么写,防未来重写误删规则;缺陷复盘见 docs/每日分析/选股/2026-09-08.md §2/§6):
  修复1 方向符号:纯减持样本 → 事件驱动=看空(增持=正、减持=负,不误判为利好)。
  修复2 时效过滤:陈旧(>N月)增减持不进当日消息面(锁 600356 一类);近期真实增减持照常计入;
        开关关 → 退回旧无下限行为(可逆)。
  修复3 重组承诺文本排除:"承诺不减持/锁定期/转让限制"不计减持利空;真二级抛售优先。
  修复4 代码归属:别的 code 的减持不串味到本 code(锁 000019 vs 000027 张冠李戴)。
  修复5 方向冲突:事件驱动(真减持=看空)与情绪三层(看多)冲突时,情绪不得单方面把减持拉正
        → 消息面分 ≤ 0(锁 000019 一类);开关关 → 退回旧行为(可逆)。
  防未来:变动日期 > as_of 的不参与(回归保留)。
"""
import pandas as pd
import pytest

from tools.analysis.event_driven import judge, summary
from tools.collectors import event_driven as _ed
from tools.config.strategy import THRESHOLDS
from tools.pipeline import candidate_message as cm


def _insider(rows):
    return pd.DataFrame(rows)


@pytest.fixture
def _no_earnings(monkeypatch):
    """断开业绩精数值路径(避免触网),只留 ggcg 增减持。"""
    monkeypatch.setattr(summary, "_quarter_ends_before", lambda a, b: [])


# ———————————— 修复1 · 方向符号正确(纯减持=看空)————————————
def test_pure_recent_reduction_is_bearish(monkeypatch, _no_earnings):
    """纯减持(近期真实二级抛售)→ 事件驱动=看空(增持正/减持负,不误判利好)。"""
    fake = _insider([{"code": "000019", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2026-08-01"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("000019", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空" and s["强度"] < 0


# ———————————— 修复2 · 时效过滤(陈旧减持不误压)————————————
def test_stale_reduction_filtered_out(monkeypatch, _no_earnings):
    """只有陈旧(>6月)减持、近期无事件 → 陈旧被过滤 → 无事件 → 弃权中性(锁 600356 一类)。"""
    fake = _insider([{"code": "600356", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2020-09-12"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    # 2026-08-08 as_of,减持在 2020(远早于 6 月窗)→ 过滤 → 无可用事件 → 弃权 None(不再 -1.0 强看空)
    assert summary.summarize("600356", "2026-08-08", announcements=[]) is None


def test_recent_reduction_within_window_kept(monkeypatch, _no_earnings):
    """近期(≤6月)真实减持照常计入 → 看空(时效过滤只砍陈旧、不误伤近期)。"""
    fake = _insider([{"code": "000001", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2026-06-20"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("000001", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空"


def test_recent_buy_over_stale_sell(monkeypatch, _no_earnings):
    """近期增持 + 陈旧减持 → 陈旧减持被过滤、只剩近期增持 → 看多且依据是增持(锁 000035 一类)。"""
    fake = _insider([
        {"code": "000035", "方向": "减持", "变动股数": 1e6, "方式": "集中竞价", "日期": "2026-02-01"},
        {"code": "000035", "方向": "增持", "变动股数": 1e6, "方式": None, "日期": "2026-08-20"},
    ])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("000035", "2026-09-08", announcements=[])
    assert s is not None and s["方向"] == "看多"
    assert any("增持" in d for d in s["依据"])          # 依据是近期增持,不是上半年旧减持


def test_stale_filter_switch_off_reverts(monkeypatch, _no_earnings):
    """开关「增减持时效过滤」关 → 陈旧减持仍计入(退回旧无下限行为,可逆)。"""
    monkeypatch.setitem(THRESHOLDS["事件驱动"], "增减持时效过滤", False)
    fake = _insider([{"code": "600356", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2020-09-12"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("600356", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空"          # 关开关 → 陈旧仍被当利空(旧行为)


def test_missing_date_reduction_kept(monkeypatch, _no_earnings):
    """缺日期的增减持记录保守保留(时效过滤不因缺日期而漏,向后兼容)。"""
    fake = _insider([{"code": "000001", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": None}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("000001", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空"


# ———————————— 修复3 · 重组承诺文本排除 ————————————
def test_reorg_promise_text_not_bearish():
    """重组"承诺不减持/锁定期/转让限制"文本 → 类别'非减持承诺'、中性、不计利空。"""
    cls = judge.classify_share_change("减持", text="重组报告书:控股股东承诺不减持,锁定期36个月")
    assert cls["类别"] == "非减持承诺" and cls["看空有效"] is False
    v = judge.judge_corporate_action("减持", None, text="承诺不减持、转让限制期内不减持")
    assert v["方向"] == "中性" and v["强度"] == 0.0


def test_secondary_market_wins_over_promise_text():
    """真二级抛售(集中竞价)优先于承诺文本:既有'集中竞价减持'又提'锁定'时仍看空。"""
    cls = judge.classify_share_change("减持", method="集中竞价",
                                      text="拟集中竞价减持不超过2%,剩余股份继续锁定")
    assert cls["类别"] == "二级减持" and cls["看空有效"] is True


def test_reorg_promise_in_precise_path_not_bearish(monkeypatch, _no_earnings):
    """端到端:ggcg 减持 + 公告文本为重组承诺不减持 → 事件驱动不再机械看空(非减持承诺→中性)。"""
    fake = _insider([{"code": "600356", "方向": "减持", "变动股数": 1e6,
                      "方式": None, "日期": "2026-08-01"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    anns = [{"date": "2026-08-02", "type": "重组",
             "title": "发行股份购买资产报告书:交易对方承诺不减持、锁定期36个月",
             "summary": "转让限制期内不减持"}]
    s = summary.summarize("600356", "2026-08-08", announcements=anns)
    assert s is not None and s["方向"] != "看空"          # 承诺不减持文本不被当利空


# ———————————— 修复4 · 代码归属校验(防张冠李戴)————————————
def test_code_attribution_isolated(monkeypatch, _no_earnings):
    """别的 code(000027 深圳能源)的减持不串味到本 code(000019 深粮控股)。"""
    fake = _insider([{"code": "000027", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2026-08-01"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    # 查 000019:缓存里只有 000027 的减持 → 不串味 → 000019 无事件 → 弃权 None
    assert summary.summarize("000019", "2026-08-08", announcements=[]) is None
    # 查 000027 本尊:照常看空
    s = summary.summarize("000027", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空"


def test_code_attribution_normalizes_format(monkeypatch, _no_earnings):
    """代码格式差异(int/带后缀)归一化后仍能对齐(不因格式漏配)。"""
    fake = _insider([{"code": 19, "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2026-08-01"}])   # 采集侧写成 int 19
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    s = summary.summarize("000019", "2026-08-08", announcements=[])
    assert s is not None and s["方向"] == "看空"          # 19 归一化=000019 → 对齐


# ———————————— 防未来:变动日期 > as_of 不参与(回归保留)————————————
def test_future_dated_reduction_skipped(monkeypatch, _no_earnings):
    fake = _insider([{"code": "000001", "方向": "减持", "变动股数": 1e6,
                      "方式": "集中竞价", "日期": "2026-12-31"}])
    monkeypatch.setattr(_ed, "load_insider_trades", lambda tag="latest": fake)
    assert summary.summarize("000001", "2026-08-08", announcements=[]) is None


# ———————————— 修复5 · 回灌层方向冲突处理 ————————————
def _fake_convene_conflict():
    """伪 council.convene:消息面合议综合看多(情绪拉的),但归因里事件驱动=看空(真减持);
    base 合议中性。用于隔离测试 _score_one 的方向冲突逻辑(不依赖磁盘专家)。"""
    def _f(expert_names, record, *a, **k):
        if list(expert_names) == ["情绪三层", "事件驱动"]:
            return {"综合方向": "看多", "综合分": 0.6, "是否冲突": True,
                    "归因": [
                        {"专家": "情绪三层", "方向": "看多", "置信度": 0.8, "贡献": 0.5,
                         "依据": ["净情绪偏多"], "数据充分度": "充分"},
                        {"专家": "事件驱动", "方向": "看空", "置信度": 1.0, "贡献": -0.3,
                         "依据": ["集中竞价减持"], "数据充分度": "充分"},
                    ]}
        return {"综合方向": "中性", "综合分": 0.2, "归因": []}   # base(数据面综合分)
    return _f


def test_conflict_downweight_neutralizes_positive(monkeypatch):
    """事件驱动=看空(真减持)而情绪=看多冲突 → 情绪不得单方面把减持拉正 → 消息面分 ≤ 0。"""
    monkeypatch.setattr(cm.council, "convene", _fake_convene_conflict())
    out = cm.rescore_pool(["X"], provenance={},
                          load_record=lambda c: {"meta": {"code": "X", "name": "X"}})
    x = out[0]
    assert x["消息面方向"] == "看多"                      # council 综合方向仍是看多(情绪拉的)
    assert x["方向冲突"] is True and x["冲突降权"] is True
    assert x["消息面分"] <= 0                             # 中和:情绪不能把真减持拉成正分


def test_conflict_downweight_switch_off_reverts(monkeypatch):
    """开关「方向冲突降权」关 → 不中和,情绪拉正的消息面分保留(退回旧行为,可逆)。"""
    monkeypatch.setattr(cm.council, "convene", _fake_convene_conflict())
    cfg_off = {**THRESHOLDS["消息面回灌"], "方向冲突降权": False}
    out = cm.rescore_pool(["X"], provenance={},
                          load_record=lambda c: {"meta": {"code": "X", "name": "X"}},
                          cfg=cfg_off)
    x = out[0]
    assert x["冲突降权"] is False and x["消息面分"] > 0    # 旧行为:情绪拉正未被中和
