"""文字档 → 标准描述 + 数值权重 的集中映射(「先定义再归类」范式的落地件)。

用户原则(硬约束):**大模型不擅长打数值分,要给具体描述**。范式正解见
`tools/analysis/sector_forecast/news_catalyst.py::RUBRIC`——每个维度的每一档都写死
定义、全文字、绝不让 LLM 给数值。本模块把该范式推广到 tools/llm/prompts.py 的所有
打分类提示词:

  ① 定义档(LEVELS 字典:档名 → 明确定义),供 prompts.py 渲染进指令 + SCHEMA(单一真源,
     测试据此断言「每档定义都在提示词里」);
  ② 回填(helpers):LLM 只输出**文字档**,代码再把档映射成
       · 标准描述(*_desc,口径统一,供人/下游看)
       · 数值(*_to_num / *_sign / *_to_net,需要计算时才用)。

**legacy 兼容(务必)**:老缓存 / 老 LLM 结果里 影响强度是 1~5 数值、净情绪是 -1~1 小数。
所有 *_to_num / *_to_net 对**数值或数字串直接透传**,只有文字档才查表——故新旧混跑不崩,
下游行为等价或更稳(见 docs/计划)。⚠️ 测试环境研究模拟,非投资建议。
"""
from __future__ import annotations

# ════════════════════ 一、方向(利好 / 利空 / 中性)════════════════════
# 用于:news_extract.影响方向、policy_score.影响方向、UGC 之外的方向判定。
DIRECTION_LEVELS = {
    "利好": "该消息对标的基本面或短期预期是**正向推动**:政策扶持落地、订单/中标、"
            "业绩超预期、技术突破、需求/价格向上拐点等。",
    "利空": "该消息对标的基本面或短期预期是**负向拖累**:监管收紧/处罚、诉讼败诉、"
            "业绩暴雷、减持/解禁、需求/价格向下拐点等。",
    "中性": "无明确多空方向,或多空对冲、纯事务性信息:日常经营公告、常规人事变动、"
            "影响方向不明或尚不可判。",
}
DIRECTION_SIGN = {"利好": 1, "利空": -1, "中性": 0}


# ════════════════════ 二、影响强度(强 / 中 / 弱)════════════════════
# 用于:news_extract.影响强度、policy_score.影响强度。
# 数值口径:回填到 1~5 档(强=5/中=3/弱=1),使下游既有 `强度/5` 归一化无需改动;
# legacy 1~5 数值原样透传。
STRENGTH_LEVELS = {
    "强": "直接、显著改变标的基本面或短期预期:政策落地、大额订单/中标、技术突破、"
          "产能/价格拐点、业绩大幅超预期或暴雷等**量级大、确定性高**的事件。",
    "中": "边际影响:单一业务进展、评级/目标价调整、非核心事件,方向明确但**量级有限**。",
    "弱": "轻微或事务性:常规经营、日常公告、股价异动播报、未证实传闻,对基本面**几无实质改变**。",
}
STRENGTH_NUM = {"强": 5.0, "中": 3.0, "弱": 1.0}


# ════════════════════ 三、与本股关系(直接 / 间接 / 无关)════════════════════
# 用于:news_extract.与本股关系。数值=聚合时的关系权重(与 event._REL_W 对齐)。
RELATION_LEVELS = {
    "直接": "新闻**主体就是该上市公司自身**(其公告、业绩、诉讼、产品、人事等)。",
    "间接": "新闻讲该公司**所在行业 / 同板块 / 上下游**,对其有传导性影响,但主体不是它。",
    "无关": "新闻只是**顺带提及**代码/名称,主体是别的公司/事项,对本股无实质关系(防蹭词)。",
}
RELATION_WEIGHT = {"直接": 1.0, "间接": 0.5, "无关": 0.0}


# ════════════════════ 四、UGC 舆情多空(强多 / 偏多 / 中性 / 偏空 / 强空)════════════════════
# 用于:ugc_sentiment.多空。5 档回填到 [-1,1] 净情绪,替代原来让 LLM 直接给 -1~1 小数
# (数值不可信)。legacy 净情绪小数 / 旧 3 档(偏多/偏空/中性)均兼容。
STANCE_LEVELS = {
    "强多": "帖子**压倒性看多**:高频喊多、追涨、报喜,几乎无看空声音。",
    "偏多": "整体**偏多但有分歧**:看多为主,夹杂部分谨慎/看空。",
    "中性": "**多空相当或无明显倾向**:观望、讨论基本面、情绪不明。",
    "偏空": "整体**偏空但有分歧**:看空为主,夹杂部分抄底/看多。",
    "强空": "帖子**压倒性看空**:高频喊空、割肉、报忧,几乎无看多声音。",
}
STANCE_NET = {"强多": 1.0, "偏多": 0.5, "中性": 0.0, "偏空": -0.5, "强空": -1.0,
              "多": 0.5, "空": -0.5}   # 末两项:旧口径兼容


# ════════════════════ 五、财报综合研判(全文字档,补齐每档定义)════════════════════
FINANCIAL_RATING_LEVELS = {
    "优": "盈利质量高、成长确定、财务稳健,无明显红旗,横向领先。",
    "良": "基本面扎实、瑕疵可控,整体正面但非顶尖。",
    "中": "喜忧参半 / 平庸:有亮点也有拖累,缺乏明确方向。",
    "差": "基本面走弱:增长失速、盈利质量下滑或多处瑕疵。",
    "风险": "存在重大红旗(商誉/减值/现金流断裂/审计非标/关联异常等),需高度警惕。",
}
FINANCIAL_PROFIT_QUALITY_LEVELS = {
    "高": "利润以扣非主营为主、现金含量足(经营现金流覆盖净利润),含金量高。",
    "中": "扣非占比或现金含量一般,存在一定非经常性/应收占用。",
    "低": "利润严重依赖非经常损益 / 现金含量差(增收不增现),质量存疑。",
}
FINANCIAL_CONFIDENCE_LEVELS = {
    "高": "数据完整、口径一致、文本与数值相互印证,结论可靠。",
    "中": "数据基本齐全但有个别缺口 / 轻微不一致。",
    "低": "材料明显不足或前后矛盾,结论为初步、需人工复核。",
}
# 管理层语气(financial_qualitative)
TONE_LEVELS = {
    "积极": "措辞乐观、强调增长/扩张/超预期,对前景明确看好。",
    "中性": "措辞平实、就事论事,不显著偏乐观或谨慎。",
    "谨慎": "措辞保守、强调风险/不确定/压力,对前景偏防御。",
}


# ════════════════════ 渲染 helper(供 prompts.py 生成「档定义」段)════════════════════
def render_levels(levels: dict[str, str], *, sep: str = "\n") -> str:
    """把 {档名: 定义} 渲染成「  · 档名:定义」多行文本,供指令内嵌(单一真源)。"""
    return sep.join(f"  · {k}:{v}" for k, v in levels.items())


def render_inline(levels: dict[str, str]) -> str:
    """把 {档名: 定义} 渲染成「档名=定义;…」单段文本(紧凑场景)。"""
    return "；".join(f"{k}={v}" for k, v in levels.items())


# ════════════════════ 回填 helper:文字档 → 数值 ════════════════════
def _passthrough_num(v):
    """legacy 兼容:v 是数值 / 数字串 → 返回 float;否则 None(交由查表/默认)。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def strength_to_num(level, default: float = 1.0) -> float:
    """影响强度文字档 → 1~5 数值(下游 /5 归一口径不变)。

    强=5 / 中=3 / 弱=1;legacy 数值/数字串原样透传;缺失/未知 → default。
    """
    if level is None or level == "":
        return default
    s = str(level).strip()
    if s in STRENGTH_NUM:
        return STRENGTH_NUM[s]
    n = _passthrough_num(level)
    return n if n is not None else default


def strength_desc(level, default: str = "") -> str:
    """影响强度文字档 → 标准描述。legacy 数值映射到最近档描述。"""
    s = str(level).strip() if level is not None else ""
    if s in STRENGTH_LEVELS:
        return STRENGTH_LEVELS[s]
    n = _passthrough_num(level)
    if n is not None:                       # legacy 1~5:>=4 强 / >=2 中 / 其余 弱
        lvl = "强" if n >= 4 else ("中" if n >= 2 else "弱")
        return STRENGTH_LEVELS[lvl]
    return default


def direction_sign(level, default: int = 0) -> int:
    """方向文字档 → 符号(利好+1/利空-1/中性0)。未知 → default。"""
    if level is None:
        return default
    return DIRECTION_SIGN.get(str(level).strip(), default)


def direction_desc(level, default: str = "") -> str:
    return DIRECTION_LEVELS.get(str(level).strip() if level is not None else "", default)


def relation_weight(level, default: float = 0.5) -> float:
    """与本股关系文字档 → 权重(直接1.0/间接0.5/无关0.0)。未知 → default(与 event 旧口径一致)。"""
    if level is None:
        return default
    return RELATION_WEIGHT.get(str(level).strip(), default)


def stance_to_net(level, default: float = 0.0) -> float:
    """UGC 多空文字档 → 净情绪 [-1,1]。

    强多=1/偏多=0.5/中性=0/偏空=-0.5/强空=-1;legacy 净情绪小数原样透传(clamp 到 [-1,1]);
    缺失/未知 → default。
    """
    if level is None or level == "":
        return default
    s = str(level).strip()
    if s in STANCE_NET:
        return STANCE_NET[s]
    n = _passthrough_num(level)
    if n is not None:
        return max(-1.0, min(1.0, n))
    return default
