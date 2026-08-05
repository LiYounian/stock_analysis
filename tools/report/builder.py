"""报告层:渲染技术面 Markdown 报告(P1 范围)。

- 组合技术概览:技术评级排行 + 板块强弱 + 异动清单(方案2 第一层)
- 单票技术卡:均线/MACD/KDJ/RSI/量价 + 评级依据
情绪面章节留占位("P2 补"),不空造。产出到 docs/报告/。
契约见 docs/计划/P1_技术面打通.md Step 3。
"""
from __future__ import annotations

from statistics import median

import pandas as pd

from tools.config import settings, stock_pool


def _median_or_none(vals):
    nums = [v for v in vals if isinstance(v, (int, float))]
    return round(median(nums), 2) if nums else None


def _today() -> str:
    return pd.Timestamp.today().strftime("%Y%m%d")


def _name(code: str) -> str:
    s = stock_pool.get(code)
    return s.name if s else code


def _sector(code: str) -> str:
    s = stock_pool.get(code)
    return s.sector if s else "未知"


def _write(path, text: str) -> str:
    settings.ensure_dirs()
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------- 组合技术概览 ----------
def build_portfolio_tech_report(results: dict[str, dict],
                                fundamentals: dict[str, dict] | None = None,
                                announcements: dict[str, list] | None = None) -> str:
    """输入 {code: technical.compute 输出}(可选基本面);产出 docs/报告/组合技术_{date}.md。"""
    date = _today()
    valid = {c: r for c, r in results.items() if "signal" in r}
    skipped = [c for c in results if c not in valid]

    rows = sorted(valid.items(), key=lambda kv: kv[1]["signal"]["得分"], reverse=True)

    lines = [f"# 组合技术概览 · {date}", "",
             f"票池 {len(results)} 只,有效 {len(valid)},数据不足 {len(skipped)}。",
             "> 技术面为回看趋势读数,不含当日盘中;情绪面见 P2。", ""]

    # 1. 技术评级排行
    lines += ["## 一、技术评级排行(按综合得分)", "",
              "| 排名 | 名称 | 代码 | 板块 | 评级 | 得分 | MACD | KDJ | 量比 | 收盘 | 涨跌% |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, (code, r) in enumerate(rows, 1):
        s, last = r["signal"], r["last"]
        lines.append(f"| {i} | {_name(code)} | {code} | {_sector(code)} | "
                     f"{s['评级']} | {s['得分']} | {r['macd']['状态']} | {r['kdj']['状态']} | "
                     f"{r['vol']['量比']} | {last['close']} | {last['pct_chg']} |")

    # 2. 板块强弱
    sec_scores: dict[str, list[int]] = {}
    for code, r in valid.items():
        sec_scores.setdefault(_sector(code), []).append(r["signal"]["得分"])
    sec_rank = sorted(((sec, sum(v) / len(v), len(v)) for sec, v in sec_scores.items()),
                      key=lambda x: x[1], reverse=True)
    lines += ["", "## 二、板块强弱(板块内平均得分)", "",
              "| 板块 | 平均得分 | 只数 |", "|---|---|---|"]
    for sec, avg, cnt in sec_rank:
        lines.append(f"| {sec} | {avg:.1f} | {cnt} |")

    # 3. 异动清单
    golden = [c for c, r in valid.items() if r["macd"]["状态"] == "金叉"]
    oversold = [c for c, r in valid.items() if r["kdj"]["状态"] == "超卖"]
    overbought = [c for c, r in valid.items() if r["kdj"]["状态"] == "超买"]
    heavy = [c for c, r in valid.items()
             if isinstance(r["vol"]["量比"], (int, float)) and r["vol"]["量比"] > 1.5]
    lines += ["", "## 三、技术异动与超跌反弹", ""]
    for label, codes in [("MACD 金叉", golden), ("KDJ 超卖", oversold),
                         ("KDJ 超买", overbought), ("放量(量比>1.5)", heavy)]:
        names = "、".join(f"{_name(c)}({c})" for c in codes) if codes else "无"
        lines.append(f"- **{label}**:{names}")

    # 超跌反弹拐点榜(独立于趋势评级,捕捉主升启动)
    rev_rows = [(c, r["reversal"]) for c, r in valid.items()
                if r.get("reversal", {}).get("拐点标签", "无") != "无"]
    rev_rows.sort(key=lambda x: x[1]["拐点评分"], reverse=True)
    lines += ["", "### 超跌反弹拐点榜(趋势评级之外的独立维度)", ""]
    if rev_rows:
        lines += ["| 名称 | 代码 | 拐点标签 | 拐点评分 | 趋势评级 | 依据 |",
                  "|---|---|---|---|---|---|"]
        for c, rev in rev_rows:
            lines.append(f"| {_name(c)} | {c} | {rev['拐点标签']} | {rev['拐点评分']} | "
                         f"{valid[c]['signal']['评级']} | {'、'.join(rev['依据'])} |")
    else:
        lines.append("当前无超跌反弹信号。")

    if skipped:
        lines += ["", f"> 数据不足未纳入:{'、'.join(skipped)}"]

    # 4. 板块基本面对比(有基本面数据时)
    if fundamentals:
        sec_fund: dict[str, dict[str, list]] = {}
        for code, f in fundamentals.items():
            d = sec_fund.setdefault(_sector(code), {"PE": [], "ROE": [], "净利增速": []})
            d["PE"].append(f.get("PE_TTM"))
            d["ROE"].append(f.get("ROE"))
            d["净利增速"].append(f.get("净利增速"))
        lines += ["", "## 四、板块基本面对比(板块内中位数)", "",
                  "| 板块 | PE(TTM) | ROE | 净利增速% | 只数 |", "|---|---|---|---|---|"]
        # 板块顺序沿用技术强弱排名
        for sec, _avg, cnt in sec_rank:
            d = sec_fund.get(sec, {})
            lines.append(f"| {sec} | {_median_or_none(d.get('PE', []))} | "
                         f"{_median_or_none(d.get('ROE', []))} | "
                         f"{_median_or_none(d.get('净利增速', []))} | {cnt} |")

    # 5. 重要公告清单(公司行为情绪层,有公告数据时)
    idx = ["一", "二", "三", "四", "五", "六"]
    n = 3 + (1 if fundamentals else 0)
    if announcements:
        important = {"业绩预告", "业绩快报", "增持", "减持", "回购", "合同订单",
                     "诉讼仲裁", "权益变动", "股权激励", "再融资"}
        rows_a = []
        for code, items in announcements.items():
            for it in items:
                if it["type"] in important:
                    rows_a.append((it["date"], code, it))
        rows_a.sort(key=lambda x: x[0], reverse=True)
        lines += ["", f"## {idx[n]}、重要公告清单(公司行为)", ""]
        if rows_a:
            lines += ["| 日期 | 名称 | 代码 | 类型 | 方向 | 标题 |",
                      "|---|---|---|---|---|---|"]
            for date_a, code, it in rows_a[:25]:
                lines.append(f"| {date_a} | {_name(code)} | {code} | {it['type']} | "
                             f"{it['impact']} | {it['title'][:30]} |")
        else:
            lines.append("近期无重要公告。")
        n += 1

    lines += ["", "---", f"## {idx[n]}、情绪面",
              "*(P2-C 补:政策 / 舆情;公司行为见上「重要公告」)*", ""]
    return _write(settings.REPORT_DIR / f"组合技术_{date}.md", "\n".join(lines))


# ---------- 单票技术卡 ----------
def _fundamental_section(f: dict | None) -> list[str]:
    if not f:
        return ["## 基本面", "*(未采集)*", ""]
    def g(k):
        return f.get(k) if f.get(k) is not None else "-"
    return [
        "## 基本面", f"报告期 {f.get('报告期', '-')}",
        f"- 营收 {g('营收')} / 净利 {g('净利')}(增速 营收{g('营收增速')}% 净利{g('净利增速')}%)",
        f"- ROE {g('ROE')} / 毛利率 {g('毛利率')} / 净利率 {g('净利率')} / 负债率 {g('负债率')}",
        f"- 估值:PE(TTM) {g('PE_TTM')} / PB {g('PB')} / 总市值 {g('总市值')}亿", "",
    ]


def _announcement_section(items: list | None) -> list[str]:
    if items is None:
        return ["## 近期公告", "*(未采集)*", ""]
    if not items:
        return ["## 近期公告", "近期无公告。", ""]
    out = ["## 近期公告", "", "| 日期 | 类型 | 方向 | 标题 |", "|---|---|---|---|"]
    for it in items[:10]:
        out.append(f"| {it['date']} | {it['type']} | {it['impact']} | {it['title'][:32]} |")
    return out + [""]


def build_stock_tech_report(code: str, result: dict, fundamental: dict | None = None,
                            announcements: list | None = None) -> str:
    """输入单票 compute 输出(可选基本面/公告);产出 docs/报告/个股_{code}_{date}.md。"""
    date = _today()
    name = _name(code)
    if "signal" not in result:
        text = f"# {name}({code}) 技术卡 · {date}\n\n数据不足,无法分析。\n"
        return _write(settings.REPORT_DIR / f"个股_{code}_{date}.md", text)

    s, ma, md, kd, rs, vol, last = (result["signal"], result["ma"], result["macd"],
                                    result["kdj"], result["rsi"], result["vol"], result["last"])
    lines = [
        f"# {name}({code}) 技术卡 · {date}", "",
        f"**综合评级:{s['评级']}(得分 {s['得分']})** · 板块 {_sector(code)} · "
        f"收盘 {last['close']} · 涨跌 {last['pct_chg']}%", "",
        "## 均线",
        f"- MA5/10/20/60:{ma['ma5']} / {ma['ma10']} / {ma['ma20']} / {ma['ma60']}",
        f"- 排列:**{ma['排列']}**", "",
        "## MACD",
        f"- DIF {md['dif']} / DEA {md['dea']} / 柱 {md['macd']} → **{md['状态']}**", "",
        "## KDJ",
        f"- K {kd['k']} / D {kd['d']} / J {kd['j']} → **{kd['状态']}**", "",
        "## RSI",
        f"- RSI6/12/24:{rs['rsi6']} / {rs['rsi12']} / {rs['rsi24']}", "",
        "## 量价",
        f"- 量比 {vol['量比']} → **{vol['状态']}**", "",
    ]
    rev = result.get("reversal")
    if rev:
        lines += ["## 拐点信号(独立于趋势)",
                  f"- **{rev['拐点标签']}**(拐点评分 {rev['拐点评分']})"
                  + (f":{'、'.join(rev['依据'])}" if rev['依据'] else ""), ""]
    lines += ["## 评级依据"]
    lines += [f"- {r}" for r in s["依据"]] or ["- (无)"]
    lines += ["", "---"] + _fundamental_section(fundamental)
    lines += ["---"] + _announcement_section(announcements)
    lines += ["---", "## 情绪面", "*(P2-C 补:政策 / 舆情;公司行为见上「近期公告」)*", ""]
    return _write(settings.REPORT_DIR / f"个股_{code}_{date}.md", "\n".join(lines))
