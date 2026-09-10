# 大盘预测 v1（market_forecast）

沪深300 / 全A等权代理指数的 **T+1 / T+5 涨跌方向概率 + 五档** 预测器。标称四维因子：
**技术**（指数）+ **市场广度**（全A横截面）+ **消息面**（政策舆情净利好度）+ **资金流**（SSE 市场级两融）。
需求与设计见 `docs/计划/大盘预测策略.md`（§7 = v1 资金流维实现与 A/B 结论）。**非投资建议**，测试环境研究模拟。

> ⚠️ **效力诚实标注**（2026-09-10 维度贡献研究，见 `docs/计划/2026-09-10_市场情绪预测维度贡献研究.md`）：
> 虽名义四维，**每天真正参与判别的只有技术 + 广度两维**——资金流权重=0（kill-switch，2026-09-02），
> 消息面历史仅 ~1 月、长样本覆盖率 ≈1% 被自动降权到 ≈0（且从未经样本外验证）。整体方向命中 ~55%，
> 较纯惯性有 **+4.8pp 的统计边际（显著）**，但 **多空收益价差 ≈0（无经济 alpha）**——方向能微弱猜、
> 收益换不出来，一部分是顺势 β。技术与广度**高度冗余**（去一维命中几乎不掉）。**勿把高概率读成能赚钱。**

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
   (composite 按覆盖率自动降权)。判别力**弱正、样本短不显著**(见下)。**当前默认权重=0.0
   (kill-switch，2026-09-02 统筹/用户决策)**——即该维只接采集管道+快照攒数据、暂不参与预测；
   待 hs300 真实历史累积到样本量足够、判别力转显著再翻回 provisional(0.3)激活。
4. **消息面维长样本里事实上未参与**：历史仅 ~1 月(2026-08 起)，长回测覆盖率≈1%→有效权重≈0.01；
   最近 ~1 月的生产预测里它才有非零权重，但那段**从未经样本外验证**(walk-forward min_train=120 > 消息面天数)。
   激活前须先积累到 ≥(min_train + 可评窗)。
5. **整体命中~55% 无经济 alpha**：方向命中较惯性有 +4.8pp 统计边际(显著)，但**多空收益价差≈0**——
   命中可微弱猜、收益换不出。技术与广度高度冗余、叠加无 1+1>2。详见维度贡献研究报告。

## 回测结论

**v0.5(三维)**：CompositeModel 在真·沪深300（T+1，样本外约 210 日）命中率 **54.3%**，胜 50%/
惯性基线，分档 Spearman 0.7、prob-return 相关 +0.11；全A代理（约 1962 日）胜 50%/惯性（+4pp）
但未胜幸存者偏差抬高的多数类基线。

**v1(补资金流)A/B**（详见 `docs/计划/大盘预测策略.md` §7.3）：真资金流(SSE 两融)判别力**弱正、
样本短不显著**——hs300 T+1 在资金流权重 **0.3** 下命中 0.543→**0.557**(+1.4pp，n=210 内噪声，
命中率标准误≈3.4pp)、分档单调 0.7→0.8；**共等权(1.0)反而稀释、损校准**(mono 0.7→0.3)；T+5 无益。
**结论：只接采集管道+schema+快照(让真资金流累积、供选股读 β)，资金流维暂不参与预测——
默认权重=0.0(kill-switch，2026-09-02)**，待 hs300 历史累积、判别力转显著再翻回 provisional(0.3)。

**维度贡献研究 + tech_atr A/B**（2026-09-10，详见 `docs/计划/2026-09-10_市场情绪预测维度贡献研究.md`
与 `docs/计划/2026-09-10_tech_atr提维加权_AB结论.md`）：
- 拆维发现每天真起作用的只有**技术+广度**，二者高度冗余；tech_atr(波动率，全表最强 IC≈0.08-0.10)
  被技术维内等权 9 列稀释到 1/10。
- A/B 试"ATR/涨停广度提独立维 或 维内 |IC| 加权"提权(`tools/backtest/market_forecast_atr_ab.py`)：
  命中未升、**多空收益价差未显著转正**——proxy h1 最多 +0.0009/天(t≈0.8，不显著)、proxy h5 与
  hs300 各格不一致；naive 把 ATR 单提为整维(B1)反而**拖累命中**。
- **判据不达标 → 未接生产**，只维持文档/研究留痕。确认"~55% 命中无经济 alpha"是**结构性**的：
  重新配权放不出收益，不是稀释造成的可修问题。
