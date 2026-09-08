# tools/eval —— 离线评测脚本

只读历史落盘数据(`data/analysis/<date>/*.json` 的 `council.default` 块 + `data/master/kline`)做
**as-of 前向评测**,不触网、不跑合议、不改任何生产数据。攒够样本(约 40–60 交易日)后用于复评校准。

跑法(用项目 conda 环境):

```bash
~/.conda/envs/stock_analysis/bin/python tools/eval/msg_forward_eval_v2.py
~/.conda/envs/stock_analysis/bin/python tools/eval/msg_doublecount.py
```

可选环境变量:
- `STOCK_ANALYSIS_ROOT`:覆盖仓库根目录(默认由脚本位置推导)。
- `EVAL_OUT_CSV`(仅 v2):覆盖导出行明细路径(默认 `data/analysis/backtest/msg_eval_rows.csv`)。

## 脚本清单

- **msg_forward_eval_v2.py** —— 消息面回灌前向评测主脚本。产出:消息面分 vs 前向收益的
  Spearman IC(全样本 / 仅发声 / 按日 RankIC)、方向分档→平均前向收益、回灌权重 Top-N 扫描、基线。
- **msg_doublecount.py** —— 量化 double-count:消息面专家(情绪三层/事件驱动/资金流)在数据面综合分
  分母(权重×置信度)里的占比;对比「base 含 msg / base 剔 msg」两种口径的 Top-10 前向收益与
  各基数 daily RankIC。结论支撑 2026-09-08 校准(回灌权重 0.5→0.3 + 资金流移出消息面消费者专家)。

## 口径提示

两脚本评测口径里 `MSG` 集合仍含「资金流」,是为**复现历史 double-count 的量级**(度量口径);
这**不等于**生产回灌口径——生产已把资金流移出「消息面消费者专家」,只留 情绪三层 / 事件驱动
(见 `tools/config/strategy.py` 的「消息面回灌.消费者专家」),资金流作为数据面因子仍留默认专家组。
复评时若要评测**新生产口径**,把脚本里 `MSG` 改成 `{"情绪三层","事件驱动"}` 即可。

**2026-09-08(续7)方案乙已上线**:生产已改为 **base(数据面综合分)重算合议时剔除消息面专家**
(`strategy.py`「消息面回灌.base剔除消息面专家」= True)——情绪三层/事件驱动只走回灌通道、不在 base
重复计数(资金流仍留 base)。∴ 复评对齐生产口径时:MSG 集合取 情绪三层/事件驱动,且 **base 变体选
「base 剔 msg」那一档**(即 `msg_doublecount.py` 里 base 排除该两位的口径);w 复评仍围绕 0.3。
