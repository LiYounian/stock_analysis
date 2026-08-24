# 情绪数据新鲜度:date-pin 采集 + 新鲜度字段(contract-first)· 设计与预注册

> 日期:2026-08-24 · 分支:`feat/sentiment-freshness-datepin` · 状态:计划待审阅(约法4)· ⚠️ 非投资建议
> 缘起:情绪「净情绪分」多天雷同。策略提供者已定位根因两条(输入陈旧静默复用 + 结构性零值)。本轮**只在采集侧治理**:date-pin 锁定当日交易日采集、禁用/标记 latest 回退旧数据;并 contract-first 给每条情绪记录补新鲜度字段,让消费侧能识别陈旧、按需失效或降权。
> 本轮**不改情绪引擎打分算法**(三层加权口径不动)、**不动稳定消费接口**。

---

## 〇 背景 / 根因(已核实,非推翻)

### 0.1 现状数据流(核实到代码)

情绪三层的输入全部经 `store.get_raw(kind, code, date="latest")` 读缓存:

| 层 | 读取入口 | 代码位置 | 传 date 吗 |
|---|---|---|---|
| 新闻 | `news.load_news(code)` → `store.get_raw("news", code)` | `tools/collectors/news.py:259` | ❌ 恒 "latest" |
| 舆情 | `ugc.load_ugc(code)` → `store.get_raw("ugc", code)` | `tools/collectors/ugc.py:192` | ❌ 恒 "latest" |
| 政策 | `policy.load_policy(date)` → `store.get_raw("policy", policy_{date})` | `tools/collectors/policy.py:270` | ✅ 有 date(缺省今天) |
| 编排 | `event.analyze_stock(code)` | `tools/analysis/event.py:254` | ❌ 无 date 形参 |

即:**新闻/舆情两层完全没有 date 概念**,`analyze_stock` 也没有 date 形参,永远读"最新那份 raw"。

### 0.2 `get_raw(date="latest")` 的回退语义(核实到代码)

`store/repo.py` 的 `_resolve_read_date`(L107-123):

- 显式具体日期(非 None/"latest"):直接返回,不判存在性(保留原 FileNotFoundError 路径)。
- `None`/`"latest"`:`_date_dirs_desc` 从最新日期**倒序遍历**,返回**第一个该文件确实存在的日期**——即"含该 (kind, code) 的最新日期",命中即停。

这就是**主因**:某票某天没重采,`data/raw/<今天>/news/<code>.json` 不存在,`get_raw("news", code, "latest")` **静默回退**到含该文件的上一个日期目录(如昨天),读到**字节完全相同的旧新闻** → 叠加 LLM `temperature=0` + `_cached_extract` 按 `(指令+文本)` 内容 hash 命中缓存(`event.py:65-74`)→ 抽取结果字节级等于昨天 → 聚合净情绪分逐日雷同。
(实测:相邻两日新闻层字节相同占 28%、整块 sentiment 相同 8.9%。)

### 0.3 已有但未被利用的新鲜度基建(核实到代码)

`store/repo.py` **已经**有 sidecar 元数据与新鲜度判定,但**情绪引擎从没调用**:

- `put_raw` 旁写 `<code>.meta.json`,含 `fetched_at`(采集时刻 ISO)、`kind`、`code`、`rows`、`source`(`_write_meta` L226-234)。
- `get_raw_meta(kind, code, date="latest")`:回退逻辑**与 get_raw 完全一致**(L237-248)——锚定到实际读到的那一日数据,故 `fetched_at` 反映的是"真正读到的那份 raw 的采集时刻",天然可识别"回退到旧数据"。
- `raw_age_days` / `is_stale(kind, code, max_days)`(L251-267):现成的陈旧判定,无数据/无 meta 一律视为陈旧。

**结论**:识别陈旧的基建已备,缺的是①采集侧把 date 锁死、②情绪引擎读 meta 并把新鲜度写进 record。

### 0.4 结构性零值(次因,本轮标注不消除)

- 政策层按 `stock.sector` 命中行业(`event.py:_stock_policy_items` L178-189),89.1% 记录命中数=0 → `_policy_layer_net` 返回 `(0.0, 0)`。
- 新闻无相关(与本股关系=无关被 rel 权重 0 剔除)15.8%、UGC 无缓存 14.7% → `ugc_sentiment` 降级 `净情绪 0.0 + degraded` 标记(`event.py:216-219`)。

这些**不是 bug**(有新料的票净情绪逐日在动,引擎本身正常),是"零样本 = 中性 0.0"的语义。本轮通过 `新鲜度=无数据` + `样本数=0` **让消费侧能区分"真中性"与"没数据的 0"**,不改打分。

---

## 一 目标与非目标

### 目标
1. **date-pin 采集/读取**:情绪三层按当日交易日锁定读取,禁用"latest 静默回退到旧数据",或让回退**可识别**(带新鲜度戳)。
2. **新鲜度字段(contract-first)**:每条情绪记录补 `采集日期` / `新鲜度`(分层),消费侧能识别陈旧并自主失效/降权。
3. **契约先行**:先改 `record.schema.json` + `contracts/record.py` 校验 + 文档,再改生产代码;旧记录零改动仍能通过校验。
4. **无未来函数 / 口径一致**:date-pin 后每日跑与回测读取口径一致,历史记录不被回填污染。

### 非目标(本轮不做)
- 不改三层加权算法(新闻0.5/政策0.3/舆情0.2)、不改 `净情绪分` 数值口径。
- 不动稳定消费接口字段(见 §四.1)。
- 不消除结构性零值(政策行业命中率低是覆盖问题,单独排期),只标注。
- 不引入"陈旧则重采"的自动补采(采集调度是另一模块),只做"读到陈旧能识别"。

---

## 二 稳定消费接口(冻结,不能动)

核实所有 `sentiment` 块消费方,以下字段**语义与位置冻结**:

| 字段 | 消费方 | 代码位置 |
|---|---|---|
| `sentiment.净情绪分`(−1~1) | 买卖倾向、合议、情绪专家 | `predict.py:155`、`council.py:155`、`experts.py:136` |
| `sentiment.样本数` | 同上(样本0不计分) | `predict.py:156`、`council.py:156`、`experts.py`附近 |
| `sentiment.利好数` / `利空数` | web 情绪面、公告情绪温度 | `stock.html`、`screen.html` |
| `sentiment.口径` | web 情绪面副标题 | `stock.html:230` |
| `sentiment.三层.{新闻,舆情,政策}.{净情绪,样本数,多空,degraded}` | web 三层表 | `stock.html:234-244` |
| `sentiment.events[]{影响方向,与本股关系,层,标题,time}` | web 事件列表 + 契约校验 | `stock.html:252`、`record.py:130-136` |

新增字段**一律增量并列**,不改上述任何键的名字/类型/含义。旧消费方读不到新字段时行为不变(向后兼容)。

---

## 三 功能点

### A. date-pin 采集与读取

**A-1 读取入口补 date 形参(透传到 store)**
- `news.load_news(code, date=None)` / `ugc.load_ugc(code, date=None)`:新增 date,透传 `store.get_raw(..., date=date)`。缺省 `None`→保持现状"latest"(向后兼容),显式传日期即锁定。
- `event.analyze_stock(code, ..., date=None)`、`extract_news_events(code, ..., date=None)`、`ugc_sentiment(code, ..., date=None)`、`score_policy(date=None)`(已有)、`_stock_policy_items`:把 date 一路透传到三层读取。
- `event.py` 内 date 缺省取 `store.active_date()`(编排入口已 `set_active_date(as_of)`,见 `run.py:346` 等),使"当日跑"自动锁当日;回测复算传历史日。

**A-2 禁用/标记"latest 回退到旧数据"**(**决策点,见 §六**)
两个候选,推荐 A2:

- **A1 严格锁定**:date 锁定后该日无 raw → 该层直接按"无数据"处理(样本数=0、`新鲜度=无数据`),**绝不回退旧数据**。
  - 优:彻底杜绝陈旧复用与雷同;口径最干净。
  - 劣:某票当天没重采就情绪空窗,覆盖率下降(尤其全A非每日重采新闻/UGC)。

- **A2 可识别回退(推荐,默认)**:date 锁定优先读当日;当日缺失时**允许在有限窗口(`max_stale_days`,建议 3 个交易日)内回退到最近的旧 raw**,但**必须**把 `新鲜度=陈旧` + `采集日期=实际那份旧 raw 的日期`写进记录,交消费侧决定失效/降权。超窗 → `新鲜度=无数据`,不再回退。
  - 优:保留覆盖率与历史面板连续性;陈旧不再"静默",消费侧可自主降权(如陈旧层权重×0 或×0.5)。
  - 劣:仍可能读到旧数据,但**可识别**——由 §四 字段兜住。

实现层面:在 `store.get_raw` 上加 `store.get_raw_resolved(kind, code, date)`(或 event 侧封装),返回 `(payload, resolved_date, fetched_at)`,把"实际读到哪一天"暴露给引擎,不再是黑盒。`resolved_date != 锁定日` 即判陈旧。

### B. 新鲜度字段(见 §四 字段契约)

每条情绪记录 + 每层补 `采集日期` / `新鲜度`,顶层 `新鲜度` 由三层取"最坏"聚合。

### C. contract-first 落地
- 先改 `record.schema.json` 的 `sentiment` 契约字符串 + `contracts/record.py` 校验(新增 `新鲜度` 枚举、`采集日期` 日期校验),**新字段全部 optional**,旧记录不触发校验错。
- 校验通过后再改 `event.py` 写入 + 采集侧 date-pin。

---

## 四 字段契约(草案,与策略提供者对齐用)

### 4.1 冻结不动(重申)
`净情绪分` / `样本数` / `利好数` / `利空数` / `口径` / `三层.*.{净情绪,样本数,多空,degraded,利好数,利空数}` / `events[]`——**位置、键名、类型、含义全部不变**。

### 4.2 新增字段(增量并列)

顶层 `sentiment` 与每层 `三层.{新闻,舆情,政策}` 各补:

```jsonc
{
  "sentiment": {
    // —— 冻结字段(原样保留)——
    "净情绪分": -1..1,
    "利好数": int, "利空数": int, "样本数": int,
    "口径": "三层加权 新闻0.5/政策0.3/舆情0.2,缺层重归一",
    "events": [ /* 原样 */ ],

    // —— 新增(顶层聚合)——
    "采集日期": "YYYY-MM-DD | null",   // 本票本次情绪所依据原始数据的实际采集日;
                                        //   三层取"最旧"的一层(最保守);全层无数据→null
    "新鲜度": "新鲜 | 陈旧 | 无数据",   // 三层聚合:任一层陈旧→陈旧;全无数据→无数据;否则新鲜
    "锁定日期": "YYYY-MM-DD | null",   // 本次运行锁定的交易日(active_date);回测复算=历史日
                                        //   诊断用:锁定日期 vs 采集日期 不等即回退发生

    "三层": {
      "新闻": {
        "净情绪": -1..1, "样本数": int, "利好数": int, "利空数": int,  // 冻结
        "采集日期": "YYYY-MM-DD | null",   // 该层原始 raw 的实际采集日(取自 meta.fetched_at 的日期)
        "新鲜度": "新鲜 | 陈旧 | 无数据"     // 该层单独判定
      },
      "舆情": {
        "净情绪": -1..1, "多空": "...", "样本数": int, "degraded": "...(可选)",  // 冻结
        "采集日期": "YYYY-MM-DD | null",
        "新鲜度": "新鲜 | 陈旧 | 无数据"
      },
      "政策": {
        "净情绪": -1..1, "样本数": int,   // 冻结
        "采集日期": "YYYY-MM-DD | null",   // 政策按 policy_{date} 聚合,采集日=该 date
        "新鲜度": "新鲜 | 陈旧 | 无数据"
      }
    }
  }
}
```

### 4.3 设计决策与理由

**(1) 枚举 `新鲜度` vs 布尔 `is_stale` — 选枚举(三态)。**
状态有三种且消费语义不同:
- `新鲜`:当日锁定日采到 → 正常计分。
- `陈旧`:回退到旧 raw(A2 场景) → 消费侧可降权/失效,但**数据存在**。
- `无数据`:该层从来没采到(结构性零值) → 应与"真中性 0.0"区分,消费侧应**排除**而非当中性。

布尔 `is_stale` 会把 `陈旧` 与 `无数据` 混为一谈,而这两者消费动作不同(降权 vs 排除)。故用三态枚举。
(若统筹坚持要布尔便利位,可**额外派生** `is_stale = 新鲜度 != "新鲜"` 作只读便利字段,但以枚举为权威。)

**(2) 分层 as_of vs 单一 as_of — 选分层 + 顶层聚合。**
新闻/舆情/政策**独立采集、独立陈旧**(如新闻当天采了、UGC 三天没动、政策一周一更)。只给顶层一个 `采集日期` 会丢失"到底哪层陈旧"的信息,而本轮的核心诉求正是"识别哪个输入陈旧"。故每层各带 `采集日期`+`新鲜度`;顶层再给一个聚合值(最坏口径)供快速判断与 web 徽标。

**(3) 顶层聚合口径 — 最坏优先。**
`采集日期` = 三层中最旧的一层日期(最保守,代表整块最陈旧的部分);`新鲜度` = 任一层陈旧则陈旧、全部无数据才无数据、否则新鲜。理由:情绪块作为一个整体被合议/买卖倾向消费,应以"最不新鲜的成分"提示风险,避免"新闻新鲜"掩盖"舆情陈旧"。

**(4) `锁定日期` 的必要性。**
`锁定日期`(active_date)+`采集日期`两者并存,使"回退是否发生"一目了然(不等即回退),也让回测复算可自证锁的是历史哪天。诊断/审计用,消费侧可忽略。

### 4.4 新鲜度判定规则(写入侧)

对每层,基于 §0.3 已有基建:
- 取该层实际读到的 raw 的 `resolved_date` 与 `meta.fetched_at`。
- `无数据`:该层 date-pin 后无任何可用 raw(A1 缺当日 / A2 超窗)。
- `陈旧`:`resolved_date < 锁定日期`(A2 回退发生);或(可选加严)`raw_age_days > max_stale_days`。
- `新鲜`:`resolved_date == 锁定日期`。
- 政策层特例:政策按 `policy_{锁定日期}` 聚合,缺当日政策文件即 `无数据`;命中本票行业条数=0 但政策文件是当日的 → `新鲜` 但 `样本数=0`(区分"没政策文件"与"有政策文件但没命中本行业")。

---

## 五 实现计划(先框架后填充,约法5;分步 commit,约法9)

### 涉及模块表

| 阶段 | 模块/文件 | 改动 | 类型 |
|---|---|---|---|
| P0 契约 | `tools/contracts/record.schema.json` | `sentiment` 契约串补新字段 + 新增 `新鲜度` 枚举 | 契约 |
| P0 契约 | `tools/contracts/record.py` | 校验 `新鲜度`∈枚举、`采集日期`/`锁定日期` 日期格式;全 optional | 校验 |
| P0 契约 | `docs/信息流转与层职责.md`(如涉及)/ 本文档 | 字段说明 | 文档 |
| P1 store | `tools/store/repo.py` | 新增 `get_raw_resolved(kind,code,date)`→`(payload,resolved_date,fetched_at)`;不改 `get_raw` 签名(向后兼容) | 基建 |
| P2 采集 | `tools/collectors/news.py` / `ugc.py` | `load_news/load_ugc` 补 `date=None` 透传 | 采集 |
| P2 采集 | `tools/collectors/policy.py` | `load_policy` 已有 date,确认 date-pin 语义 | 采集 |
| P3 引擎 | `tools/analysis/event.py` | `analyze_stock/extract_news_events/ugc_sentiment` 补 date;读 resolved_date+meta→写三层与顶层 `采集日期/新鲜度/锁定日期`;A1/A2 策略开关 | 引擎 |
| P3 编排 | `tools/run.py` | `run_sentiment` 把 as_of/active_date 传入 `analyze_stock`;日志加新鲜度统计 | 编排 |
| P4 序列化 | `tools/analysis/serialize.py:84` | `sentiment = {**srec.sentiment, ...}` 已用 spread,新字段自动带出;确认无字段裁剪 | 组装 |
| P5 展示(可选,非阻断) | `web/templates/stock.html` | 三层表加"新鲜度/采集日期"列或徽标;陈旧标灰 | 展示 |
| P6 测试 | `tests/test_sentiment_freshness.py`(新增) | 见 §七 | 测试 |

### 分期步骤(有依赖,顺序执行)
1. **P0 契约先行**:改 schema + record.py 校验 + 本文档字段契约。跑现有记录校验确认旧记录不报错。commit。
2. **P1 store 基建**:`get_raw_resolved`。单测锁"回退发生时 resolved_date≠锁定日、命中当日时相等"。commit。
3. **P2 采集 date 透传**:load_news/load_ugc 补 date。commit。
4. **P3 引擎写入**:event.py 读 resolved_date/meta → 写新鲜度字段;A2 有限窗回退 + 陈旧戳;A1/A2 由 settings 开关(默认 A2)。commit。
5. **P4 编排贯通**:run.py 传 active_date;日志统计"新鲜/陈旧/无数据"三态占比(验收观测点)。commit。
6. **P5 web**(可选,可另起小 PR):三层表展示新鲜度。commit。
7. **P6 测试**:补断言锁语义。commit。
8. 全绿 → merge main(约法:独立 worktree 合并)。

---

## 六 需统筹 / 用户拍板的决策点

1. **A1 严格锁定 vs A2 可识别回退(默认口径)** — 推荐 **A2**(保覆盖率 + 陈旧可识别),`max_stale_days` 建议 **3 个交易日**。若统筹更看重"绝不读旧数据的纯净口径",则选 A1。**这决定 event.py 的核心行为,须先定。**
2. **`max_stale_days` 窗口取值**(A2 下):3 交易日?5?超窗即 `无数据`。
3. **消费侧是否本轮就动**:本轮契约只"让陈旧可识别",**是否顺带让合议/买卖倾向对 `陈旧`/`无数据` 降权或排除**?建议**本轮不改消费打分逻辑**(只落字段),消费侧降权另开一轮(避免同时动采集+打分,回测口径难归因)。请确认。
4. **是否额外派生只读 `is_stale` 布尔**(便利位,枚举为权威)——看 web/下游偏好。
5. **web 展示(P5)是否纳入本轮**——可拆为独立小 PR,不阻断采集侧治理。
6. **历史记录回填**:旧日期记录无新鲜度字段,是否需要脚本回填(基于历史 meta 反推)?建议**不回填**(历史 as_of 记录冻结,新字段只对新跑生效,`null` 兼容);仅新记录带新鲜度。请确认。

---

## 七 验收 / 测试标准(锁"为什么改",约法6)

新增 `tests/test_sentiment_freshness.py`,断言锁死以下语义(防未来重写误删):

**锁 date-pin(禁/标 latest 回退):**
1. **回退不再静默**:构造 `raw/T-1/news/X` 存在、`raw/T/news/X` 缺失,以 `date=T` 跑;A1 断言该层 `样本数=0 且 新鲜度=无数据`(不读到 T-1 内容);A2 断言 `新鲜度=陈旧 且 采集日期=T-1 且 锁定日期=T`。**核心回归**。
2. **当日新鲜命中**:`raw/T/news/X` 存在,`date=T` 跑 → 该层 `新鲜度=新鲜 且 采集日期=T`。
3. **超窗不回退**(A2):`raw/T-10/news/X`、窗口=3,`date=T` → `新鲜度=无数据`,不读 T-10。

**锁陈旧能被标出:**
4. 三层独立陈旧:新闻新鲜、UGC 陈旧、政策无数据 → 各层新鲜度正确;顶层 `新鲜度=陈旧`(最坏聚合)、顶层 `采集日期` = 最旧层日期。
5. `无数据` 与 `真中性 0.0` 可区分:无 raw 层 `新鲜度=无数据 且 样本数=0`;有 raw 但净情绪算出 0.0 的层 `新鲜度=新鲜 且 样本数>0`。

**锁契约兼容:**
6. `record.py` 校验:带新字段的记录 `is_valid=True`;**旧记录(无新鲜度字段)仍 `is_valid=True`**(向后兼容)。
7. 非法 `新鲜度`(枚举外值)→ 校验报错。

**锁稳定接口不动:**
8. `净情绪分`/`样本数`/`利好数`/`利空数`/`口径`/`三层.*.净情绪` 的键名、类型、数值口径与改动前逐字节一致(用一份 fixture 记录跑前后对比冻结字段)。

**观测验收(非单测,跑一轮全A/自选池看日志):**
- `run_sentiment` 日志出现"新鲜/陈旧/无数据"三态占比;陈旧+无数据占比应可解释先前"雷同 28%"的来源。
- 抽查连续两日:此前字节雷同的票,现应带 `新鲜度=陈旧` 戳(A2)或变空(A1),不再"看起来像新数据"。

---

## 八 工作量估(agent 视角,约法11)

| 阶段 | Token(in+out 量级) | agent 工时(含工具往返+审阅来回) |
|---|---|---|
| P0 契约(schema+record.py+文档) | ~30–50K | 0.5–1h |
| P1 store `get_raw_resolved`+单测 | ~30K | 0.5h |
| P2 采集 date 透传 | ~20K | 0.3h |
| P3 引擎写入(核心,A2 回退+新鲜度) | ~60–90K | 1–1.5h |
| P4 编排+日志 | ~20K | 0.3h |
| P5 web(可选,可拆) | ~30K | 0.5h |
| P6 测试(8 条断言) | ~40–60K | 0.5–1h |
| **合计** | **~230–300K** | **~4–5h**(不含用户审阅决策点等待) |

均为 Claude subagent 自做(本项目调研/批量操作不外包,依记忆约定)。P0–P4 为一条依赖链(顺序),P5/P6 可与主线部分并行(P5 独立 PR、P6 随各阶段增量补)。
