"""news_persistence.py 单测:输出契约规整(纯代码,hermetic)+ 缓存命中(mock)
+ 典型结构性 vs 短暂各判对(真实 LLM,未配置则跳过)。

断言锁住"为什么改"的语义:①持续性/方向/印证强度只能落在受限取值集(防下游分组被脏值污染);
②同一文本命中缓存不重复烧钱;③构造的典型结构性/短暂消息 LLM 能各判对(防 prompt 被改坏)。
"""
import pytest

from tools.analysis import event as ev
from tools.analysis import news_persistence as npst
from tools.llm import client as lc


# ---------- 纯代码:输出规整契约 ----------
def test_normalize_clamps_to_allowed_values():
    # 越界值降级:持续性/方向→中性,印证强度→弱;依据保留
    r = npst._normalize({"持续性": "永久性", "方向": "暴涨", "印证强度": "超强", "依据": "x"})
    assert r["持续性"] == "中性"
    assert r["方向"] == "中性"
    assert r["印证强度"] == "弱"
    assert r["依据"] == "x"


def test_normalize_keeps_valid_values():
    r = npst._normalize({"持续性": "结构性持续", "方向": "利好", "印证强度": "强", "依据": "在手订单饱满"})
    assert r == {"持续性": "结构性持续", "方向": "利好", "印证强度": "强", "依据": "在手订单饱满"}


def test_normalize_error_passthrough():
    r = npst._normalize({"error": "timeout"})
    assert r["持续性"] is None and r["error"] == "timeout"


def test_classify_empty_text_degrades():
    r = npst.classify("   ")
    assert r["持续性"] is None and "error" in r


# ---------- 缓存(mock LLM,不触网)----------
class _FakeClient:
    def __init__(self):
        self.calls = 0

    def extract(self, text, schema, *, instruction, temperature=0.0):
        self.calls += 1
        return {"持续性": "结构性持续", "方向": "利好", "印证强度": "强", "依据": "构造"}


def test_cache_hit_avoids_second_call(monkeypatch, tmp_path):
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path)
    c = _FakeClient()
    r1 = npst.classify("同一段公告文本", client=c)
    r2 = npst.classify("同一段公告文本", client=c)
    assert r1 == r2 and c.calls == 1                 # 第二次命中缓存
    assert r1["持续性"] == "结构性持续"


# ---------- 真实 LLM:典型结构性 vs 短暂各判对(未配置则跳过)----------
_STRUCTURAL = (
    "公司公告:与国际龙头客户签订为期三年的长期供货框架协议,新增在手订单合计约 80 亿元,"
    "并投产第二条产线扩充产能约 40%;近三个报告期归母净利润连续同比高增长(去年+45%、今年一季度+52%)。"
)
_TRANSIENT = (
    "公司公告:因本期出售一处闲置厂房获得一次性资产处置收益约 1.2 亿元,使本季度实现扭亏为盈;"
    "公司同时提示,主营业务经营状况未发生实质性变化,该项收益为非经常性损益、不具可持续性。"
)


@pytest.mark.skipif(not lc.is_configured(), reason="LLM env 未配置")
def test_live_structural_vs_transient():
    struct = npst.classify(_STRUCTURAL)
    trans = npst.classify(_TRANSIENT)
    # LLM 配额/网络降级(返回 error)时跳过——外部不可用不应判逻辑失败,只在真能调通时校验判对
    if struct.get("error") or trans.get("error"):
        pytest.skip(f"LLM 调用降级(配额/网络),跳过: {struct.get('error') or trans.get('error')}")
    assert struct.get("持续性") == "结构性持续", struct
    assert trans.get("持续性") == "短暂事件", trans
