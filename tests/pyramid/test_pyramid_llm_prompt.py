"""W3 语义锁：d2_pyramid_llm_select 的 SYSTEM_PROMPT（DeepSeek/千问 共用·闭卷合成层）。

锁两件事，防未来重写误伤：
  ① 新增的"辩证核对"段在位（我vs骨架出入 / 借 digest 辨真伪 / 区分 α-β 读大盘caveat / 诚实不硬凑）;
  ② 既有闭卷骨架约束一字不删（池内选 / 不得产价位或分数 / 每判断引用某工具浓缩块具体一行 /
     买入规避不重叠 / 诚实边界）。辩证是"开深度"，不是放开广度或数值。
"""
from tools.experimental.d2_pyramid_llm_select import SYSTEM_PROMPT


def test_prompt_含辩证核对段():
    p = SYSTEM_PROMPT
    assert "辩证核对" in p
    assert "我的判断 vs 骨架" in p           # 我vs系统出入核对，写进 adjust_log
    assert "adjust_log" in p
    assert "新闻digest" in p and "辨真伪" in p  # 消息面借 digest 辨真伪，不只看净情绪标量
    assert "区分 α" in p                      # α(个股) vs β(大盘) 区分
    assert "效力诚实标注" in p                # 大盘走"⚠️效力诚实标注"、不洗白
    assert "不硬凑" in p                      # 诚实不硬凑（不够格明说只给 N 只）


def test_prompt_保留闭卷骨架约束():
    """辩证增补不得删掉受限调整的硬边界（开深度 ≠ 放开广度/数值/无据加塞）。"""
    p = SYSTEM_PROMPT
    assert "之内" in p                                    # 只在骨架 top-N 池之内选
    assert "不得产出任何价位或分数数字" in p              # 数值程序算·LLM 不产数字
    assert "引用某工具浓缩块里的具体一行" in p            # 每判断有据·不主观加塞
    assert "买入与规避名单不得重叠" in p
    assert "诚实边界" in p                                # 代理覆盖诚实标注段仍在


def test_prompt_不洗白大盘效力():
    """区分 α/β 段须明确禁止把大盘上行概率读成个股必涨 / 高概率读成能赚钱。"""
    p = SYSTEM_PROMPT
    assert "不得把大盘上行概率读成个股必涨" in p
    assert '不得把"高概率"读成"能赚钱"' in p
