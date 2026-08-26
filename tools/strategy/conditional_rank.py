"""策略11:指标条件化状态排序(每日选股)。

把"指标条件化预测"(个股页已有:每持有期 方向/上涨概率%/置信度/是否退回)做成每日全市场
排序榜——按**基线上涨概率% 降序 + 置信度 tiebreak**,每持有期取 Top-K。

⚠️ 诚实定位:该预测自身回测=**聚合无显著超额**、全A概率挤在 45~52%(1日弱区分、5/10日近噪声),
本策略是【状态相似度排序参考】,**非已验证 alpha**;不作胜率/涨跌承诺(沿用个股页那句标注)。

排序键取【基线 上涨概率%/方向】——**不用**激进版倾斜的"修正"(未经回测标定,且全A多数票无
sentiment→不倾斜)。纯函数:输入已含 prediction.指标条件化预测 的中心记录,输出各持有期排行;
可脱离建池/IO 独测。薄管线(tools/pipeline/screen_conditional_rank.py)负责逐票现算条件化并喂本函数。
"""
from __future__ import annotations

from tools.config.strategy import THRESHOLDS
from tools.strategy.registry import strategy

_CFG = THRESHOLDS.get("指标条件化选股", {})
_HORIZONS = list(_CFG.get("持有期", ["1日", "5日", "10日"]))
_TOP_K = int(_CFG.get("top_k", 10))
_PROB_FLOOR = _CFG.get("上涨概率下限%", 50)
_DROP_FALLBACK = bool(_CFG.get("剔除退回", True))
_DROP_INSUFFICIENT = bool(_CFG.get("剔除数据不足", True))
_CONF_ORDER = dict(_CFG.get("置信度序", {"高": 3, "中": 2, "低": 1}))

_SCHEMA = {
    "records": "dict[code, 中心记录];每条含 prediction.指标条件化预测.{1日/5日/10日}",
    "top_k": f"每持有期取前 N(默认 {_TOP_K})",
    "horizons": f"持有期列表(默认 {_HORIZONS})",
    "prob_floor": f"上涨概率软标记阈值%(默认 {_PROB_FLOOR};仅打 过下限 标记,不硬筛)",
    "drop_fallback": f"剔除 是否退回=True(默认 {_DROP_FALLBACK})",
    "drop_insufficient": f"剔除 方向=数据不足(默认 {_DROP_INSUFFICIENT})",
}


def _num(x):
    return float(x) if isinstance(x, (int, float)) else None


@strategy("指标条件化状态排序", "选股", params_schema=_SCHEMA)
def conditional_rank_screen(
    records: dict[str, dict],
    top_k: int = _TOP_K,
    horizons=None,
    prob_floor=_PROB_FLOOR,
    drop_fallback: bool = _DROP_FALLBACK,
    drop_insufficient: bool = _DROP_INSUFFICIENT,
    conf_order=None,
) -> dict:
    """按基线上涨概率% 降序(置信度 tiebreak)对各持有期取 Top-K。

    读每票 prediction.指标条件化预测.{horizon} 的 上涨概率%/方向/置信度/是否退回;剔除
    退回/数据不足/概率缺失后排序。prob_floor 仅作 过下限:bool 标记、不硬筛(保证凑满 Top-K)。
    返回 {排行:{horizon:[{code,name,上涨概率%,方向,置信度,放宽层级,是否退回,过下限}...]},
          有效样本:{horizon:int}, 跳过:{horizon:{原因:数}}, top_k, 参数, 口径}。
    """
    horizons = list(horizons or _HORIZONS)
    conf_order = dict(conf_order or _CONF_ORDER)
    floor = _num(prob_floor)

    rankings: dict[str, list] = {}
    valid_cnt: dict[str, int] = {}
    skip: dict[str, dict] = {}

    for h in horizons:
        rows = []
        sk = {"无预测块": 0, "无该持有期": 0, "概率缺失": 0, "退回": 0, "数据不足": 0}
        for code, rec in (records or {}).items():
            cp = ((rec or {}).get("prediction") or {}).get("指标条件化预测")
            if not isinstance(cp, dict) or cp.get("error"):
                sk["无预测块"] += 1
                continue
            v = cp.get(h)
            if not isinstance(v, dict):
                sk["无该持有期"] += 1
                continue
            if drop_fallback and v.get("是否退回"):
                sk["退回"] += 1
                continue
            if drop_insufficient and v.get("方向") == "数据不足":
                sk["数据不足"] += 1
                continue
            p = _num(v.get("上涨概率%"))
            if p is None:
                sk["概率缺失"] += 1
                continue
            snap = (rec or {}).get("snapshot") or {}
            rows.append({
                "code": str(code),
                "name": ((rec or {}).get("meta") or {}).get("name"),
                "上涨概率%": round(p, 1), "方向": v.get("方向"),
                "置信度": v.get("置信度"), "放宽层级": v.get("放宽层级"),
                "相似样本数": v.get("相似样本数"),          # 状态格样本量(展示;同格相同)
                "成交额万元": _num(snap.get("amount_wan")),  # per-stock 流动性(破同状态格并列)
                "是否退回": bool(v.get("是否退回")),
                "过下限": bool(floor is not None and p >= floor),
            })
        # 排序:上涨概率%(状态格级)→ 置信度(高>中>低)→ 成交额(流动性)。
        # ⚠️ 上涨概率%/置信度 都是**状态格级**(同一指标状态的票取值相同),故 Top 的真正 tiebreak 是
        # 成交额(唯一 per-stock 键)——即"最强状态格里挑流动性好的",不是个股 alpha 排名。
        rows.sort(key=lambda r: (r["上涨概率%"], conf_order.get(r["置信度"], 0), r["成交额万元"] or 0.0),
                  reverse=True)
        rankings[h] = rows[:top_k]
        valid_cnt[h] = len(rows)
        skip[h] = sk

    return {
        "排行": rankings,
        "有效样本": valid_cnt,
        "跳过": skip,
        "top_k": top_k,
        "参数": {"持有期": horizons, "上涨概率下限%": prob_floor,
                 "剔除退回": drop_fallback, "剔除数据不足": drop_insufficient},
        "口径": ("上涨概率%(状态格级)降序 → 置信度 → 成交额(流动性,唯一per-stock次级键,破同状态格并列)。"
                 "⚠️同上涨概率=同指标状态格(个股间无区分),同格内按流动性排、非个股alpha排名;"
                 "状态排序参考·非alpha(聚合无超额;1日弱区分、5/10日近噪声)"),
    }
