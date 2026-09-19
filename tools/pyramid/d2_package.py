"""D2-2 · 金字塔选股 决策包组装（骨架 + 全浓缩块 + 全板块概览）。

产出"决策包"文本：喂给 LLM 合成层（Claude Code / DeepSeek / 千问）做受限调整，
或统筹直接读它产出金字塔选股。不含 LLM 调用，纯组装。

板块口径（用户 09-18 定）：**全部板块都分析，重点标 3~5 个，浓缩不遗漏**——
全 29 板块每行一个（冷热/拥挤/均涨/涨停/上涨占比），另深挖重点 3~5（含消息驱动）。
"""
from __future__ import annotations

from typing import Optional
import json
import os

from tools.pyramid.指标词表 import render_词表, render_消息面词表, render_大盘词表  # v2：统一指标词表·喂 prompt 开头一次


def _load(root: Optional[str], as_of: str, fname: str):
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = os.path.join(root or base, "data", "analysis", as_of, fname)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _txt(v) -> str:
    """A6/D4 铁律：任何值转成人读文本，禁止裸 dict/json 进 prompt。

    dict → `k=v k=v`（无花括号）；list/tuple → 顿号连接（空→"无"）；标量 → str。递归。
    """
    if v is None:
        return "无"
    if isinstance(v, dict):
        return " ".join(f"{k}={_txt(x)}" for k, x in v.items()) or "无"
    if isinstance(v, (list, tuple)):
        return "、".join(_txt(x) for x in v) if v else "无"
    return str(v)


def _scalar(v, key: Optional[str] = None):
    """从可能是 dict 的字段里取标量：优先取同名内层键，否则整体文字化。"""
    if isinstance(v, dict):
        if key and key in v and not isinstance(v[key], (dict, list)):
            return v[key]
        return _txt(v)
    return v


_排除冷热 = {"过热", "拐点"}  # A6：★ 排除 过热A / 拐点A

# ── 四面板渲染顺序（面attr → 序号/显示名）；卡头/经验单列，不在四面板内 ──
# 面attr 与 registry.四面枚举 对齐（消息情绪面 显示简称"消息面"）。
_面板序 = [
    ("一", "基本面", "基本面"),
    ("二", "技术面", "技术面"),
    ("三", "资金面", "资金面"),
    ("四", "消息情绪面", "消息面"),
]

# ── 消息面段常量 ──────────────────────────────────────────────
# 国际/宏观 启发式关键词：数据无 per-event 国际 flag，对『事件』文本命中即归国际/宏观。
# 策展词表·聚焦 运价/地缘/汇率/关税/货币/贸易/海外订单；**刻意不含裸『国际』二字**
# （实测『APEC国际研讨会』这类社交活动会误命中）。诚实标注启发式、非精确分类。
_宏观国际关键词 = (
    "BDI", "欧线", "集运", "干散货", "油运", "航线", "运价", "运费", "VLCC", "TD3C",
    "原油", "石油", "OPEC", "天然气", "关税", "加息", "降息", "美联储", "美债",
    "汇率", "人民币", "美元", "地缘", "战争", "中东", "出口", "进口", "长单", "海外订单",
)
_影响程度序 = {"大": 0, "中": 1, "小": 2}  # 事件排序：影响大者靠前
_事件上限默认 = 8  # 统筹裁定：与体检卡 G3 默认一致，逐事件渲染上限（照 G3 可配）


def _load_names(root: Optional[str] = None) -> dict:
    """code→股票名 映射（config/code_name.json 随代码走·非 data-root）。缺失回退空表。"""
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for d in (base, root):
        if not d:
            continue
        p = os.path.join(d, "config", "code_name.json")
        try:
            with open(p, "r", encoding="utf-8") as f:
                m = json.load(f)
            if isinstance(m, dict) and m:
                return m
        except Exception:
            continue
    return {}


def _stock_name(code: str, names: dict, root: Optional[str], as_of: str) -> str:
    """取股票名：code_name.json 优先 → per-stock meta.name（非 code 占位时）→ code 兜底。"""
    nm = names.get(code)
    if nm and str(nm).strip() and str(nm).strip() != str(code):
        return str(nm).strip()
    j = _load(root, as_of, f"{code}.json")
    if isinstance(j, dict) and isinstance(j.get("meta"), dict):
        mn = j["meta"].get("name")
        if mn and str(mn).strip() and str(mn).strip() != str(code):
            return str(mn).strip()
    return str(code)


def market_overview(as_of: str, root: Optional[str] = None, n_focus: int = 5) -> dict:
    """市场定调 + 全板块概览（全分析·重点 n_focus·浓缩不遗漏）。

    A6：★重点板块改按 focus_score 降序挑，且排除"过热+拥挤A / 拐点+拥挤A"
    （按 regime 冷热标签/拥挤档判，与全板块渲染显示的标签一致）。
    """
    focus = _load(root, as_of, "sector_focus.json") or {}
    regime = _load(root, as_of, "sector_regime.json") or {}
    boards = regime.get("板块") or []
    # 全板块按截面动量分位降序（强主线在前）
    def mom(b):
        return b.get("动量_截面分位") or 0.0
    boards_sorted = sorted(boards, key=mom, reverse=True)
    # regime 标签映射（板块→冷热标签/拥挤档），★排除口径以它为准
    reg_label = {b.get("板块"): (b.get("冷热标签"), b.get("拥挤档")) for b in boards}
    avoid = {(x.get("板块") if isinstance(x, dict) else x)
             for x in (focus.get("规避板块池") or [])}

    def _过热拥挤(name: str) -> bool:
        冷热, 拥挤 = reg_label.get(name, (None, None))
        return 拥挤 == "A" and 冷热 in _排除冷热

    # A6：★ 从重点板块池按 focus_score 降序，排除 规避 与 过热A/拐点A
    key_entries = [x for x in (focus.get("重点板块池") or []) if isinstance(x, dict)]
    key_entries.sort(key=lambda x: (x.get("focus_score") or 0.0), reverse=True)
    重点 = [x.get("板块") for x in key_entries
            if x.get("板块") not in avoid and not _过热拥挤(x.get("板块"))][:n_focus]
    if not 重点:  # 兜底：无 focus 池时退回动量截面（同样排除 过热A/拐点A 与规避）
        重点 = [b.get("板块") for b in boards_sorted
                if b.get("板块") not in avoid and not _过热拥挤(b.get("板块"))][:n_focus]

    # 描述性输入侧：风险偏好 dict 里已带上游产出的描述句「依据」+ 净广度/涨停跌停（§8 用户明说）
    _rp = focus.get("风险偏好") if isinstance(focus.get("风险偏好"), dict) else {}
    return {
        "as_of": as_of,
        # A6 文字化：风险偏好在 focus.json 里是 dict，取内层标量避免裸 dict 进 prompt
        "风险偏好": _scalar(focus.get("风险偏好"), "风险偏好"),
        "广度档": _scalar(focus.get("风险偏好"), "广度档") if isinstance(focus.get("风险偏好"), dict) else None,
        # 描述性源：依据=上游产出的整句（拼装层原样 surface·不编）；净广度/涨停跌停供多空描述
        "净广度": _rp.get("净广度"),
        "依据": _rp.get("依据"),
        "涨停": _rp.get("涨停"),
        "跌停": _rp.get("跌停"),
        "宏观情景": _scalar(focus.get("宏观情景"), "宏观情景"),
        "宏观净方向": _scalar(focus.get("宏观净方向"), "宏观净方向"),
        "消息驱动": focus.get("消息驱动"),  # 原始保留（render 不直接印，需要时经 _txt）
        "规避板块池": [(x.get("板块") if isinstance(x, dict) else x)
                     for x in (focus.get("规避板块池") or [])],
        "全板块": boards_sorted,
        "重点板块": 重点,
        "重点口径": "focus_score↓·排除规避与过热A/拐点A",
    }


def render_boards(ov: dict) -> str:
    """全板块概览渲染：每行一个板块，浓缩不遗漏。"""
    lines = []
    key = set(ov.get("重点板块") or [])
    for b in ov.get("全板块") or []:
        name = b.get("板块")
        mark = "★" if name in key else "·"
        lines.append(
            f"{mark}{name}: {b.get('冷热标签')}/拥挤{b.get('拥挤档')} "
            f"均涨{b.get('板块均涨幅')}% 涨停{int(b.get('涨停数') or 0)} "
            f"上涨占比{round((b.get('上涨家数占比') or 0)*100)}% "
            f"动量截面{round((b.get('动量_截面分位') or 0),2)}"
        )
    return "\n".join(lines)


def _is_国际(事件: str) -> bool:
    """启发式：事件文本命中宏观/国际关键词即归国际/宏观（非精确分类）。"""
    if not isinstance(事件, str):
        return False
    return any(kw in 事件 for kw in _宏观国际关键词)


def _事件在窗(e: dict, as_of: Optional[str]) -> bool:
    """防未来 as_of 守卫：丢弃 时间 > as_of 的事件（ISO 日期串直接可比）。
    时间缺失或 as_of 缺失时保守保留（不因缺元数据静默丢真实事件）。"""
    if not as_of:
        return True
    t = e.get("时间") if isinstance(e, dict) else None
    if not t:
        return True
    return str(t) <= str(as_of)


def _事件排序键(e: dict):
    """排序：影响大>中>小，非中性优先，时间新在前（时间倒序用负字典序不便，改元组）。"""
    影响 = _影响程度序.get(e.get("影响程度"), 3)
    非中性 = 0 if e.get("方向") in ("利好", "利空") else 1
    return (影响, 非中性, _neg_date(e.get("时间")))


def _neg_date(t) -> str:
    """让时间倒序参与升序排序：新日期应排前，用『取反』字符串。
    ISO 日期逐字符对 9 取补，得到新→旧的升序键。"""
    s = str(t or "")
    return "".join(chr(ord("9") - (ord(c) - ord("0"))) if c.isdigit() else c for c in s)


def _事件行(e: dict) -> str:
    """逐事件行：只 surface 原样字段（方向/可信度/影响程度/执行度/来源），render 不编研判。"""
    时间 = _txt(e.get("时间"))
    事件 = _txt(e.get("事件"))
    tags = [
        f"方向{_txt(e.get('方向'))}", _txt(e.get("可信度")),
        f"影响{_txt(e.get('影响程度'))}", f"执行{_txt(e.get('执行度'))}",
        _txt(e.get("来源")),
    ]
    if _is_国际(e.get("事件") or ""):
        tags.append("🌐国际/宏观")
    return f"- {时间}｜{事件}〔{'·'.join(tags)}〕"


def _候选文本(候选: list, 带联动: bool = False) -> str:
    """龙头/跟涨候选 → 人读文本（A6：禁裸 dict 进 prompt）。"""
    if not isinstance(候选, list) or not 候选:
        return "—"
    parts = []
    for x in 候选:
        if not isinstance(x, dict):
            parts.append(_txt(x)); continue
        seg = f"{_txt(x.get('name'))}{_txt(x.get('code'))}"
        if x.get("已动"):
            seg += "(已动)"
        if 带联动 and x.get("联动依据"):
            seg += f"[{_txt(x.get('联动依据'))}]"
        parts.append(seg)
    return "、".join(parts)


# ── 大盘定调段（读 market_forecast.json·结构化大盘卡）────────────────────
_大盘horizons显示 = [("1", "1日"), ("5", "5日")]


def _mf_有效(mf, as_of) -> bool:
    """market_forecast 是否可用作结构化大盘卡主读数。
    需为 dict、含 targets，且 mf.as_of ≤ 包 as_of（防未来：晚于 as_of 的整份预测丢弃）。"""
    if not isinstance(mf, dict) or not isinstance(mf.get("targets"), dict) or not mf.get("targets"):
        return False
    mfa = mf.get("as_of")
    if as_of and mfa and str(mfa) > str(as_of):
        return False
    return True


def _num3(x) -> str:
    """概率/比例类保留三位小数（0.5029→'0.503'）；非数字回退文字化。"""
    return f"{x:.3f}" if isinstance(x, (int, float)) else _txt(x)


def _int文(x) -> str:
    """计数类去 .0（2575.0→'2575'）；非数字回退文字化。"""
    return str(int(x)) if isinstance(x, (int, float)) else _txt(x)


def _pct1文(x) -> str:
    """0~1 比例 → 一位小数百分数（0.2804→'28.0%'）；非数字回退文字化。"""
    return f"{x*100:.1f}%" if isinstance(x, (int, float)) else _txt(x)


def _亿(x) -> str:
    """大额金额 → 亿元整数（1333197305694→'13332亿'）；非数字回退文字化。"""
    return f"{x/1e8:.0f}亿" if isinstance(x, (int, float)) else _txt(x)


def _多空(net) -> str:
    """净广度→多空描述（阈值 ±0.1，与现有市场定调薄读数口径一致）。"""
    if not isinstance(net, (int, float)):
        return ""
    return "多头占优" if net > 0.1 else ("空头占优" if net < -0.1 else "多空均衡")


def _方向行(key: str, tgt: dict, 角色: str, as_of) -> Optional[str]:
    """单基准方向行：各 horizon 的 p_up + 方向档 + direction（数据直取·render 不编）。
    防未来：target.as_of 晚于包 as_of → 返回该基准丢弃标注行（逐 target 各自守卫）。"""
    name = _txt(tgt.get("name"))
    t_asof = tgt.get("as_of")
    if as_of and t_asof and str(t_asof) > str(as_of):
        return f"- {角色} {key}({name})：as_of={_txt(t_asof)} 晚于 {_txt(as_of)}·防未来丢弃"
    hs = tgt.get("horizons") or {}
    segs = []
    for h, disp in _大盘horizons显示:
        hz = hs.get(h) or {}
        if not hz:
            continue
        bucket = _txt(hz.get("prob_bucket") or hz.get("bucket"))
        segs.append(f"{disp} p_up={_num3(hz.get('p_up'))}·{bucket}({_txt(hz.get('direction'))})")
    if not segs:
        return None
    适用 = _txt(tgt.get("适用范围"))
    return f"- {角色} {key}({name}·{适用})：" + "；".join(segs)


def render_大盘定调(mf, as_of: Optional[str] = None) -> str:
    """结构化大盘定调卡（读 market_forecast.json）：方向面/广度情绪资金面/板块强弱衔接，
    段尾**原样 surface mf.notes 效力诚实标注**（render 不编研判·勿精简）。

    口径贯通不许编：方向 p_up/方向档/direction、分歧标记类型、广度/情绪/两融快照
    全数据直取；含义/口径句用上游现成说明(选股用β基准.说明/分歧标记.说明/notes)原样。
    缺 market_forecast 或防未来失效 → 降级中性提示（回退 sector_focus 薄读数由 render_package 兜底）。
    """
    as_of = as_of or (mf.get("as_of") if isinstance(mf, dict) else None)
    if not _mf_有效(mf, as_of):
        why = "缺 market_forecast"
        if isinstance(mf, dict) and mf.get("as_of") and as_of and str(mf.get("as_of")) > str(as_of):
            why = f"market_forecast.as_of={_txt(mf.get('as_of'))} 晚于 {_txt(as_of)}·防未来丢弃"
        return f"（{why}·降级中性：无结构化大盘方向/广度读数，回退 sector_focus 薄读数）"

    out = [f"\n### 大盘定调 (as_of={_txt(as_of)}·{_txt(mf.get('schema'))})"]
    out.append(render_大盘词表())

    # 方向面：proxy(个股β基准·默认) 在前、hs300(权重β背景) 在后
    tgts = mf.get("targets") or {}
    β = mf.get("选股用β基准") or {}
    默认, 背景 = β.get("默认"), β.get("背景")
    out.append("\n#### 方向面(上行概率分位·方向口径非涨跌幅·勿读成涨跌幅/大幅)")
    序: list[tuple] = []
    if 默认 and 默认 in tgts:
        序.append((默认, "个股β基准"))
    if 背景 and 背景 in tgts:
        序.append((背景, "权重β背景"))
    for k in tgts:  # 兜底：β基准未声明的 target 也渲（不静默漏）
        if k not in [x[0] for x in 序]:
            序.append((k, ""))
    for k, 角色 in 序:
        行 = _方向行(k, tgts.get(k) or {}, 角色, as_of)
        if 行:
            out.append(行)
    if β.get("说明"):
        out.append(f"  β基准口径：{_txt(β.get('说明'))}")

    # 分歧标记（触发维度逐条·hs300 vs proxy 方向背离）
    div = mf.get("分歧标记") or {}
    if div.get("触发"):
        for h, d in (div.get("维度") or {}).items():
            if not isinstance(d, dict) or not d.get("触发"):
                continue
            out.append(
                f"- ⚑分歧标记[触发·{_txt(d.get('类型'))}]：{h}日 "
                f"hs300 {_txt(d.get('hs300_direction'))}(p{_num3(d.get('hs300_p_up'))}) vs "
                f"proxy {_txt(d.get('proxy_direction'))}(p{_num3(d.get('proxy_p_up'))})·"
                f"方向档背离={_txt(d.get('方向档背离'))}"
            )
        if div.get("说明"):
            out.append(f"  分歧口径：{_txt(div.get('说明'))}")

    # 维度贡献（默认基准·5日·数据直取，佐证效力标注：资金流权重=0、消息面≈0）
    默认tgt = tgts.get(默认) or {}
    fc = ((默认tgt.get("horizons") or {}).get("5") or {}).get("factor_contrib")
    if isinstance(fc, dict):
        out.append(
            f"- 维度贡献(数据直取·{_txt(默认)}·5日)："
            + "/".join(f"{k}{_num3(v)}" for k, v in fc.items())
            + "（佐证效力标注：资金流权重≈0、消息面≈0，真正起作用只技术+广度）"
        )

    # 广度情绪资金面
    out.append("\n#### 广度情绪资金面")
    bs = mf.get("breadth_snapshot") or {}
    if bs:
        na = bs.get("net_adv")
        多空 = _多空(na)
        out.append(
            f"- 广度 breadth：涨{_int文(bs.get('adv'))}/跌{_int文(bs.get('dec'))}·"
            f"涨停{_int文(bs.get('limit_up'))}/跌停{_int文(bs.get('limit_down'))}·"
            f"净广度{_num3(na)}{('('+多空+')') if 多空 else ''}·"
            f"站上MA20 {_pct1文(bs.get('above_ma20_ratio'))}(破MA20 {_pct1文(bs.get('below_ma20_ratio'))})·"
            f"中位涨幅{_txt(bs.get('median_pct'))}%"
        )
    ss = mf.get("sentiment_snapshot") or {}
    if ss:
        out.append(
            f"- 情绪 sentiment：多空net{_int文(ss.get('se_net'))}·多空比{_txt(ss.get('se_ratio'))}·"
            f"看多{_int文(ss.get('se_bull'))}/看空{_int文(ss.get('se_bear'))}(样本{_int文(ss.get('se_n'))})"
        )
    ff = mf.get("fundflow_snapshot") or {}
    if ff:
        out.append(
            f"- 资金 fundflow(两融·盘后滞后)：融资余额{_亿(ff.get('融资余额'))}·"
            f"融资买入{_亿(ff.get('融资买入额'))}·融资融券余额{_亿(ff.get('融资融券余额'))}·"
            f"截至{_txt(ff.get('margin_date'))}"
        )
        if ff.get("note"):
            out.append(f"  资金口径：{_txt(ff.get('note'))}")

    # 板块强弱衔接（复用全板块概览 + 消息面·不重复）
    out.append("\n#### 板块强弱(衔接·不重复)")
    out.append(
        "- 板块冷热/拥挤/动量分位见下方「全板块概览」；"
        "消息驱动龙头/催化/谁受什么新闻震动见「市场·国际·板块消息面」段。"
    )

    # 效力诚实标注（原样 surface notes·render 绝不编/勿精简）
    out.append("\n#### ⚠️效力诚实标注(上游 market_forecast.notes 原句·必读·勿精简/勿删)")
    out.append(_txt(mf.get("notes")))
    return "\n".join(out)


def render_消息面(ov: dict, as_of: Optional[str] = None, 上限: int = _事件上限默认) -> str:
    """市场·国际·板块消息面段：消息面词表 + 板块消息卡 + 国际/宏观汇总。

    口径贯通不许编：方向/可信度/影响程度/执行度/来源 原样取自消息驱动块；
    含义/影响用上游现成研判句（依据/持续性/可靠性综述）+ 龙头/跟涨候选个股 原样组装。
    强弱=强 板块按上限深挖全事件；强弱=中 板块只渲 影响大/中 事件（精简）。
    防未来：丢弃 时间 > as_of 的事件。
    """
    md = ov.get("消息驱动") if isinstance(ov, dict) else None
    as_of = as_of or (md.get("as_of") if isinstance(md, dict) else None) or ov.get("as_of")
    out = [f"\n## 市场·国际·板块消息面 (as_of={_txt(as_of)})"]
    out.append(render_消息面词表())

    if not isinstance(md, dict) or not md.get("利好板块"):
        out.append("（今日无消息驱动数据落盘·待补；不编造）")
        return "\n".join(out)

    boards = [b for b in (md.get("利好板块") or []) if isinstance(b, dict)]
    # 板块排序：强弱(强>中>弱)、事件多者靠前
    强弱序 = {"强": 0, "中": 1, "弱": 2}
    boards.sort(key=lambda b: (强弱序.get(b.get("强弱"), 3),
                               -len(b.get("关键事件") or [])))
    国际汇总: list[tuple] = []  # (board, tag, 强弱, [国际事件短句])

    for b in boards:
        board = _txt(b.get("board"))
        tag, 强弱 = _txt(b.get("tag")), _txt(b.get("强弱"))
        events = [e for e in (b.get("关键事件") or []) if isinstance(e, dict)]
        总数 = len(events)
        # 防未来守卫
        events = [e for e in events if _事件在窗(e, as_of)]
        丢弃 = 总数 - len(events)
        # 强弱=中 精简：只留 影响大/中
        is_强 = b.get("强弱") == "强"
        if not is_强:
            events = [e for e in events if e.get("影响程度") in ("大", "中")]
        筛后 = len(events)
        events.sort(key=_事件排序键)
        取 = events[:上限]
        余 = 筛后 - len(取)

        cnt = f"关键事件{总数}条"
        if 丢弃:
            cnt += f"·防未来丢弃{丢弃}"
        if not is_强:
            cnt += f"·中板块仅取影响大/中{筛后}"
        cnt += f"·取{len(取)}"
        if 余 > 0:
            cnt += f"·余{余}略"
        out.append(f"\n### {board} —— {tag} · {强弱} ({cnt})")
        for e in 取:
            out.append(_事件行(e))
        # 含义/影响：全部上游现成研判句 + 候选个股，原样组装（render 不编）
        if b.get("依据"):
            out.append(f"  含义(上游依据)：{_txt(b.get('依据'))}")
        if b.get("持续性"):
            out.append(f"  持续性：{_txt(b.get('持续性'))}")
        龙头 = _候选文本(b.get("龙头候选"))
        跟涨 = _候选文本(b.get("跟涨候选"), 带联动=True)
        out.append(f"  影响(个股指向)：龙头候选 {龙头}；跟涨候选 {跟涨}")
        if b.get("可靠性综述"):
            out.append(f"  可靠性：{_txt(b.get('可靠性综述'))}")
        # 收集国际/宏观事件（用在窗事件·同守卫/中筛口径，汇总求全不受逐卡上限裁剪）
        国际事件 = [ _txt(e.get("事件")) for e in events if _is_国际(e.get("事件") or "") ]
        if 国际事件:
            国际汇总.append((board, tag, 强弱, 国际事件[:5]))

    # 国际/宏观事件汇总
    out.append(f"\n## 国际/宏观事件汇总(启发式抽取·as_of={_txt(as_of)})")
    if 国际汇总:
        for board, tag, 强弱, evs in 国际汇总:
            摘 = "·".join(s[:32] for s in evs)
            out.append(f"- {board}({tag}{强弱})：{摘}")
        out.append("影响：让 LLM 一眼看到当日国际/宏观面系统性利好/利空哪些板块。")
    else:
        out.append("（当日各板块关键事件未命中国际/宏观关键词）")
    return "\n".join(out)


def build_package(as_of: str, root: Optional[str] = None, top_n: int = 15,
                  scan_kline: bool = True, n_focus: int = 5) -> dict:
    """决策包：市场定调 + 全板块概览 + 骨架 top_n（每票全部工具浓缩块）。"""
    from tools.pyramid import d2_compose as C
    from tools.pyramid import registry
    import tools.pyramid.tools  # noqa: F401

    skel = C.build_skeleton(as_of, root=root, scan_kline=scan_kline)
    ov = market_overview(as_of, root=root, n_focus=n_focus)
    # 贯通 market_forecast：结构化大盘卡的真实数据源（缺/防未来失效时降级中性，见 render_大盘定调）
    ov["market_forecast"] = _load(root, as_of, "market_forecast.json")

    # shared_pool 不逐票重扫（每次 scan_kline 要 14s）；卡头用骨架已存的来源标签直接渲染。
    # 四面重排：各工具块按其 .面 分组，d2_package 只按面板顺序排版（拼装层不写口径）。
    # 按四面分组排列（面板分组仍由各工具 .面 决定；此处顺序决定同面板内块序）。
    tool_names = [
        # 基本面
        "financial_redflag", "insider_reduction", "valuation", "growth_quality",
        # 技术面
        "price_volume", "gate", "entry_price", "technical_detail",
        # 资金面
        "unlock_risk", "fund_flow",
        # 消息情绪面
        "sector_context", "fake_good_news", "stock_sentiment",
        # 经验纪律（跨面·尾块）
        "experience_rules",
    ]
    tools = {t: registry.get(t) for t in tool_names}
    names = _load_names(root)

    top = C.select_top(skel["排序"], top_n)  # A10：同分不硬切，整桶纳入
    cards = []
    for row in top:
        code = row.code
        面块: dict[str, list] = {}  # 面attr → [工具浓缩块, ...]
        for t in tool_names:
            try:
                r = tools[t].run(as_of, code, root=root)
                面 = getattr(r, "面", None) or getattr(tools[t], "面", None) or "基本面"
                面块.setdefault(面, []).append(r.to_prompt())
            except Exception as e:
                # 工具异常也归其声明面，保证面板不整块消失、可诊断
                面 = getattr(tools[t], "面", None) or "基本面"
                面块.setdefault(面, []).append(f"【{t}】ERR {e}")
        cards.append({
            "code": code,
            "名称": _stock_name(code, names, root, as_of),
            "as_of": as_of,
            "骨架分": row.骨架分,
            "子分": row.子分,
            "来源标签": row.来源标签,
            "面块": 面块,
        })
    return {
        "as_of": as_of,
        "市场定调": ov,
        "骨架": skel,
        "候选卡片": cards,
        "top_n": top_n,
    }


def _sub_txt(subs) -> str:
    """子分 dict → 人读文本（A6：禁止裸 dict 进 prompt）。"""
    if not isinstance(subs, dict):
        return _txt(subs)
    return " ".join(f"{k} {v}" for k, v in subs.items())


def render_package(pkg: dict) -> str:
    """决策包 → 文本（供 LLM prompt 或统筹阅读）。A6：全程文字化，无裸 dict。"""
    ov = pkg.get("市场定调") or {}
    out = []
    out.append(f"# 金字塔决策包 · as_of={pkg.get('as_of')}")
    # v2：统一指标词表——共性定义/全档位区间喂一次，下方个股卡只给「值+档+本股影响」不重复
    out.append("\n" + render_词表())
    # §8 市场定调：market_forecast 可用→结构化大盘卡为主读数（降级中性风险偏好句让位）；
    # 缺/防未来失效→回退 sector_focus 薄读数兜底（不静默）。板块轮动(sector_focus)始终保留。
    out.append("\n## 市场定调")
    _asof = ov.get("as_of") or pkg.get("as_of")
    mf = ov.get("market_forecast")
    out.append(render_大盘定调(mf, as_of=_asof))
    if not _mf_有效(mf, _asof):
        # 兜底：market_forecast 缺失/防未来失效 → 保留现有 sector_focus 薄读数（原描述性主句）
        广度 = ov.get("广度档")
        净广度 = ov.get("净广度")
        s = f"当前市场风险偏好{_txt(ov.get('风险偏好'))}"
        if 广度:
            s += f"（广度档{_txt(广度)}"
            if isinstance(净广度, (int, float)):
                方向 = "多头占优" if 净广度 > 0.1 else ("空头占优" if 净广度 < -0.1 else "多空均衡")
                s += f"·净广度{净广度:+.3f}→{方向}"
            zt, dt = ov.get("涨停"), ov.get("跌停")
            if zt is not None or dt is not None:
                s += f"·涨停{_txt(zt)}/跌停{_txt(dt)}"
            s += "）"
        s += f"；宏观情景{_txt(ov.get('宏观情景'))}、宏观净方向{_txt(ov.get('宏观净方向'))}。"
        out.append(s)
        if ov.get("依据"):
            out.append(f"定调依据：{_txt(ov.get('依据'))}")  # 上游产出整句·拼装层原样 surface
    out.append(
        f"板块轮动：重点主线★ {_txt(ov.get('重点板块'))}；规避 {_txt(ov.get('规避板块池'))}"
        + (f"（{ov.get('重点口径')}）" if ov.get("重点口径") else "")
    )
    # 市场·国际·板块消息面（消息驱动逐事件研判显性化；市场定调后、全板块概览前）
    out.append(render_消息面(ov, as_of=ov.get("as_of") or pkg.get("as_of")))
    out.append("\n## 全板块概览(全分析·★重点·浓缩不遗漏)")
    out.append(render_boards(ov))
    skel = pkg.get("骨架") or {}
    # A11：口径三分拆·自洽（参与 = 计分 + 数据缺；计分 = 排序 + 否决）
    池 = skel.get("池规模")
    参与 = skel.get("参与票数", 池)
    计分 = skel.get("计分票数", len(skel.get("排序") or []) + len(skel.get("排雷否决") or []))
    排序票 = skel.get("排序票数", len(skel.get("排序") or []))
    否决票 = skel.get("否决票数", len(skel.get("排雷否决") or []))
    缺 = skel.get("数据缺失票数", max(参与 - 计分, 0) if isinstance(参与, int) else 0)
    out.append(
        f"\n## 骨架排序(池{池}·参与{参与}=计分{计分}+数据缺{缺}；"
        f"计分{计分}=排序{排序票}+否决{否决票}·权重 {_txt(skel.get('权重'))})"
    )
    if skel.get("排序键"):
        out.append(f"排序键：{skel['排序键']}（同分不跨界硬切）")
    cards = pkg.get("候选卡片") or []
    out.append(f"\n## 候选 top{pkg.get('top_n')}(实入{len(cards)}张·四面结构·每票 卡头+基本面/技术面/资金面/消息面+经验)")
    for c in cards:
        面块 = c.get("面块") or {}
        # 卡头·元信息（股票名 + code + as_of + 骨架分/子分/来源）
        out.append(
            f"\n### {_txt(c.get('名称'))}（{c.get('code')}） as_of={c.get('as_of')} "
            f"骨架分={c.get('骨架分')} 子分[{_sub_txt(c.get('子分'))}] 来源={_txt(c.get('来源标签'))}"
        )
        labels = c.get("来源标签") or []
        out.append(
            f"【卡头·元信息】命中来源: {'/'.join(labels) if labels else '无'}"
            f"（{len(labels)}/4来源·≥2=交叉共识）"
        )
        # 四面板固定顺序·全展开（空面板显式标待填充，不静默消失）
        for 序, 面attr, 显示 in _面板序:
            out.append(f"【{序}·{显示}】")
            blocks = 面块.get(面attr) or []
            if blocks:
                out.extend(blocks)
            else:
                out.append("（本面暂无工具·待 Wave2 填充）")
        # 经验纪律尾块（跨面）
        exp = 面块.get("经验") or []
        if exp:
            out.append("【经验纪律·跨面】")
            out.extend(exp)
    return "\n".join(out)


def save_package(pkg: dict, txt: Optional[str] = None, root: Optional[str] = None,
                 label: Optional[str] = None) -> str:
    """A12：决策包 md 落盘到 data/analysis/<as_of>/金字塔决策包_<label>.md，返回路径。"""
    if txt is None:
        txt = render_package(pkg)
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    as_of = pkg.get("as_of") or "unknown"
    d = os.path.join(root or base, "data", "analysis", as_of)
    os.makedirs(d, exist_ok=True)
    lab = label or f"top{pkg.get('top_n')}"
    p = os.path.join(d, f"金字塔决策包_{lab}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(txt)
    return p


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", required=True)
    p.add_argument("--data-root", default=None)
    p.add_argument("--top-n", type=int, default=15)
    p.add_argument("--out", default=None, help="显式输出路径（不给则默认落 data/analysis/<as_of>/）")
    p.add_argument("--no-save", action="store_true", help="不落盘（仅打印）")
    a = p.parse_args()
    pkg = build_package(a.as_of, root=a.data_root, top_n=a.top_n)
    txt = render_package(pkg)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"决策包写入 {a.out}（{len(txt)} 字）")
    elif a.no_save:
        print(txt)
    else:
        # A12：默认落盘过程文件（决策包 md）
        path = save_package(pkg, txt, root=a.data_root)
        print(f"决策包落盘 {path}（{len(txt)} 字）")
