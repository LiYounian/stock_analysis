# 大盘预测 v1（market_forecast）

沪深300 / 全A等权代理指数的 **T+1 / T+5 涨跌方向概率 + 五档** 预测器。四维因子：
**技术**（指数）+ **市场广度**（全A横截面）+ **消息面**（政策舆情净利好度）+ **资金流**（SSE 市场级两融）。
需求与设计见 `docs/计划/大盘预测策略.md`（§7 = v1 资金流维实现与 A/B 结论）。**非投资建议**，测试环境研究模拟。

## 模块

| 文件 | 职责 |
|---|---|
| `dataroot.py` | worktree 兼容的数据根解析（含 master/kline），monkeypatch `store` |
| `breadth.py` | 市场广度聚合器（扫 master/kline，**涨停线按板块**，纯本地可回溯 2018） |
| `sentiment.py` | 消息面因子（读 `analysis/*/sentiment_policy.json` 聚合日度净利好度） |
| `fundflow.py` | **资金流因子**（读 SSE 市场级两融缓存 → 融资买入强度/余额5日/20日动量） |
| `technical_index.py` | 指数技术因子（复用 `tools.analysis.technical` 算子，向量化 as-of） |
| `features.py` | 拼特征面板 + 构建标的收盘序列（hs300 / proxy）+ 标签；资金流滞后拼接（防未来函数） |
| `predictor.py` | CompositeModel（四维可解释因子加权，主）/ LogisticModel（对照） |
| `forecast.py` | 每日产出 `market_forecast.json`（schema `market_forecast/v1`，含 `fundflow_snapshot`） |
| `tools/collectors/market_fundflow.py` | **SSE 市场级两融采集器**（akshare `stock_margin_sse`，回溯 2022，缓存 `raw/market_fundflow/`） |
| `tools/backtest/market_forecast_backtest.py` | walk-forward 前向回测（`--no-fundflow` 关资金流维=v0.5，供 A/B） |

## 涨停线（按板块，无 ST 名单时的启发式）

主板 60/00 ±10%、创业板 30 / 科创板 68 ±20%、北交所 92… ±30%、主板 ST ±5%。
判定 = **封板**（涨停 close≈high / 跌停 close≈low）**且** pct 落在该板块允许限价容差带内。
主板另用 5% 带兜住 ST（无 ST 成分名单，故为启发式；可能把恰好 +5% 且封板的普通主板票误记，
概率低，已在报告标注）。

## 用法

```bash
PY=~/.conda/envs/stock_analysis/bin/python
# 采集/更新 SSE 市场级两融（资金流维数据源；缓存 raw/market_fundflow/，前向增量幂等）
$PY -m tools.collectors.market_fundflow            # 缺省 2022-01-01→今
# 前向回测（--target proxy|hs300, --horizon 1|5, --model composite|logistic；--no-fundflow=v0.5三维）
$PY -m tools.backtest.market_forecast_backtest --target hs300 --horizon 1 --model composite
# 产出某日预测（默认最新交易日；--write-analysis 落 data/analysis/<日>/market_forecast.json）
$PY -m tools.analysis.market_forecast.forecast --model composite
```

## 防未来函数（硬红线）

- 因子只用 ≤T 信息：技术/广度用因果 rolling，消息面只用信号日当天条目；
- **资金流盘后披露 → 滞后≥1交易日**：as_of=T 的资金流特征只用两融 date<T（`_attach_fundflow_lagged`
  以 merge_asof `allow_exact_matches=False` 强制取前一交易日），单测 `tests/test_market_fundflow.py` 锁死；
- 标签用 T+1/T+5 前瞻收益（合法）；
- 回测 walk-forward：训练集**严格早于测试日且标签已到期**（`pos[t]+h < pos[d]`），
  标准化/定向/系数只在训练集拟合；单测 `tests/test_market_forecast_predictor.py` 锁死。

## 已知局限（诚实标注）

1. **全A等权代理指数含幸存者偏差**：master 只含当前在市个股 → 代理指数上偏、"always-up"
   基线被抬高。方向研究可用，绝对收益勿当真。真·沪深300 无此偏差但历史仅约 2025-04 起。
2. **消息面历史浅**（~1 月）：长回测里绝大多数日子消息面缺省为 0；CompositeModel 按训练集
   **消息面覆盖率自动降权**，避免稀疏因子绑架预测。
3. **资金流维=SSE 两融代理**（v1）：东财大盘资金流本机被墙、北向 2024-08 停披露，故用 SSE 市场级
   两融(回溯2022)作真资金流。**只含沪市**(未并深/北)，且 2018-2021 段代理回测无两融→该维缺省 0
   (composite 按覆盖率自动降权)。判别力**弱正、样本短不显著**(见下)，默认权重降到 **0.3**。

## 回测结论

**v0.5(三维)**：CompositeModel 在真·沪深300（T+1，样本外约 210 日）命中率 **54.3%**，胜 50%/
惯性基线，分档 Spearman 0.7、prob-return 相关 +0.11；全A代理（约 1962 日）胜 50%/惯性（+4pp）
但未胜幸存者偏差抬高的多数类基线。

**v1(补资金流)A/B**（详见 `docs/计划/大盘预测策略.md` §7.3）：真资金流(SSE 两融)判别力**弱正、
样本短不显著**——hs300 T+1 在资金流权重 **0.3** 下命中 0.543→**0.557**(+1.4pp，n=210 内噪声，
命中率标准误≈3.4pp)、分档单调 0.7→0.8；**共等权(1.0)反而稀释、损校准**(mono 0.7→0.3)；T+5 无益；
proxy 命中微降但校准 corr 改善(−0.007→+0.024)。logistic 对照普涨命中。
**结论：v1 采集管道+schema+快照接生产(让真资金流累积、供选股读 β)，资金流判别贡献 provisional、
默认权重 0.3，待 hs300 历史累积再复核升权**；仍未强达标到可直接门控交易。
