"""live 观测轨:从 data/analysis/<日期>/<策略>.json 读线上实际落盘预测 → 统一预测记录表。

验"线上系统跑对没"。上线仅约 14 交易日,样本薄——所有长窗如实标数据不足。
排序型策略额外抽 rank_score(策略0=综合分、反转低换手=综合分、指标条件化=上涨概率%)供 rank-IC。
方向文案 → ±1;纯多头选股缺方向字段默认 +1。
"""
from __future__ import annotations

import json
import logging
import os

import numpy as np

from . import schema

logger = logging.getLogger("backtest.eval_v3.live")

_DIR = {"看多": 1, "看涨": 1, "多": 1, "看空": -1, "看跌": -1, "空": -1, "中性": 0}
_PICK_KEYS = ("入选清单", "top", "rows", "达标清单", "达标", "观察清单")
# 排序分候选字段名(按优先级):不同策略命名不同。
_SCORE_KEYS = ("综合分", "综合得分", "上涨概率%", "动量分", "score", "分数", "得分")
_SKIP_STEMS = {"backtest", "panel", "screen", "sentiment", "sentiment_policy",
               "factor", "news_ai", "SEPA雷达", "形态选股回测汇总", "选股分析报告"}


def _dir_of(obj: dict) -> int:
    for k in ("综合方向", "方向"):
        v = obj.get(k)
        if v is not None:
            return _DIR.get(str(v), 0)
    return 1


def _score_of(obj: dict):
    for k in _SCORE_KEYS:
        v = obj.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return np.nan


def extract_records(view: dict, meta: schema.StrategyMeta, pred_date: str) -> list[dict]:
    """从一个策略 view 抽预测记录(统一 schema)。排行式(按 horizon)展开为每期一条,方向/分独立。"""
    recs: list[dict] = []
    base = {"strategy_id": meta.strategy_id, "strategy": meta.name,
            "source": "live", "stype": meta.stype, "replayable": meta.replayable}

    rank = view.get("排行")
    if isinstance(rank, dict) and any(str(h).endswith("日") for h in rank):
        for _hkey, lst in rank.items():
            if not isinstance(lst, list):
                continue
            for it in lst:
                if isinstance(it, dict) and it.get("code"):
                    recs.append({**base, "pred_date": pred_date, "code": str(it["code"]),
                                 "direction": _dir_of(it), "rank_score": _score_of(it)})
        # 去重(同票多 horizon 出现→保留一条即可,打分层对所有 horizon 统一算)
        seen, uniq = set(), []
        for r in recs:
            if r["code"] not in seen:
                seen.add(r["code"])
                uniq.append(r)
        return uniq

    top_dir_txt = view.get("方向")
    top_dir = _DIR.get(str(top_dir_txt)) if top_dir_txt is not None else None
    lst = None
    for k in _PICK_KEYS:
        v = view.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("code"):
            lst = v
            break
    if lst is None:
        for v in view.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("code"):
                lst = v
                break
    if not lst:
        return recs
    for it in lst:
        code = it.get("code")
        if not code:
            continue
        d = _dir_of(it)
        if d == 1 and top_dir is not None and "综合方向" not in it and "方向" not in it:
            d = top_dir
        recs.append({**base, "pred_date": pred_date, "code": str(code),
                     "direction": d, "rank_score": _score_of(it)})
    return recs


def _iter_files(analysis_dir: str, dates=None):
    if not os.path.isdir(analysis_dir):
        return
    for d in sorted(os.listdir(analysis_dir)):
        ddir = os.path.join(analysis_dir, d)
        if not (os.path.isdir(ddir) and d[:4].isdigit()):
            continue
        if dates is not None and d not in dates:
            continue
        for fn in sorted(os.listdir(ddir)):
            if not fn.endswith(".json"):
                continue
            stem = fn[:-5]
            if stem in _SKIP_STEMS or (len(stem) == 6 and stem.isdigit()):
                continue
            yield d, stem, os.path.join(ddir, fn)


def load_live_predictions(analysis_dir: str, dates=None):
    """扫 analysis_dir → 统一预测记录表(source=live)。返回 DataFrame。"""
    records = []
    for d, stem, path in _iter_files(analysis_dir, dates):
        try:
            view = json.load(open(path, encoding="utf-8"))
        except Exception as e:   # noqa: BLE001
            logger.warning("读取 %s 失败: %s", path, str(e)[:60])
            continue
        if not isinstance(view, dict):
            continue
        meta = schema.meta_for(stem)
        pred_date = str(view.get("as_of") or d)[:10]
        records.extend(extract_records(view, meta, pred_date))
    return schema.make_frame(records)
