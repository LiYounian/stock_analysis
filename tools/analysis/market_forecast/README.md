# 大盘预测 v0.5（market_forecast）

沪深300 / 全A等权代理指数的 **T+1 / T+5 涨跌方向概率 + 五档** 预测器。三维因子：
**技术**（指数）+ **市场广度**（全A横截面）+ **消息面**（政策舆情净利好度）。
需求与设计见 `docs/计划/大盘预测策略.md`。**非投资建议**，测试环境研究模拟。

## 模块

| 文件 | 职责 |
|---|---|
| `dataroot.py` | worktree 兼容的数据根解析（含 master/kline），monkeypatch `store` |
| `breadth.py` | 市场广度聚合器（扫 master/kline，**涨停线按板块**，纯本地可回溯 2018） |
| `sentiment.py` | 消息面因子（读 `analysis/*/sentiment_policy.json` 聚合日度净利好度） |
| `technical_index.py` | 指数技术因子（复用 `tools.analysis.technical` 算子，向量化 as-of） |
| `features.py` | 拼特征面板 + 构建标的收盘序列（hs300 / proxy）+ 标签 |
| `predictor.py` | CompositeModel（可解释因子加权，v1 主）/ LogisticModel（对照） |
| `forecast.py` | 每日产出 `market_forecast.json`（schema `market_forecast/v0.5`） |
| `tools/backtest/market_forecast_backtest.py` | walk-forward 前向回测 |

## 涨停线（按板块，无 ST 名单时的启发式）

主板 60/00 ±10%、创业板 30 / 科创板 68 ±20%、北交所 92… ±30%、主板 ST ±5%。
判定 = **封板**（涨停 close≈high / 跌停 close≈low）**且** pct 落在该板块允许限价容差带内。
主板另用 5% 带兜住 ST（无 ST 成分名单，故为启发式；可能把恰好 +5% 且封板的普通主板票误记，
概率低，已在报告标注）。

## 用法

```bash
PY=~/.conda/envs/stock_analysis/bin/python
# 前向回测（--target proxy|hs300, --horizon 1|5, --model composite|logistic）
$PY -m tools.backtest.market_forecast_backtest --target hs300 --horizon 1 --model composite
# 产出某日预测（默认最新交易日；--write-analysis 落 data/analysis/<日>/market_forecast.json）
$PY -m tools.analysis.market_forecast.forecast --model composite
```

## 防未来函数（硬红线）

- 因子只用 ≤T 信息：技术/广度用因果 rolling，消息面只用信号日当天条目；
- 标签用 T+1/T+5 前瞻收益（合法）；
- 回测 walk-forward：训练集**严格早于测试日且标签已到期**（`pos[t]+h < pos[d]`），
  标准化/定向/系数只在训练集拟合；单测 `tests/test_market_forecast_predictor.py` 锁死。

## 已知局限（诚实标注）

1. **全A等权代理指数含幸存者偏差**：master 只含当前在市个股 → 代理指数上偏、"always-up"
   基线被抬高。方向研究可用，绝对收益勿当真。真·沪深300 无此偏差但历史仅约 2025-04 起。
2. **消息面历史浅**（~1 月）：长回测里绝大多数日子消息面缺省为 0；CompositeModel 按训练集
   **消息面覆盖率自动降权**，避免稀疏因子绑架预测。
3. **资金流维缺位**（v1 再补 akshare 大盘资金流 / 北向 / 两融）。

## 回测结论（截至 2026-09-01，见开发记录）

CompositeModel 在真·沪深300（T+1，样本外约 209 日）命中率 **54.6%**，胜 50% / 多数类 /
惯性三基线，分档 Spearman 0.7、prob-return 相关 +0.11；全A代理（约 1961 日）胜 50%/惯性
（+4pp）但**未胜幸存者偏差抬高的多数类基线**。LogisticModel 过拟合、不稳。
**结论：原型验证出弱正 α，够做 β 环境信号（带说明），未强达标到可直接门控交易**；
建议扩指数历史 + 累积消息面/资金流后再升 v1。
