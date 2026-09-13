# 模型卡:千问 `qwen3.8-max`

> 一句话:规划中的**批量情感打分** LLM(高耗 token 任务外包 qwen,不烧主额度);**当前配了但未接线**。核实日 2026-09-13。

## 1. 标识
- **模型名 / ID**:`qwen3.8-max`(备选 `qwen3.7-max` / `qwen3.6`)
- **kind**:LLM
- **用途**:批量情感打分(设计意图);高耗 token 的批量任务默认外包 qwen,不烧主额度
- **通道**:fintopia 网关,经 `qwen-delegate`,**不烧 Bedrock 额度**
- **接线状态**:**配了但未接线**。`tools/analysis/sentiment.py` 全仓仅自身 + settings 引用,
  **无任何管线 import**;`settings.py:53` `USE_QWEN_SENTIMENT = True` 只是意向开关,尚无生产调用方

## 2. 数据源
- 设计输入 = 批量新闻/文本(与 DeepSeek 消息面同源上游),做情感/利好利空批量打分。
- 现状:未接线,无生产数据流经它。生产消息面实际走 baidu 标签 + event.py(DeepSeek),**不是千问**。

## 3. 输入特征(prompt 输入 / 输出 schema)
- 设计:分批调 `qwen-delegate`,prompt 需**堵死反问陷阱**、给定**严格输出 schema**
  (`sentiment.py` 模块 docstring 明确此约束)。
- 批大小等参数:`settings.QWEN_BATCH_SIZE` / `LLM_ROUTE`(见 `sentiment.py` 头部注释引用)。

## 4. 训练 / 加工方式
- **不微调**,prompt 工程 + 批处理外包。
- 关键工程约束:headless 外包必须堵死反问(信息不全自查/自定,绝不反问)、严格 JSON schema 兜底。

## 5. 关键参数现值(带出处)

| 参数 | 现值 | 出处 |
|---|---|---|
| 模型 ID | `qwen3.8-max`(备选 3.7-max/3.6) | fintopia 网关 `~/.qwen/settings.json` `model.name` |
| 开关 | `USE_QWEN_SENTIMENT = True`(意向,未接线) | `settings.py:53` |
| 批大小 / 路由 | `QWEN_BATCH_SIZE` / `LLM_ROUTE` | `settings.py`(`sentiment.py:5` 引用) |

## 6. 效力 & 局限
- **未接线**:`USE_QWEN_SENTIMENT=True` 仅意向,无管线 import,无样本外效力可标注。
- 走 fintopia 网关不烧 Bedrock,成本优势明确。
- 架构方向(用户 2026-09-13):**模型优先千问 + DeepSeek**,随两家推新持续上引;要求模型可插拔。
  接线属架构设计 §1「路由层」承接项(见 `docs/计划/2026-09-13_选股系统架构设计_程序化与模型迁移.md`)。

## 7. 变更日志
- **2026-09-13** — 建卡。核实人:模型卡文档窗。状态:配了未接线(`sentiment.py` 无管线 import;`settings.py:53` 意向开关)。
