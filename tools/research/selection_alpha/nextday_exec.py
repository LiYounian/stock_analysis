"""次日执行口径 · 事件评估器。

一次性 load 全A kline → 全A等权基准(mkt_ir/mkt_cc)+ 各票 β,再把任意 picks(date,code)
按「D+1 入场 → D+1 收盘」口径评估为 per-event 表(绝对收益/净收益/α/涨停不可买/β桶)。

⚠️ 测试环境研究模拟,非投资建议。防未来:入场/基准/β 全部只用 ≤D+1 已定 OHLC。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import nextday_kernel as K


class Universe:
    """全A特征 + 市场基准 + β 缓存。构造后用 evaluate(picks) 评估任意候选流。"""

    def __init__(self, data_root: str, min_date: str, exclude_bj: bool = True,
                 codes: list[str] | None = None):
        self.data_root = data_root
        self.min_date = min_date
        if codes is None:
            codes = K.universe_codes(data_root, exclude_bj=exclude_bj)
        self.feats: dict[str, dict] = {}
        for code in codes:
            df = K.load_kline(data_root, code, min_date=min_date)
            if df is None or len(df) < 2:
                continue
            self.feats[code] = K.precompute(df)
        self.market = K.build_market(self.feats)
        self.mkt_ir = self.market["mkt_ir"]           # 全A等权 open→close(执行日基准)
        self.mkt_cc = self.market["mkt_cc"]            # 全A等权 昨收→今收(算 β)
        self._date_pos: dict[str, dict[str, int]] = {}
        self._beta: dict[str, np.ndarray] = {}
        # 对齐市场 cc 序列到每票日期,算 β
        for code, f in self.feats.items():
            self._date_pos[code] = {d: i for i, d in enumerate(f["dates"])}
            mkt_cc_aligned = np.array([self.mkt_cc.get(d, np.nan) for d in f["dates"]])
            self._beta[code] = K.rolling_beta(f["ret_cc"], mkt_cc_aligned)

    def max_settled_date(self) -> str:
        """全A出现过的最大执行可用日期(有 open→close 的日)。"""
        return max(self.mkt_ir) if self.mkt_ir else ""

    def evaluate(self, picks: pd.DataFrame, cost_bps: float = 10.0,
                 rules: tuple[str, ...] = ("limit_pc_0.0", "limit_pc_0.01", "open"),
                 model: str = "marketable",
                 max_exec_date: str | None = None) -> pd.DataFrame:
        """picks[date,strategy,code] → 逐事件逐档评估。长表:每 (event × rule) 一行。"""
        out_rows = []
        for r in picks.itertuples(index=False):
            code = r.code
            f = self.feats.get(code)
            if f is None:
                continue
            pos = self._date_pos[code].get(r.date)
            if pos is None or pos + 1 >= f["n"]:
                continue                      # 决策日不在该票 / 无 D+1(未到期)
            t = pos
            exec_date = f["dates"][t + 1]
            if max_exec_date is not None and exec_date > max_exec_date:
                continue
            o_n = f["o"][t + 1]; h_n = f["h"][t + 1]; l_n = f["lo"][t + 1]; c_n = f["c"][t + 1]
            close_t = f["c"][t]
            if not (o_n > 0) or not np.isfinite(c_n):
                continue
            mkt = self.mkt_ir.get(exec_date, np.nan)
            beta = self._beta[code][t]
            # 动量代理成员(as-of D):close>MA20>MA60 且 ret20>0(用于"我们是否在动量之上加价值")
            ma20 = f["ma20"][t]; ma60 = f["ma60"][t]; r20 = f["ret20"][t]
            mom = bool(np.isfinite(ma20) and np.isfinite(ma60) and np.isfinite(r20)
                       and close_t > ma20 and ma20 > ma60 and r20 > 0)
            unbuyable = bool(K.limit_up_unbuyable(
                code, np.array([close_t]), np.array([o_n]))[0])
            Ps = K.entry_prices(f, np.array([t]))
            for rule in rules:
                P = Ps[rule]
                fill, ret, filled = K.fill_and_return(
                    P, np.array([o_n]), np.array([h_n]), np.array([l_n]),
                    np.array([c_n]), model)
                filled0 = bool(filled[0])
                ret0 = float(ret[0]) if filled0 else np.nan
                net0 = float(K.net_return(np.array([ret0]), cost_bps)[0]) if filled0 else np.nan
                alpha0 = (ret0 - mkt) if (filled0 and np.isfinite(mkt)) else np.nan
                alpha_net0 = (net0 - mkt) if (filled0 and np.isfinite(mkt)) else np.nan
                out_rows.append(dict(
                    date=r.date, exec_date=exec_date, strategy=r.strategy, code=code,
                    rule=rule, filled=filled0, unbuyable=unbuyable,
                    fill=float(fill[0]) if filled0 else np.nan,
                    ret=ret0, net=net0, mkt=float(mkt) if np.isfinite(mkt) else np.nan,
                    alpha=alpha0, alpha_net=alpha_net0,
                    beta=float(beta) if np.isfinite(beta) else np.nan,
                    mom=mom,
                ))
        return pd.DataFrame(out_rows)
