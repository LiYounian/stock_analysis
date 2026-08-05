"""报告层:渲染技术面 Markdown 报告(P1 范围)。

- 组合技术概览:技术评级排行 + 板块强弱 + 异动清单(方案2 第一层)
- 单票技术卡:均线/MACD/KDJ/RSI/量价 + 评级依据
情绪面章节留占位("P2 补"),不空造。产出到 docs/报告/。
契约见 docs/计划/P1_技术面打通.md Step 3。
"""
from __future__ import annotations

import pandas as pd

from tools.config import settings, stock_pool


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
def build_portfolio_tech_report(results: dict[str, dict]) -> str:
    """输入 {code: technical.compute 输出};产出 docs/报告/组合技术_{date}.md。"""
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
    lines += ["", "## 三、技术异动清单", ""]
    for label, codes in [("MACD 金叉", golden), ("KDJ 超卖", oversold),
                         ("KDJ 超买", overbought), ("放量(量比>1.5)", heavy)]:
        names = "、".join(f"{_name(c)}({c})" for c in codes) if codes else "无"
        lines.append(f"- **{label}**:{names}")

    if skipped:
        lines += ["", f"> 数据不足未纳入:{'、'.join(skipped)}"]
    lines += ["", "---", "## 四、情绪面", "*(P2 补:政策 / 公司行为 / 舆情三层)*", ""]

    return _write(settings.REPORT_DIR / f"组合技术_{date}.md", "\n".join(lines))


# ---------- 单票技术卡 ----------
def build_stock_tech_report(code: str, result: dict) -> str:
    """输入单票 compute 输出;产出 docs/报告/个股_{code}_{date}.md。"""
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
        "## 评级依据",
    ]
    lines += [f"- {r}" for r in s["依据"]] or ["- (无)"]
    lines += ["", "---", "## 情绪面", "*(P2 补:政策 / 公司行为 / 舆情)*", ""]
    return _write(settings.REPORT_DIR / f"个股_{code}_{date}.md", "\n".join(lines))
