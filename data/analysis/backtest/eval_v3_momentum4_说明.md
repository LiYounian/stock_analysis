# 策略4 动量组合(A腿)接入 v3 回放轨 · 说明

> 纯评测产物,非投资建议。当前口径为阶段参照;最终 Top-N 裁定由统筹整合 eval-topn 线后再出。

## 做了什么
把**策略4 动量组合(A腿)**接进 v3 回放轨(`tools/backtest/eval_v3/replay_source.py`),
新增两个函数:
- `replay_momentum_predictions(...)`:动量4历史回放预测(`source=replay`),**带 `rank_score`=动量分**。
- `run_momentum_replay(...)`:复用 v3 现有打分层(`scoring`/`aggregate`/`prices`)出当前口径成绩。

产物:`eval_v3_momentum4.json`(agg+meta);驱动:`_run_momentum4.py`。

## 口径与防未来函数
动量是**截面 TopK 型**(区别于 5 个逐票 `signal_at` screener):每交易日 T 对回放宇宙每票算
「加权对数动量」分 → R²≥0.4 + 拉普拉斯末根='买'(s=0.07,min_slope=0.002)双闸门 → 按动量分
降序取 Top30 作当日选股。参数复用 `screen_momentum` 默认值,算子复用
`tools.strategy.momentum`,**口径与生产 `combo_momentum_screen` 完全一致**(已核验:同一 as-of
日选股 set 与 order 逐一相等)。

防未来函数:动量分只用 `closes[:t+1]`(尾部=当日 T);拉普拉斯为因果 EMA,`L[t]` 仅依赖 ≤t,
故一次性对全序列预算 L 与逐日截断再算数值等价(省时不泄露未来)。历史 <lookback+1(26)根跳过。

预测记录:`strategy_id=4, strategy=动量组合(A腿), pred_date=T, code, direction=+1,
rank_score=动量分, source=replay, stype=directional`。stype 与既有 replay screener 同口径
(走命中/收益/超额),rank_score 同时供 Top-N / rank-IC。

## 抽样(可复现)
全 master 均匀抽样 **800 票**(seed=20260828)× 近 **250 交易日**(2025-08-19 ~ 2026-08-28),
stride=1,top_k=30。有效扫描 749 票,命中记录 7500 条。抽样宇宙同时作等权基准。运行 ~35s。

## 当前口径结果(全史窗)
| horizon | 已到期样本 | 命中率%_期末[聚类CI] | 期内触及% | 收益均值%(组合日均) | 基准全市场% | 超额%_vs全市场[聚类CI] | 超额聚类p | 收益质量(中位/胜率/盈亏比) |
|---|---|---|---|---|---|---|---|---|
| 1日 | 7470 | 44.9 [42.6, 47.2] | — | 0.142 | 0.078 | **+0.064** [-0.133, 0.250] | 0.494 | 中0.0 / 胜47.0% / 盈亏比1.14 |
| 5日 | 7350 | 42.4 [40.2, 44.8] | 91.5 | -0.262 | 0.134 | **-0.396** [-0.858, 0.078] | 0.081 | 中-1.434 / 胜43.6% / 盈亏比1.21 |

要点:5 日超额 **-0.396%**(vs 全市场等权),聚类 **p=0.081**(未坐实显著负,但方向偏弱);
1 日超额 +0.064%(p=0.494 不显著)。用 close 入场占比 0%,隔夜跳空均值约 -0.25%。
带 rank_score(动量分)=是,可供后续 rank-IC / Top-N。
