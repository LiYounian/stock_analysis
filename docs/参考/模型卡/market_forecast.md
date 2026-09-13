# 模型卡:大盘预测 market_forecast(CompositeModel)

> 一句话:四维可解释因子加权的**小统计模型**,预测大盘指数 T+1/T+5 的**上涨方向概率 + 五档**,
> 只作选股的 β 方向背景,**绝不当收益依据**。核实日 2026-09-13。
> 素材源:`docs/参考/2026-09-13_当前模型与策略清单.md` §2;设计 `docs/计划/大盘预测策略.md`;目录 README。

## 1. 标识
- **模型名 / ID**:`CompositeModel`(主,v1 可解释)/ `LogisticModel`(对照,numpy 自实现 L2 逻辑回归)
  - 注册点:`MODELS = {"composite":…, "logistic":…}`(`tools/analysis/market_forecast/predictor.py:224`)
- **kind**:统计(四维因子加权,**非神经网络 / 非大参数模型**)
- **用途**:预测 **沪深300 / 全A等权代理指数(proxy)** 的 T+1、T+5「上涨方向概率 + 五档」;
  **只给方向概率,不预测涨跌幅**。选股环节读它做 β 基准、算个股 α。
- **通道**:纯 Python,launchd 定时无头程序化(不依赖任何 LLM、不依赖 Claude 窗口)
- **接线状态**:**已接生产**,每日滚动重训(`tools/analysis/market_forecast/forecast.py`)

## 2. 数据源(每维:源 / 回溯 / 披露滞后)

| 维 | 模块 | 数据源 | 回溯 | 披露滞后 |
|---|---|---|---|---|
| 技术 | `technical_index.py` | 指数自身 K 线 | 指数历史 | 无(as-of 因果) |
| 广度 | `breadth.py` | 全 A 横截面(扫 master/kline) | 回溯 2018 | 无(截面当日) |
| 消息面 | `sentiment.py` | `analysis/*/sentiment_policy.json`(上游=baidu 标签 + event.py/DeepSeek) | 历史仅 ~1 月(2026-08 起) | 无 |
| 资金流 | `fundflow.py` | **SSE 市场级两融**(akshare `stock_margin_sse`,**仅沪市**) | 回溯 2022 | **盘后披露 → 滞后 ≥1 交易日**(防未来硬约束) |

## 3. 输入特征(四维 × 因子搭配)

- **技术**:复用 technical 算子(MA / MACD / RSI / BIAS 等),as-of 向量化因果 rolling(`technical_index.py`)。
  最强单因子 `tech_atr`(波动率,全表最强 IC≈0.08–0.10)。
- **广度**:涨跌家数 / 净广度、**涨停线按板块**、above/below_ma20 占比、创 N 日新高新低等(`breadth.py`)。
- **消息面**:日度政策舆情净利好度(`sentiment.py`);历史浅 → 长回测覆盖率≈1% → 有效权重≈0.01。
- **资金流**:融资买入强度、融资余额 5 日 / 20 日动量(`fundflow.py`);滞后 ≥1 交易日。
- 特征列清单见 `predictor.py`(`FEATURE_COLS`)。

## 4. 训练 / 加工方式(walk-forward + expanding window)

**被拟合的**(每次都只在训练集上):① 每维因子**标准化**(z-score + 温莎化裁剪,防近零方差爆掉);
② **定向**——每特征在**训练集内**与"次日涨跌标签"算相关、取符号(正向/反向);③ 分维取均值打分 →
按**组权重**合成;④ **校准**成逻辑函数 → P(上涨) + 五档。

- 生产每天产出时用"截至 T 之前的全部历史"**重新 fit**(**expanding window,每日滚动重训**),
  `forecast.py:91-109`(`_fit_predict_asof` → `P.MODELS[name](cfg).fit(Xtr, ytr)`)。
- **⚠️ 没被学习的:维间组权重手工固定(config),不随市场自适应**——当前"会固化"的点(架构设计 §5 承接)。
- 标签 = T+1/T+5 前瞻收益方向(合法);walk-forward 训练集严格早于测试日,单测锁死防未来
  (`tests/test_market_forecast_predictor.py`)。

## 5. 关键参数现值(带出处)

> **真源常量 = `tools/config/strategy.py` 的 `THRESHOLDS["大盘预测"]`**;文档只引出处,数值以代码为准。
> 组权重经 `predictor.py:32`(`_CFG = THRESHOLDS["大盘预测"]`)→ `predictor.py:125`(`gw = self.cfg["因子权重"]`)读取。

| 参数 | 现值 | 出处 |
|---|---|---|
| 预测视野 | `[1, 5]`(T+1 / T+5) | `strategy.py:360` |
| 最小训练样本 | `120` 根(不足则跳过该测试日) | `strategy.py:361` |
| 资金流滞后交易日 | `1`(防未来硬约束,勿改小) | `strategy.py:372` |
| **组权重(四维)** | 见下 WEIGHTS_TABLE | `strategy.py:370` |

**组权重现值**(机读小表,由 `tests/test_model_card_weight_consistency.py` 解析校验;
改此表必同步改 `strategy.py:370`——否则一致性测试红):

<!-- WEIGHTS_TABLE:BEGIN (keep in sync with tools/config/strategy.py:370; parsed by tests/test_model_card_weight_consistency.py) -->

| 维 | 组权重现值 |
|---|---|
| 技术 | 1.0 |
| 广度 | 1.0 |
| 消息面 | 1.0 |
| 资金流 | 0.0 |

<!-- WEIGHTS_TABLE:END -->

**kill-switch 语义(资金流 = 0.0)**:2026-09-02 统筹/用户决策关闭。A/B 回测(独立复算确认)显示真资金流
判别力**弱正、样本短(hs300 n≈210,标准误≈3.4pp)不显著**——降权 0.3 时 hs300 T+1 命中 0.543→0.557
(+1.4pp,落噪声内)、单调性 0.7→0.8;等权 1.0 反而稀释判别力 / 损校准。**此权重即 kill-switch:
0=关(现值),0.3=provisional 激活**;先只接采集管道 + 攒数据,待 hs300 真实历史累积到判别力转显著再翻回 0.3。
消息面维同理按训练集覆盖率**自动降权**(历史未采到的日子缺省 0 → 权重缩 0),长样本里事实上有效权重≈0.01。

## 6. 效力 & 局限(诚实标注)

- 沪深300 T+1 命中率约 **54.3%**(样本外 ~210 日),胜 50%/惯性基线但幅度小(标准误≈3.4pp)。
- **无经济 alpha**——方向能微弱猜、多空收益价差≈0("钱换不出来")。
- 四维标称、**实际只有技术 + 广度真起作用**(二者高度冗余);消息面按覆盖率自动降权≈0;资金流权重=0.0(kill-switch)。
- 全A代理指数(proxy)含**幸存者偏差**(只含当前在市个股 → 偏高),绝对收益别当真。
- **定位**:只作选股时的 β 方向背景,**绝不当收益依据**。
- 回测口径:`tools/backtest/market_forecast_backtest.py`(walk-forward 前向回测,算命中率 / 分档单调性 / 多空价差)。

## 7. 变更日志

- **2026-09-13** — 建卡(本文件),规范化现状基线 §2;组权重纳入一致性测试。核实人:模型卡文档窗。
- **2026-09-02** — 资金流组权重 0.3 → **0.0**(kill-switch 关闭)。决策:统筹/用户。出处:`strategy.py:370` 注释 + 本卡 §5。
