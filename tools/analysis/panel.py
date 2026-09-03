"""横向总表:把 32 只票的结构化 JSON 拍平成一张表(筛选/排序/比较用)。

读 data/analysis/{code}.json → 拍平 → 落 data/analysis/panel.csv + panel.json + docs 视图。
一行一票,列为关键指标。缺失值留空,不报错。
"""
from __future__ import annotations

import logging

import pandas as pd

from tools.analysis import serialize
from tools.config import settings, stock_pool

logger = logging.getLogger("analysis.panel")

_OUT_DIR = settings.PROJECT_ROOT / "data" / "analysis"


def _yi(x):
    """元 → 亿(保留2位),None 安全。"""
    return None if x is None else round(x / 1e8, 2)


def _vint(block) -> str | None:
    """块的口径日期(块缺失 / 旧记录无该字段 → None,不报错)。"""
    return (block or {}).get("口径日期")


def _row(rec: dict) -> dict:
    m = rec.get("meta") or {}
    snap = rec.get("snapshot") or {}
    sig = rec.get("signals") or {}
    val = rec.get("valuation") or {}
    fund = rec.get("fundamental") or {}
    fin = rec.get("financial") or {}
    flow = rec.get("fundflow") or {}
    pred = rec.get("prediction") or {}
    bias_tend = pred.get("买卖倾向") or {}
    scen = pred.get("情景预测") or {}
    hold = pred.get("持有期建议") or {}
    sup = pred.get("支撑位") or []
    res = pred.get("压力位") or []
    trend = sig.get("trend") or {}
    rev = sig.get("reversal") or {}
    obos = sig.get("ob_os") or {}
    ma = snap.get("ma") or {}
    macd = snap.get("macd") or {}
    kdj = snap.get("kdj") or {}
    rsi = snap.get("rsi") or {}
    return {
        "代码": m.get("code"), "名称": m.get("name"), "板块": m.get("sector"),
        # 价量
        "收盘": snap.get("close"), "涨跌%": snap.get("pct_chg"), "BIAS20": snap.get("bias20"),
        "量比": snap.get("vol_ratio"), "量状态": snap.get("vol_state"),
        # 判定(核心)
        "超买超卖": obos.get("verdict"), "共振数": obos.get("resonance"),
        "趋势评级": trend.get("评级"), "趋势分": trend.get("得分"),
        "拐点标签": rev.get("拐点标签"), "拐点分": rev.get("拐点评分"),
        # 技术明细
        "均线排列": ma.get("排列"), "MACD": macd.get("状态"),
        "KDJ_K": kdj.get("k"), "KDJ_J": kdj.get("j"), "RSI12": rsi.get("rsi12"),
        # 估值
        "PE": val.get("pe_ttm"), "PB": val.get("pb"), "市值亿": val.get("mktcap_yi"),
        "PE有效": val.get("pe_valid"), "PE模式": val.get("mode"),
        # 基本面
        "营收增速": fund.get("营收增速"), "净利增速": fund.get("净利增速"),
        "ROE": fund.get("ROE"), "毛利率": fund.get("毛利率"), "负债率": fund.get("负债率"),
        # 财报质地(P1:analysis.financial 产出的 financial 块)
        "财报评级": fin.get("评级"), "财报红旗": "/".join(fin.get("flags") or []) or None,
        "审计闸门": fin.get("审计意见闸门"),
        # 资金流
        "主力净流入亿": _yi(flow.get("今日主力净流入")), "主力净占比%": flow.get("今日主力净占比"),
        "近5日主力亿": _yi(flow.get("近5日主力合计")), "连续净流入天": flow.get("主力连续净流入天数"),
        # 预测/推荐(P3.2,全百分比)
        "买卖倾向": bias_tend.get("结论"), "买卖分": bias_tend.get("得分"),
        "ATR%": pred.get("atr_pct"),
        "1日涨概率%": (scen.get("1日") or {}).get("上涨概率%"),
        "5日涨概率%": (scen.get("5日") or {}).get("上涨概率%"),
        "5日止盈%": (hold.get("5日") or {}).get("目标盈利%"),
        "5日止损%": (hold.get("5日") or {}).get("最大亏损%"),
        "近支撑": sup[0] if sup else None, "近压力": res[0] if res else None,
        # 事件
        "公告数": len(rec.get("events") or []),
        # —— 口径日期(修「一行里各列不同龄却看不出来」)——
        # 实证的混龄:某票 收盘=09-02 口径,而同一行的 PE/市值是按 08-31 收盘价折算的
        # (市值/股本反推的价格恰好等于 08-31 收盘价,PE 比值与价格比值完全相等)。
        # 根因不在 panel:panel 只是把 record 的块拍平,**record 内部各块本来就不同龄**
        # (价量来自 K线最后一根 bar,估值来自 fundamental 缓存命中的分区),
        # 而 record 过去没有任何字段暴露这一点。故:
        #   ① 各块口径日期已由 serialize 盖在块内(单一真源,panel 不自己另算);
        #   ② panel 只把它们拍平成列,并给一个一眼可见的 `混龄` 标记;
        #   ③ 另加 `记录日期`(=meta.as_of):panel 走 load_record(date="latest"),
        #      各票的最新记录日可能不同,这是**第二条**混龄轴,同样必须可见。
        "记录日期": m.get("as_of"),
        "价格日期": _vint(snap), "估值日期": _vint(val),
        "资金流日期": _vint(flow), "资金流新鲜度": (flow or {}).get("新鲜度"),
        "报告期滞后": val.get("报告期滞后"),
        "混龄": _mixed_vintage(_vint(snap), _vint(val), _vint(flow)),
    }


def _mixed_vintage(*dates) -> bool | None:
    """同一行的**数据列之间**出现两个以上不同口径日期 → True(横向比较需当心)。

    只比数据列彼此,**不把 meta.as_of 算进来**:as_of 与价格日期差一天在盘前/盘中是常态,
    混进来会让这一列天天为 True(报警疲劳)。「块 vs as_of」那条轴由各块自己的 新鲜度 表达。
    全部判不出(旧记录无口径字段)→ None,不假装 False(那会把"不知道"说成"没问题")。
    """
    present = {str(d)[:10] for d in dates if d}
    if not present:
        return None
    return len(present) > 1


def build_panel(codes: list[str] | None = None) -> pd.DataFrame:
    """组装横向总表 DataFrame(按趋势分升序,超卖/弱势在前便于看反弹候选)。"""
    rows = []
    for code in (codes or stock_pool.get_codes()):
        try:
            rows.append(_row(serialize.load_record(code)))
        except FileNotFoundError:
            logger.warning("%s 无结构化记录,跳过(先 serialize)", code)
    df = pd.DataFrame(rows)
    if "趋势分" in df.columns:
        df = df.sort_values("趋势分", na_position="last").reset_index(drop=True)
    return df


def write_panel(codes: list[str] | None = None) -> dict:
    """落盘 panel 视图(经 store 按日期)+ CSV/markdown 人读artifact。返回路径。"""
    from tools.store import repo as store
    df = build_panel(codes)
    records = df.where(pd.notna(df), None).to_dict(orient="records")
    view_p = store.put_view("panel", records)             # 按日期视图(Web/程序读)

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_p = _OUT_DIR / "panel.csv"
    df.to_csv(csv_p, index=False, encoding="utf-8-sig")   # utf-8-sig 便于 Excel 打开

    date = pd.Timestamp.today().strftime("%Y%m%d")
    md_p = settings.PROJECT_ROOT / "data" / "reports" / f"横向总表_{date}.md"
    md_p.parent.mkdir(parents=True, exist_ok=True)
    md_p.write_text(f"# 横向总表 · {date}\n\n共 {len(df)} 只,按趋势分升序(弱势/超卖在前)。\n"
                    f"数据源 data/analysis/<日期>/*.json;完整数据见 panel.csv。\n\n"
                    + df.to_markdown(index=False) + "\n", encoding="utf-8")
    logger.info("横向总表:%d 只 → %s(视图)/ %s", len(df), view_p, csv_p)
    return {"view": view_p, "csv": str(csv_p), "md": str(md_p)}
