# 模型卡:DeepSeek `deepseek-v4-pro`

> 一句话:系统当前**主力生产 LLM**,负责消息面富集 / 事件抽取分类 / 摘要 / 相关性初筛。核实日 2026-09-13。

## 1. 标识
- **模型名 / ID**:`deepseek-v4-pro`(**写死点** `tools/config/settings.py:60` `LLM_MODEL = "deepseek-v4-pro"`,要换模型改这一行)
- **kind**:LLM
- **用途**:消息面富集 / 事件抽取与分类 / 摘要 / 相关性初筛
- **通道**:公司内部网关(OpenAI 兼容),`LLM_BASE_URL` + `LLM_API_KEY`(凭证只在本机 shell,不入库)
- **接线状态**:**已接生产**。客户端 `tools/llm/client.py`;调用方 news_ai / event.py / candidate_message / news_recall 等

## 2. 数据源
- 输入 = 上游采集的新闻/公告文本(baidu_news 等)+ 候选票上下文;不直接读行情。
- **生产消息面实际来源**:baidu_news 的利好/利空标签(**不用 LLM**)+ event.py(DeepSeek)事件分类
  → 聚合成 `sentiment_policy.json`。**不是千问**(见 [qwen3.8-max.md](qwen3.8-max.md))。

## 3. 输入特征(prompt 输入 / 输出 schema)
- 输入:任务化 prompt(文本 + 指令 + 输出 schema 约束);按调用方(事件抽取/摘要/相关性)不同。
- 输出:结构化 JSON(事件类别、利好利空、相关性分等),供下游聚合入 `sentiment_policy.json`。

## 4. 训练 / 加工方式
- **不微调**,纯 prompt 工程 + 网关推理。
- **think 思考模式**:`tools/llm/client.py:95` 已预留 `enable_thinking`,受 `settings.LLM_DISABLE_THINKING`
  控(`settings.py:79`,默认取 env 缺省 `"true"` → 现**关**;实测对 deepseek-v4-pro 中性)。换带思考模型自动生效。
- 客户端构造:`client.py:83`(`__init__(base_url, api_key, model)`)/ `client.py:90`(`self.model = model`)/
  `client.py:97`(`model=self.model` 发起调用)。

## 5. 关键参数现值(带出处)

| 参数 | 现值 | 出处 |
|---|---|---|
| 模型 ID | `deepseek-v4-pro`(写死) | `settings.py:60` |
| think 模式 | 关(`LLM_DISABLE_THINKING` 默认 true) | `settings.py:79` / `client.py:95` |
| base_url / api_key | env 注入(不入库) | `LLM_BASE_URL` / `LLM_API_KEY` |

## 6. 效力 & 局限
- 已接生产、无头程序化:电脑醒着 + 网关通即自动出结果,不需要 Claude 窗口。
- 局限:境外/外部依赖网关可用性;think 模式对本模型实测中性,未提质。
- 架构方向(用户 2026-09-13):模型优先千问 + DeepSeek,随两家推新持续上引,要求**模型可插拔 + 易升级**
  (见 `docs/计划/2026-09-13_选股系统架构设计_程序化与模型迁移.md` §1)。

## 7. 变更日志
- **2026-09-13** — 建卡。核实人:模型卡文档窗。当前生产主力 LLM,写死于 `settings.py:60`。
