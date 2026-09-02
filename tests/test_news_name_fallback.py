"""修「新闻 AI『与本股关系』把策略选中票的本股新闻系统性误判为无关」的验收单测。

根因:name 取不到时回退成 code,prompt「主体并非{name}即无关」里的 {name} 变成一串代码,
LLM 严格执行把本股新闻(主体是公司名、非代码)判成「无关」,下游直接丢弃 → 情绪信号被掏空。

本文件锁四类语义(其中两对是对立断言,防未来重写无意删规则):
  1. 主验收:选中票(池外)重跑富集,标题含公司名的本股新闻 → 与本股关系不再为「无关」。
  2. 回归:name_fallback_ratio==0(选中集全拿到真名);真缺名的代码才计入回退。
  3. 不劣化:自选池票(name 本就正确)解析/措辞不变(没把无关过度翻正)。
  4. 蹭词保护:主体是别家公司、只顺带提代码的新闻,修复后仍判「无关」。
  + prompt 兜底:name 缺失/等于代码/纯数字时不生成「主体并非{name}」这句,改「无法判断填间接」;
    带配置开关(NEWS_PROMPT_NAME_GUARD)可 A/B 比对。
"""
import re

from tools.analysis import event as ev
from tools.llm import prompts


# ---------- 1+2:name 回退链 resolve_name / name_fallback_stats ----------
def test_resolve_name_sample_codes_get_real_name():
    """两只样本(池外)经全A映射拿到真名,不回退成代码。"""
    assert ev.resolve_name("002084") == ("海鸥住工", False)
    assert ev.resolve_name("301246") == ("宏源药业", False)


def test_resolve_name_pool_code_uses_pool_name():
    """自选池票走 stock_pool 真名(第一优先级)。"""
    name, fell = ev.resolve_name("300308")
    assert name == "中际旭创" and fell is False


def test_resolve_name_unknown_code_falls_back_to_code():
    """全链未命中(真不存在的代码)才回退成 code,并标 fell_back。"""
    assert ev.resolve_name("999999") == ("999999", True)


def test_name_fallback_ratio_zero_for_named_codes():
    """回归验收:选中集全部有真名 → ratio==0、无回退。"""
    st = ev.name_fallback_stats(["002084", "301246", "300308"])
    assert st["ratio"] == 0.0 and st["fallback"] == 0 and st["fallback_codes"] == []


def test_name_fallback_ratio_counts_only_missing():
    """只有真缺名的代码计入回退,ratio 反映占比(观测点第一天就能发现问题)。"""
    st = ev.name_fallback_stats(["002084", "999999"])
    assert st["fallback"] == 1 and st["fallback_codes"] == ["999999"] and st["ratio"] == 0.5


# ---------- prompt 兜底:名字缺失不再默认翻转成「无关」 ----------
def test_prompt_real_name_keeps_anti_hitchhike_line():
    """真名在场:保留「主体并非{name}即无关」原措辞(防蹭词语义不变)。"""
    instr = prompts.news_extract_instruction("海鸥住工", "002084")
    assert "海鸥住工" in instr
    assert "主体并非海鸥住工" in instr


def test_prompt_guard_drops_trap_when_name_equals_code(monkeypatch):
    """name 等于代码:兜底生效——不再出现「主体并非{code}」陷阱句,
    改为以代码对应公司为准、无法判断填『间接』(缺名不翻转成无关)。"""
    monkeypatch.setattr(prompts.settings, "NEWS_PROMPT_NAME_GUARD", True)
    instr = prompts.news_extract_instruction("002084", "002084")
    assert "主体并非002084" not in instr                 # 陷阱句消失
    assert "股票代码 002084 对应的上市公司" in instr      # 改用代码锚定公司
    assert "无法判断时填'间接'" in instr                  # 缺名 → 间接,不是无关


def test_prompt_guard_triggers_on_pure_digit_or_empty(monkeypatch):
    monkeypatch.setattr(prompts.settings, "NEWS_PROMPT_NAME_GUARD", True)
    for nm in ("", "301246"):
        instr = prompts.news_extract_instruction(nm, "301246")
        assert "主体并非" not in instr and "对应的上市公司" in instr


def test_prompt_guard_off_restores_legacy_phrasing(monkeypatch):
    """关掉开关(A/B 比对):name==code 时恢复旧措辞(陷阱句复现),证明开关有效。"""
    monkeypatch.setattr(prompts.settings, "NEWS_PROMPT_NAME_GUARD", False)
    instr = prompts.news_extract_instruction("002084", "002084")
    assert "主体并非002084" in instr


# ---------- 3+4:端到端(hermetic)——本股不再无关 / 蹭词仍无关(对立断言) ----------
class _RelClient:
    """模拟 LLM 按指令判「与本股关系」的机制:从指令 label【名字(代码)】解出目标名,
    新闻文本以该名为主体(含该名)→ 直接;主体是别家/只提代码 → 无关。

    这复现真实失效链:若喂进去的『名字』其实是代码,本股新闻正文讲的是公司名、不含代码串
    → 被判无关。也据此锁「真名在场则本股新闻判直接、蹭词判无关」这对对立语义。
    """
    def extract(self, text, schema, *, instruction, temperature=0.0):
        m = re.search(r"【(.+?)\(", instruction)
        target = m.group(1) if m else ""
        rel = "直接" if target and target in text else "无关"
        return {"事件类型": "业绩", "影响方向": "利好", "影响强度": 4,
                "与本股关系": rel, "摘要": "s", "原因": "r"}


def _prep(monkeypatch, tmp_path, items):
    monkeypatch.setattr(ev.settings, "LLM_CACHE", tmp_path)
    monkeypatch.setattr(ev.settings, "LLM_EXTRACT_WORKERS", 1)
    monkeypatch.setattr(ev.nw, "load_news", lambda code: list(items))


def test_own_stock_news_not_irrelevant_after_fix(monkeypatch, tmp_path):
    """主验收 + 不劣化:池外选中票 002084 的本股新闻(标题含『海鸥住工』)→ 直接,不再无关。"""
    items = [
        {"title": "7连板海鸥住工:若股价进一步异常上涨,可能申请停牌核查", "content": "海鸥住工公告",
         "time": "2026-09-02", "source": "s", "url": "u1"},
        {"title": "海鸥住工涨1.42%,今日主力净流入-8808.24万", "content": "海鸥住工资金",
         "time": "2026-09-02", "source": "s", "url": "u2"},
    ]
    _prep(monkeypatch, tmp_path, items)
    events = ev.extract_news_events("002084", client=_RelClient())
    assert [e["与本股关系"] for e in events] == ["直接", "直接"]
    # 下游聚合不再把这些本股新闻当无关剔除(样本数=2)
    assert ev.aggregate_sentiment(events)["样本数"] == 2


def test_hitchhike_news_still_irrelevant_after_fix(monkeypatch, tmp_path):
    """蹭词保护(对立断言):301246 下一条主体是别家公司、只顺带提代码的新闻 → 仍判无关。"""
    items = [
        {"title": "宏源药业上半年归母净利润1.56亿元 同比增长2789%", "content": "宏源药业中报",
         "time": "2026-09-02", "source": "s", "url": "u1"},                       # 本股 → 直接
        {"title": "电解液周报丨比亚迪领衔,新宙邦冲刺港交所", "content": "涉及代码301246 板块",
         "time": "2026-09-02", "source": "s", "url": "u2"},                       # 蹭词 → 无关
    ]
    _prep(monkeypatch, tmp_path, items)
    events = ev.extract_news_events("301246", client=_RelClient())
    assert events[0]["与本股关系"] == "直接"       # 本股中报不再无关
    assert events[1]["与本股关系"] == "无关"       # 蹭词仍无关(修复没过度翻正)
    assert ev.aggregate_sentiment(events)["样本数"] == 1   # 只 1 条本股进净情绪


def test_bug_reproduction_without_real_name(monkeypatch, tmp_path):
    """回归锁:若强制拿不到真名(name→code),本股新闻会被误判无关——证明真名解析正是修复点。"""
    items = [{"title": "海鸥住工连续7个交易日涨停收盘", "content": "海鸥住工",
              "time": "2026-09-02", "source": "s", "url": "u1"}]
    _prep(monkeypatch, tmp_path, items)
    # 断掉全链真名来源:池外 + 全A映射都查不到 → resolve_name 回退成 code
    monkeypatch.setattr(ev.stock_pool, "get", lambda code: None)
    import tools.analysis.serialize as sz
    monkeypatch.setattr(sz, "_code_name", lambda code: None)
    events = ev.extract_news_events("002084", client=_RelClient())
    assert events[0]["与本股关系"] == "无关"       # 缺真名 → 复现误判(修复前行为)
