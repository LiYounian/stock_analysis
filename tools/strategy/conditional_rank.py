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
# ——「破下轨接飞刀」市场广度门参数(真源见 config.THRESHOLDS['指标条件化选股'])——
_KNIFE_ON = bool(_CFG.get("破下轨接飞刀_启用", True))
_KNIFE_BREADTH = float(_CFG.get("破下轨接飞刀_广度门槛", 0.02))
_KNIFE_DEMOTE = float(_CFG.get("破下轨接飞刀_降权pp", 20.0))
_KNIFE_BOLL = list(_CFG.get("接飞刀_布林档", ["破下轨"]))

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
    breadth: float | None = None,
    knife_on: bool = None,
    knife_breadth: float = None,
    knife_demote: float = None,
    knife_boll=None,
) -> dict:
    """按基线上涨概率% 降序(置信度 tiebreak)对各持有期取 Top-K。

    读每票 prediction.指标条件化预测.{horizon} 的 上涨概率%/方向/置信度/是否退回;剔除
    退回/数据不足/概率缺失后排序。prob_floor 仅作 过下限:bool 标记、不硬筛(保证凑满 Top-K)。

    「破下轨接飞刀」市场广度门(修"抄在半山腰"):`breadth`=当日扫描池"破下轨"占比(同日截面,无未来函数);
    当 breadth < knife_breadth(平静日/个股孤立破位=真接飞刀区)时,对 snapshot.布林位置 ∈ knife_boll 的候选
    **排序上涨概率%降 knife_demote 个点**(展示 上涨概率% 不变)并标 接飞刀风险=True;门槛以上(全市场恐慌反弹是
    真 edge)不动。breadth=None(单测/降级)或 knife_on=False → 门整体关闭,行为与旧版逐字一致(向后兼容)。
    实证依据:超卖破位前瞻收益按市场广度分位单调(平静日 H5 均值−0.5%、恐慌日 +3.3%),见 config 注释。

    返回 {排行:{horizon:[{code,name,上涨概率%,方向,置信度,放宽层级,是否退回,过下限,布林位置,接飞刀风险}...]},
          有效样本:{horizon:int}, 跳过:{horizon:{原因:数}}, top_k, 参数, 口径, 市场广度}。
    """
    horizons = list(horizons or _HORIZONS)
    conf_order = dict(conf_order or _CONF_ORDER)
    floor = _num(prob_floor)
    knife_on = _KNIFE_ON if knife_on is None else bool(knife_on)
    knife_breadth = _KNIFE_BREADTH if knife_breadth is None else float(knife_breadth)
    knife_demote = _KNIFE_DEMOTE if knife_demote is None else float(knife_demote)
    knife_boll = list(knife_boll if knife_boll is not None else _KNIFE_BOLL)
    # 接飞刀门是否实际生效:开关开 + 传入了广度 + 广度低于门槛(平静日)。三者缺一即门关(不降权、不标风险)。
    knife_active = bool(knife_on and breadth is not None and float(breadth) < knife_breadth)

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
            boll_pos = snap.get("布林位置")
            # 接飞刀风险:门生效(平静日)且该票处破位档(破下轨)→ 平静日孤立破位=真接飞刀,降权 + 标风险。
            knife_risk = bool(knife_active and boll_pos in knife_boll)
            rows.append({
                "code": str(code),
                "name": ((rec or {}).get("meta") or {}).get("name"),
                "上涨概率%": round(p, 1), "方向": v.get("方向"),
                "置信度": v.get("置信度"), "放宽层级": v.get("放宽层级"),
                "相似样本数": v.get("相似样本数"),          # 状态格样本量(展示;同格相同)
                "成交额万元": _num(snap.get("amount_wan")),  # per-stock 流动性(破同状态格并列)
                "布林位置": boll_pos,                        # 破同状态格 + 接飞刀门用(缺则 None)
                "接飞刀风险": knife_risk,                    # 平静日破位=接飞刀(降权 + 展示⚠;门关时恒 False)
                "是否退回": bool(v.get("是否退回")),
                "过下限": bool(floor is not None and p >= floor),
            })
        # 排序:上涨概率%(状态格级,接飞刀候选减 knife_demote)→ 置信度(高>中>低)→ 成交额(流动性)。
        # ⚠️ 上涨概率%/置信度 都是**状态格级**(同一指标状态的票取值相同),故 Top 的真正 tiebreak 是
        # 成交额(唯一 per-stock 键)——即"最强状态格里挑流动性好的",不是个股 alpha 排名。
        # 接飞刀门:平静日破位候选排序概率降 knife_demote(展示"上涨概率%"不变),压到非超卖格之下=不占榜首。
        rows.sort(key=lambda r: (r["上涨概率%"] - (knife_demote if r["接飞刀风险"] else 0.0),
                                 conf_order.get(r["置信度"], 0), r["成交额万元"] or 0.0),
                  reverse=True)
        rankings[h] = rows[:top_k]
        valid_cnt[h] = len(rows)
        skip[h] = sk

    return {
        "排行": rankings,
        "有效样本": valid_cnt,
        "跳过": skip,
        "top_k": top_k,
        "市场广度": {"破下轨占比": (round(float(breadth), 4) if breadth is not None else None),
                     "接飞刀门槛": knife_breadth, "接飞刀门生效": knife_active,
                     "降权pp": knife_demote if knife_active else 0.0},
        "参数": {"持有期": horizons, "上涨概率下限%": prob_floor,
                 "剔除退回": drop_fallback, "剔除数据不足": drop_insufficient,
                 "破下轨接飞刀_启用": knife_on, "破下轨接飞刀_广度门槛": knife_breadth,
                 "破下轨接飞刀_降权pp": knife_demote, "接飞刀_布林档": knife_boll},
        "口径": ("上涨概率%(状态格级)降序 → 置信度 → 近20日成交额均值(流动性,唯一per-stock次级键,破同状态格并列)。"
                 "⚠️同上涨概率=同指标状态格(个股间无区分),同格内按流动性排、非个股alpha排名;"
                 "状态排序参考·非alpha(聚合无超额;1日弱区分、5/10日近噪声)。"
                 "破下轨接飞刀门:平静日(市场破下轨广度<门槛=个股孤立破位)对破位候选排序概率降权+标接飞刀风险,"
                 "避免'抄在半山腰'(超卖反弹edge实证仅在市场级广度确认时成立;广度为同日截面,无未来函数)。"),
    }
