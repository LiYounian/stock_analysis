"""财报路由离线验证(A/B,不跑收益回测)。

背景:合议的行业财报专家靠 `analyzer._industry_key(code)` 把个股路由到申万一级行业专家,
`analyzer._is_financial(code)` 决定银行/非银是否走金融业红旗特判。二者都回退
`board.board_of(code)`(证监会行业名)→ `industry_map.to_sw()` 对齐申万一级。
主仓缺 `board_membership` 数据 → board_of 恒 None → 除半导体(semi_universe 兜底)外
全行业财报专家拿不到行业、金融业特判从未触发。本脚本量化"落数据 + 补映射"前后的路由质量。

口径(见 docs/每日分析/策略建议/财报路由验证.md §4):
  A(现状/主仓):无 board_membership → board_of 恒 None → 仅半导体走 semi_universe 兜底。
  B(复活):有 board_membership/all.json(baostock 证监会行业)+ CSRC_CATEGORY_TO_SW 粗名映射。

指标:
  ① 路由命中率:标注集里 _industry_key(code)==真实申万一级 的比例(可对齐子集,目标 B≥85%)。
  ② 误路由率:路由到**错误**行业专家的比例(粗桶应返 None/弃权;目标 B=0)。
  ③ 金融业特判触发率:银行/非银样本 _is_financial(code)==True 的比例(A≈0 → B≈100%)。
  ④ 覆盖率:全 A 里 board_of 非 None、且 to_sw 能对齐到申万一级的比例(报告即可)。

标注集为**独立 ground truth**:semi_universe(申万二级 801081 成分,→电子)+ 人工标注的
知名流动性大票(真实申万一级,不依赖 to_sw)。

诚实标注的两类**证监会口径固有损失**(不计入①②的"可对齐子集",单列):
  - GRANULARITY_LOSS:申万独立而证监会并入相邻门类(家电→电气机械、美护→化工/商贸)。
  - TAXONOMY_DIVERGENCE:证监会门类天然跨多个申万一级,某票真实申万与门类主口径不一致
    (C39 计算机/通信/电子设备 跨 电子/通信/计算机;互联网/商务服务门类跨 计算机/传媒/社会服务;
     专用设备门类含 医疗器械)。这类**不是路由逻辑 bug**,是证监会→申万 门类级近似的多对一
    固有损失(见 industry_map 模块 docstring),个股需 board_of 更细口径或人工池细分才能落准。

纯离线、不触网;A 臂用 monkeypatch 让 board.load_membership 抛 FileNotFoundError 模拟主仓缺数据态。
运行:python -m tools.backtest.verify_financial_routing
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.analysis.financial import analyzer as az
from tools.analysis.financial.industry import EXPERTS as _EXPERTS
from tools.collectors import board, semi_universe

# —— 人工标注的知名大票 → 真实申万一级(独立 ground truth,不依赖 to_sw)——
# 只选证监会口径能忠实对齐申万一级的行业(银行/非银/医药/食饮/房地产/汽车/煤炭/有色/钢铁/
# 公用/交运/基化/机械/军工/传媒/通信/计算机)。家电/美护单列 GRANULARITY_LOSS。
LABELED: dict[str, str] = {
    # 银行
    "600000": "银行", "000001": "银行", "601398": "银行", "601988": "银行",
    "601288": "银行", "600036": "银行", "601166": "银行", "600016": "银行",
    # 非银金融(保险 + 券商)
    "601318": "非银金融", "601601": "非银金融", "601336": "非银金融",
    "600030": "非银金融", "601688": "非银金融", "000166": "非银金融", "000776": "非银金融",
    # 医药生物
    "600276": "医药生物", "000538": "医药生物",
    "600196": "医药生物", "002007": "医药生物",
    # 食品饮料
    "600519": "食品饮料", "000858": "食品饮料", "000568": "食品饮料",
    "600887": "食品饮料", "603288": "食品饮料",
    # 房地产
    "000002": "房地产", "001979": "房地产", "600048": "房地产",
    # 汽车
    "600104": "汽车", "000625": "汽车", "601238": "汽车",
    # 煤炭
    "601088": "煤炭", "601225": "煤炭", "600188": "煤炭",
    # 有色金属
    "600362": "有色金属", "601899": "有色金属", "603993": "有色金属",
    # 钢铁
    "600019": "钢铁", "000709": "钢铁",
    # 公用事业
    "600900": "公用事业", "600011": "公用事业",
    # 交通运输
    "601006": "交通运输", "601111": "交通运输",
    # 基础化工
    "600309": "基础化工", "002648": "基础化工",
    # 机械设备
    "600031": "机械设备", "000157": "机械设备",
    # 国防军工
    "600760": "国防军工", "000768": "国防军工",
    # 计算机
    "600570": "计算机", "002230": "计算机",
    # 电子(非半导体)
    "002415": "电子", "000725": "电子",
}

# 证监会口径粒度损失(申万独立、证监会并入相邻门类):不计入①②可对齐子集,单列诚实报告。
GRANULARITY_LOSS: dict[str, str] = {
    "000651": "家用电器", "000333": "家用电器",   # 格力/美的:CSRC C38电气机械 → 电力设备
}

# 证监会门类跨多申万一级 → 真实申万与门类主口径分叉(非逻辑 bug,门类级近似固有损失)。
TAXONOMY_DIVERGENCE: dict[str, tuple[str, str]] = {
    "300760": ("医药生物", "C35专用设备制造业含医疗器械 → 机械设备"),
    "002027": ("传媒", "L72商务服务业含广告 → 社会服务"),
    "300413": ("传媒", "I64互联网和相关服务 → 计算机"),
    "000063": ("通信", "C39计算机通信电子设备(跨电子/通信/计算机) → 电子"),
    "600522": ("通信", "C38电气机械(光纤光缆) → 电力设备"),
    "000977": ("计算机", "C39计算机通信电子设备(服务器) → 电子"),
}

FINANCIAL_SW = {"银行", "非银金融"}


def _load_membership_or_raise():
    return board.load_membership()


def _run_arm(arm: str, restore) -> dict:
    """在给定 board.load_membership 行为下,算四指标。restore=None 表示 B 臂(真实数据)。"""
    # semi_universe 标注块(→电子),独立 ground truth
    semi = list(semi_universe.load())
    labeled = dict(LABELED)
    for c in semi:
        labeled.setdefault(c, "电子")

    hit = 0
    miswrong = 0          # 路由到错误行业(非 None 且 != 真实)
    abstain = 0           # 路由 None(弃权)
    total = len(labeled)
    mismatches = []
    for code, truth in labeled.items():
        key = az._industry_key(code)
        if key == truth:
            hit += 1
        elif key is None:
            abstain += 1
        else:
            miswrong += 1
            mismatches.append((code, truth, key))

    # ③ 金融业特判触发率(仅银行/非银样本)
    fin_codes = [c for c, t in LABELED.items() if t in FINANCIAL_SW]
    fin_trig = sum(1 for c in fin_codes if az._is_financial(c))

    # ④ 覆盖率:全 A board_of 非 None 且 to_sw 能对齐
    from tools.analysis import industry_map as im
    try:
        mem = board.load_membership()
    except FileNotFoundError:
        mem = {}
    cov_nonnull = sum(1 for c in mem if board.board_of(c))
    cov_aligned = sum(1 for c, ind in mem.items() if im.to_sw(ind or "") is not None)

    return {
        "arm": arm,
        "标注集样本数": total,
        "路由命中": hit,
        "路由命中率": round(hit / total, 4) if total else 0.0,
        "误路由(错行业)": miswrong,
        "误路由率": round(miswrong / total, 4) if total else 0.0,
        "弃权(None)": abstain,
        "金融样本数": len(fin_codes),
        "金融特判触发": fin_trig,
        "金融特判触发率": round(fin_trig / len(fin_codes), 4) if fin_codes else 0.0,
        "全A映射股数": len(mem),
        "board_of非None": cov_nonnull,
        "to_sw可对齐": cov_aligned,
        "覆盖率(对齐/全A)": round(cov_aligned / len(mem), 4) if mem else 0.0,
        "误路由明细": mismatches,
    }


def run() -> dict:
    # B 臂:真实数据
    b = _run_arm("B(复活·有board_membership)", None)

    # A 臂:模拟主仓缺数据 —— 让 load_membership 抛 FileNotFoundError
    orig = board.load_membership
    board.load_membership = lambda: (_ for _ in ()).throw(FileNotFoundError("A臂模拟主仓缺数据"))
    try:
        a = _run_arm("A(现状·无board_membership)", orig)
    finally:
        board.load_membership = orig

    # 粒度损失单列(仅 B 臂,信息性)
    gl = []
    for code, truth in GRANULARITY_LOSS.items():
        gl.append({"code": code, "真实申万": truth,
                   "board_of": board.board_of(code),
                   "路由到": az._industry_key(code)})
    td = []
    for code, (truth, reason) in TAXONOMY_DIVERGENCE.items():
        td.append({"code": code, "真实申万": truth, "board_of": board.board_of(code),
                   "路由到": az._industry_key(code), "口径分叉": reason})

    return {"A": a, "B": b, "粒度损失(信息性)": gl, "门类口径分叉(信息性)": td}


def _fmt(r: dict) -> str:
    return (f"  [{r['arm']}]\n"
            f"    ① 路由命中率 : {r['路由命中率']:.1%}  ({r['路由命中']}/{r['标注集样本数']})\n"
            f"    ② 误路由率   : {r['误路由率']:.1%}  (错行业 {r['误路由(错行业)']} / 弃权 {r['弃权(None)']})\n"
            f"    ③ 金融特判率 : {r['金融特判触发率']:.1%}  ({r['金融特判触发']}/{r['金融样本数']})\n"
            f"    ④ 覆盖率     : {r['覆盖率(对齐/全A)']:.1%}  (to_sw可对齐 {r['to_sw可对齐']} / 全A {r['全A映射股数']})")


def main():
    res = run()
    print("=" * 68)
    print("财报路由离线验证 A/B(标注集 = semi_universe 178 + 人工大票 %d)" % len(LABELED))
    print("=" * 68)
    print(_fmt(res["A"]))
    print(_fmt(res["B"]))
    if res["B"]["误路由明细"]:
        print("\n  B 臂误路由明细(code, 真实, 路由到):")
        for m in res["B"]["误路由明细"]:
            print("    ", m)
    print("\n  粒度损失(证监会并入相邻门类,不计入①②):")
    for g in res["粒度损失(信息性)"]:
        print("    ", g)
    print("\n  门类口径分叉(证监会门类跨多申万,非逻辑bug,不计入①②):")
    for t in res["门类口径分叉(信息性)"]:
        print("    ", t)
    # 落盘 JSON 供留痕
    out = Path(__file__).resolve().parents[2] / "data" / "analysis" / "backtest" / "financial_routing_verify.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  结果落盘: {out}")
    return res


if __name__ == "__main__":
    main()
