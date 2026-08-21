# 股票分析 · A 股策略辅助工具

一个 **A 股行情的策略辅助工具**:从技术面 + 情绪面分析股票,评估**超买超卖**,给出**买卖倾向推荐**、
**不同持有期(1/5/10 日)的止盈止损位**与**推荐金额**。辅助决策,**非投资建议**,操作由用户自行决定。

**产品主流程(两阶段)**:选股筛选 `Screener` → 个股评估 `Evaluator`(超买超卖 = 情绪面 + 数据指标面)→ 买卖推荐 + 止盈止损 + 推荐金额。
详见 [需求与目标.md](docs/设计/需求与目标.md)。分析方向:方案 2(先组合共性,再重点票深挖)。

---

## 📑 文档索引(接手先按顺序读前 3 份)

| 文档 | 讲什么 |
|---|---|
| [docs/项目方案总览.md](docs/设计/项目方案总览.md) | ⭐⭐⭐ 对外分享版:定位/问题/两阶段流程/架构/选型/进度/路线图(**给合作方看这份**) |
| [docs/需求与目标.md](docs/设计/需求与目标.md) | ⭐⭐ 需求总账:原始需求、抽象目标、要做的改动、留口子/待讨论清单 |
| [docs/开发规范.md](docs/开发规范.md) | ⭐⭐ 开发规则(约法)+ 文档体系(计划/问题/日志)+ Phase 流程 |
| [docs/任务看板.md](docs/工程进度.md) | ⭐⭐ 任务分派 + 进度 + 待做队列(多窗口协作,**开工前先认领**) |
| [docs/架构设计.md](docs/架构设计.md) | ⭐ 分层架构、两阶段主流程、模块 I/O 契约、落地路线 |
| [docs/技术指标说明.md](docs/参考/技术指标说明.md) | 技术指标科普与口径:各指标含义/算法/用法/局限(调研 RS1) |
| [docs/数据源调研.md](docs/参考/数据源调研.md) | 数据源评估(调研 RS2):行情/基本面/公告/资金流/新闻从哪来 |
| [docs/大模型调用设计.md](docs/设计/大模型调用设计.md) | LLM 触点、统一客户端、需用户提供的 API 规格(Q7) |
| [docs/环境说明.md](docs/参考/环境说明.md) | conda 环境 `stock_analysis` 的运行/重建/部署 |
| [docs/股票清单.md](docs/参考/股票清单.md) | 32 只票池 + 行业归类(来源:东财自选截图) |
| [docs/计划/](docs/计划/) | 前瞻:分 Phase 实施计划(P1/P2/P3…) |
| [docs/问题/问题台账.md](docs/问题/问题台账.md) | 待决问题 + 外部依赖风险 + bug 踩坑闭环 |
| [docs/日志/开发日志.md](docs/日志/开发日志.md) | 回溯:开发日志(时间倒序) |

### 已完成的调研清单

| 编号 | 调研内容 | 产物 |
|---|---|---|
| RS1 | 技术指标全量(含义/算法/用法/局限)| [技术指标说明.md](docs/参考/技术指标说明.md) |
| RS2 | 数据源(行情/基本面/公告/资金流/新闻)| [数据源调研.md](docs/参考/数据源调研.md) |
| — | 反爬:东财 TLS 指纹墙 + curl_cffi 绕过 | [问题台账 B1/B2](docs/问题/问题台账.md) |
| 待做 | RS3 支撑压力算法 / RS4 止盈止损与仓位法 / RS5 财报框架 / RS6 舆情量化 | 见需求与目标 §7 |

---

## 🤝 协作约定

多窗口/多人并行开发,遵守:

1. **基座/基建改动先认领**:动 `tools/store/`、`tools/config/settings.py`、`tools/scheduler.py`、`web/` 框架、依赖(`requirements.txt`)等**多方共用的基座**前,先在 [任务看板.md](docs/工程进度.md) 认领(写明"谁 / 改哪块 / 分支名"),避免两边各写一版撞车。功能层(analysis/strategy/screener 各自新增文件)冲突风险低,可直接开。
2. **分支开发,main 只做集成**:所有改动从 main 切分支(命名 `feat/` `fix/` `docs/` `research/`),开发完合并回 main,**不在 main 上直接开发**。
3. **数据暂随 git**:`data/analysis`、`data/reports` 在展示端上线前保持跟踪(合作者靠 git 拉数据看网页),**勿擅自移出版本库**。
4. **commit 署名**:commit / PR 不加 AI 协作署名(`Co-authored-by` / `Generated with` 等),除非明确要求标注。
5. **脱敏**:提交前扫敏感字段(手机号/身份证/真名/API key/token/本机绝对路径/内部网关名)。

---

## 代码结构(分层,依赖单向向下)

| 路径 | 层 | 内容 |
|---|---|---|
| `tools/config/` | 基座 | 票池 `stock_pool` · 参数 `settings` · 策略阈值+公式 `strategy`(单一真源) |
| `tools/contracts/` | 基座 | 中心记录 schema + 枚举词表 + 校验器 `validate_record`(层间契约) |
| `tools/llm/` | 基座 | 统一 LLM 客户端(deepseek-v4-pro @ 网关)+ prompts |
| `tools/store/` | 基座 | 数据存取层:raw/记录/视图 统一读写;**DB 后端可配置切换**(`repo` 文件后端 + `backend_db` SQLite/MySQL/PG,`STORE_BACKEND`/`DB_URL`,上层零改动) |
| `tools/collectors/` | 采集 | 行情/基本面/公告/资金流/新闻/**政策/UGC股吧**(唯一外部 I/O) |
| `tools/analysis/` | 分析 | 技术(含拐点/超买超卖)· 估值 · 预测 · 情绪 `event`(LLM)· serialize(产中心记录)· panel · chart |
| `tools/strategy/` | 分析 | **策略层:选股/评分/信号 可注册**(`@strategy`) |
| `tools/screener/` | 选股 | 选股预设(N1 留口子) |
| `tools/backtest/` | 聚合同级 | **回测层:信号回测 + 绩效(防未来函数)** |
| `tools/financial_report/` | 分析 | 财报深挖(N2 留口子) |
| `tools/pipeline/` · `tools/registry/` | 编排/基座 | 编排 DAG · 能力注册表(规划,骨架已立) |
| `tools/report/` · `tools/run.py` · `tools/scheduler.py` | 展示/编排 | 报告渲染 · CLI 编排 · **定时调度(进程内 APScheduler,按配置间隔跑流水线)** |
| `web/` | 展示 | FastAPI+Jinja2+Chart.js **四页**(概览/选股/新闻/个股),只读中心记录 |
| `data/` | 存储 | `raw/` 采集缓存 + `analysis/` 中心记录+视图(gitignore) |

> 架构分层/边界/接口契约见 [架构设计.md](docs/架构设计.md);信息流转+任务流程+逐层职责见 [信息流转与层职责.md](docs/设计/信息流转与层职责.md)。

## 当前状态

**数据 / 分析 / 展示主干 + 策略/存取/回测层 + LLM 情绪均已落地。** 全量 **139 单测通过**。任务与待办见 [任务看板.md](docs/工程进度.md)。

| 能力 | 状态 |
|---|---|
| 技术面(指标/拐点/超买超卖)· 基本面 · 公告 · 资金流 | ✅ |
| 预测/推荐(止盈止损%/情景概率/买卖倾向,纯百分比) | ✅ |
| 新闻情绪(LLM 抽取 deepseek-v4-pro + 三层归类 + 情感聚合) | ✅ |
| 契约层 + 架构/规范权威化 + Git worktree 协作 | ✅ |
| 策略层(可注册)· 数据存取层 · 回测层 BT.1(信号回测) | ✅ |
| 政策采集(东财新闻)· UGC 采集(东财股吧,curl_cffi) | ✅ 采集通 |
| Web 四页(概览/选股/新闻/个股,含 K线+预测+情绪面板) | ✅ |
| 回测 BT.2 选股回测 · 情绪打分接入决策 · pipeline/registry | ⏳ 待做 |
| 选股规则 N1 · 财报框架 N2 | 🔒 待用户 |

数据源(避开东财 TLS 指纹墙):策略扫描用的全 A 日线主档=**Tushare Pro**；其他通用行情采集保留腾讯/新浪及既有降级链路，基本面=同花顺+百度，公告=巨潮，**资金流/新闻/政策/股吧=东财(curl_cffi 指纹伪装 / akshare)**，情绪=deepseek-v4-pro。

## 快速开始

环境为独立 conda 环境 `stock_analysis`(Python 3.11,见 [环境说明.md](docs/参考/环境说明.md)):

```bash
PY=~/.conda/envs/stock_analysis/bin/python
$PY -m tools.run all                       # 采集→分析→结构化JSON→总表→报告
$PY -m pytest tests/ -q                     # 跑测试(55)
$PY -m uvicorn web.app:app --port 8801      # 起 Web:http://localhost:8801
```

**Web 四页**:`/` 今日概览 · `/screen` 选股(预设筛选+组合概览)· `/news` 新闻(公司行为公告)· `/stock/{code}` 个股评估(K线+止盈止损+情景预测+基本面+资金流)。全站标注非投资建议。

## 全 A 策略日线：Tushare Pro

策略选股页面的全 A K 线数据改用 **Tushare Pro** 作为主数据源，覆盖沪深、创业板和科创板，排除北交所。数据以滚动主档形式保存在本机 `data/master/`；该目录已被 Git 忽略，避免把几百 MB 的历史行情或任何凭证上传到仓库。

- **历史首灌**：按交易日批量调用 `daily + adj_factor`，得到 OHLCV 与复权因子；价格按复权因子缩放，保证均线、涨幅、阶段新高等策略计算在同一连续价格口径中。
- **盘后增量**：每个交易日只需请求一次全市场 `daily(trade_date=...)`，再写入已有主档；最大范围选股会优先使用该路径。
- **换手率策略**：额外从 `daily_basic` 读取全市场 `turnover_rate`，对应通达信公式中的 `HSL` 百分比口径。
- **Token 安全**：仅从环境变量 `TUSHARE_TOKEN` 读取，既不写入数据文件，也不打印或提交到 Git。

首次使用前，在本机终端配置 Token（请替换为自己的值，且不要把值写进 README、代码或 Git）：

```bash
export TUSHARE_TOKEN='你的_Tushare_Token'
```

然后执行一次近五年历史首灌；时间跨度可用 `--years` 调整：

```bash
PY=/opt/miniconda3/envs/stock_analysis/bin/python
$PY -m tools.run bootstrap-history --years 5
```

策略回放所需的常用命令：

```bash
# 最大范围选股：同步最新日线并生成当日全 A 广度快照
$PY -m tools.run maxrange

# 回放近三个月的最大范围、最强、拉揉搓及三类量价策略
$PY -m tools.run backfill-maxrange --months 3
$PY -m tools.run backfill-strong
$PY -m tools.run backfill-rub
$PY -m tools.run backfill-volume --months 3
```

> Tushare 的日线为盘后数据。因此正式的日线选股应在收盘且数据源完成更新后运行；午间如需预览，应另接实时行情并将结果标记为“盘中预览”，不能与盘后确认结果混用。
