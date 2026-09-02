"""消息面因子(读 analysis/<日>/sentiment_policy.json)——大盘预测的"消息面"维。

聚合**日度净利好度** = Σ(影响强度 × 方向符号),外加利好/利空条数比、条数、受影响行业广度。
方向符号:利好 +1 / 利空 −1 / 中性 0。强度 1–5。

历史仅约 1 个月(从接入日累积),故作**近端实时因子**,长回测里覆盖不到的日期该维缺省 0
(降级为中性),在报告里说明局限。防未来函数:某日的消息面只用该日(信号日 t)落的条目。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re

import numpy as np
import pandas as pd

logger = logging.getLogger("market_forecast.sentiment")

_DIR_SIGN = {"利好": 1.0, "利空": -1.0, "中性": 0.0}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _agg_one(items: list) -> dict:
    """单日条目列表 → 聚合指标。"""
    net = 0.0
    bull = bear = neu = 0
    industries: set = set()
    for it in items or []:
        d = it.get("影响方向") or it.get("方向")
        s = it.get("影响强度", it.get("强度", 0)) or 0
        try:
            s = float(s)
        except (TypeError, ValueError):
            s = 0.0
        sign = _DIR_SIGN.get(d, 0.0)
        net += sign * s
        if sign > 0:
            bull += 1
        elif sign < 0:
            bear += 1
        else:
            neu += 1
        for ind in (it.get("受影响行业") or it.get("industries") or []):
            industries.add(ind)
    n = bull + bear + neu
    return {
        "se_net": net,                                  # 净利好度(Σ强度×方向)
        "se_bull": bull, "se_bear": bear, "se_neu": neu, "se_n": n,
        "se_ratio": (bull - bear) / (bull + bear + 1),  # 利好利空条数净比(拉普拉斯平滑)
        "se_ind_breadth": len(industries),              # 受影响行业广度
    }


def compute_sentiment(data_root=None) -> pd.DataFrame:
    """扫 analysis/<日>/sentiment_policy.json → 日度消息面因子(index=date 升序)。

    无任何文件时返回空 DataFrame(下游按缺省 0 处理)。
    """
    from tools.analysis.market_forecast.dataroot import ensure_data_root, analysis_dir
    root = ensure_data_root(str(data_root) if data_root else None)
    adir = analysis_dir(root)
    rows = {}
    for path in sorted(glob.glob(str(adir / "*" / "sentiment_policy.json"))):
        date = os.path.basename(os.path.dirname(path))
        if not _DATE_RE.match(date):
            continue
        try:
            items = json.load(open(path, encoding="utf-8"))
        except Exception as e:  # pragma: no cover
            logger.warning("读 %s 失败:%r", path, e)
            continue
        if isinstance(items, dict):                      # 兼容 {list:[...]} 包装
            items = items.get("items") or items.get("list") or []
        rows[pd.Timestamp(date)] = _agg_one(items)
    if not rows:
        logger.warning("未找到任何 sentiment_policy.json(消息面维将全缺省)")
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    df.index.name = "date"
    return df


def normalize_features(se: pd.DataFrame) -> pd.DataFrame:
    """把原始消息面指标压成 [-1,1] 量级的模型特征(tanh 挤压,零依赖历史分布→无未来函数)。"""
    if se is None or se.empty:
        return pd.DataFrame()
    out = pd.DataFrame(index=se.index)
    out["se_net_z"] = np.tanh(se["se_net"] / 20.0)      # 20≈典型日净利好度尺度
    out["se_ratio"] = se["se_ratio"]
    out["se_intensity"] = np.tanh(se["se_n"] / 50.0)    # 条数活跃度
    return out
