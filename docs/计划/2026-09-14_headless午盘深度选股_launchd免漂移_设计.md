# Headless 午盘深度选股 · launchd 免漂移（设计 / 计划）

> 日期：2026-09-14　｜　线：headless 午盘深度分析·launchd 免漂移（方案 b）　｜　统筹：stock_analysis 统筹窗口（回声-0914）
> 分支：`feat/headless-noon-deep`（基于 origin/main `afd660a`，已核 == 当前 origin/main）
> ⚠️ 测试环境研究模拟，非投资建议。
> **本文是第一 commit（约法4：先计划后实现）。出后回执统筹 review，greenlight 前不实现、绝不自换 live SKILL/调度、绝不自合 main。**

---

## 0. 一句话目标 + 交付物

把午盘逐票深度选股从**「Claude 定时任务窗口驱动（桌面 App 非活动时漂移数小时，撞 13:00 硬 deadline）」**改造成
**「纯 Python orchestrator 经 launchd 触发、headless 调 DeepSeek 逐票深度研判」**——不依赖任何 Claude 窗口活着、免漂移、准点在 13:00 开盘前产出。

**交付物**：
1. **本设计文档**（第一 commit）。
2. **orchestrator**：`tools/pipeline/intraday_deep.py`——串 `候选(stage1) → 自采Top-N消息面 → deep_analysis(DeepSeek) → write_picks(JSON) → 渲染 日内_<date>.md`。
3. **launchd job 草案（repo 内留痕）**：`ops/launchd/intraday_deep.sh`（wrapper）+ `ops/launchd/com.stock.intraday_deep.plist`。
4. **测试**（约法6，锁语义）：防未来 / 13:00 预算 / 候选消费 / 产物规格 / headless 不依赖窗口。
5. **headless 端到端手跑样例**（scratchpad 隔离，`--data-root` 只读生产）+ 时间/token 实测回填。

**不做**：真正 `launchctl load`（用户）、live SKILL/调度切换（统筹）、动现有午盘任务配置。本线只产 headless 跑通产物 + job 草案。

---

## 1. 现状核查结论（已读代码/设计/产物）

### 1.1 四件现成件核对（无误、不重造）
| 件 | 位置 | 结论 |
|---|---|---|
| headless 逐票研判引擎 | `tools/analysis/deep_analysis.py` | ✅ 吃 `--codes`，经 `get_client(deep_analysis)`/`get_client_for(provider)` 路由 DeepSeek，产 write_picks 兼容 unit。**就是我的深度引擎。** |
| 阶段1 早产候选 | `intraday_screen.py::read_noon_candidates(stage="stage1")` + `noon_candidates_stage1.json` | ✅ `--read-candidates --stage stage1` 门控就绪即返回，退出码 0=就绪/2=未就绪。 |
| 午盘深度规格参照 | `docs/参考/午盘选股SKILL草案_..._v2.md` | ✅ 逐票 9 必填结构（定性/数据/消息面/交叉/深度含α-β/D-0退出/盯点单问/PICKS 锚点）。搬到 headless 产出。 |
| launchd 模式 | `ops/launchd/intraday_screen.sh` + plist + `_bootstrap.sh` + `lib/lock.sh` | ✅ 两层（bootstrap deploy worktree → 真 wrapper dailyjob worktree）+ 锁 + 日志 + **LLM 网关 env 注入块**。照它加。 |

### 1.2 关键发现：**端到端 orchestrator 缺失**（这就是我要建的）
`deep_analysis.generate() → units → write_picks → 最终选股 md` **无任何 live 代码串联**——目前由 Claude 窗口手工完成。可复用的纯函数全齐（都已单测、可注入）：
- 候选：`intraday_screen.read_noon_candidates(as_of, root=, top=, stage="stage1")`
- 深度：`deep_analysis.generate(pick_date, codes, provider_id=, data_root=, strategies_hint_map=, ...)` → `units_of()`
- 装配/校验/落 JSON：`write_picks.build_picks_json()` → `write_picks.validate_picks(doc, picks_anchor=)` → `write_picks.write_picks()`（落 `data/analysis/<date>/每日选股.json`）
- 买入表 + 锚点渲染：`write_picks.render_picks_md_tables(doc)` / `picks_anchor_line(doc)`
- D-0 交易计划：`intraday_screen.compute_trade_plan(code, quote, as_of)`（进场/止损/止盈/波动锚，纯函数）
- Top-N 消息面自采：`tools.run message`（news）+ `tools.run sentiment`（三层情绪，per-stock）+ `tools.run events`

**唯一缺的是把这些串起来的 orchestrator + 渲染逐票深度叙事的 md renderer。** 这就是本线的核心建设。

### 1.3 深度对齐盘后的既有证据
P2 验证报告（`docs/计划/2026-09-13_headless研判_P2验证报告.md`）：deep_analysis（DeepSeek，think 关）对 2026-09-11 真实候选跑 → 与 Claude `选股/2026-09-11.md` 买入排序表对比，**表态叙事质量接近 Claude、且主动引用经验库教训**（002913 命中经验#2、300124 命中#5），边界票谨慎度差 ±1 档。→ **深度侧「基本对齐」有实证底**，缺口见 §5。

---

## 2. 架构定位

本 orchestrator 属**编排层**（架构设计 §3 的 `run.py` 薄壳同级），复用下层已建纯函数，不新增业务判定：

```
launchd com.stock.intraday_deep (工作日 ~11:50)
   └─ bootstrap(deploy wt, reset origin/main) → intraday_deep.sh(dailyjob wt, 注入 LLM 网关 env)
        └─ python -m tools.run intraday_deep --date <today>
             ├─ 0. 交易日门控 cal.is_trading_day → 非交易日 exit 0
             ├─ 1. 候选门控：read_noon_candidates(stage1) 轮询就绪(≤12:20)→ Top-N codes(数据面综合分排序)
             ├─ 2. 自采 Top-N 消息面(≤11:30 防未来 cutoff)：message + sentiment + events
             ├─ 3. deep_analysis.generate(codes, provider=DeepSeek) → units_of()
             ├─ 4. compute_trade_plan(code, 11:30快照) → 每票 D-0 计划
             ├─ 5. write_picks.build_picks_json → validate → write_picks(每日选股.json)
             └─ 6. 渲染 日内_<date>.md(PICKS锚点 + 市场环境 + 逐票深度 + 买入排序表 + D-0计划)
```

**关键：全程无 Claude 窗口。** 这既是免漂移的根本（launchd + 纯 Python），也对齐「优先千问/DeepSeek 谨慎境外模型」方向。

---

## 3. 设计决策（请统筹审 / 必要时报用户）

### D1 · orchestrator 形态 = 纯 Python（**不是** headless Claude 窗口）
午盘改造设计 §4.3 的 (b) 原写「launchd 触发 headless **Claude**」；本线按任务口径细化为**纯 Python orchestrator 直调 DeepSeek**（deep_analysis 已具备）。
- **优**：无窗口依赖、无 prompt 漂移、可单测、可复现、省 Claude 额度；深度引擎已验证 ≈Claude。
- **劣/缺口**：DeepSeek 固定 schema 抽取，无 Claude 的自由 WebSearch/长经验库全文推理（缺口与补强见 §5）。
- **推荐 D1**：纯 Python。这是本方案的核心价值（免漂移 + 模型口径），且复用度最高。

### D2 · 候选消费 = **阶段1 早产**、按数据面综合分排序、Top-N
- 门控 `--read-candidates --stage stage1`（~11:50 就绪），**不等全量**（阶段2 ~12:42 卡不进 13:00）——统筹已确认这是赶 13:00 的关键。
- stage1 消息面字段弃权 → **排序键 = `数据面综合分`**；取 Top-N（N 见 D6）逐票深挖。
- 未就绪（退出码 2）→ **有界轮询**每 3min 重探、上限 ~12:20；≤12:20 仍无 → 降级读 `日内全A_<date>.md` 台账（`stage=auto` 回退），显式标注降级来源；仍无 → 产 `<!-- PICKS: none -->` 跳过留痕，**绝不空跑/抢用隔日候选**。

### D3 · Top-N 消息面 = orchestrator 自采（**≤11:30 防未来 cutoff**）
stage1 是 `no_llm`（无消息面回灌）；deep_analysis 消费 `news_ai/<code>.json`+`sentiment/<code>.json`，若缺 → `sentiment_quality=missing` → 情绪盲区闸门封顶（不得「买入·首选」）。为对齐盘后深度：
- orchestrator 对 **Top-N（仅 ~5 只）** 跑 `tools.run message`+`sentiment`+`events`，解耦阶段2 全量 batch（~50-85 只、~50min）。5 只有界、可并发（sentiment workers），耗时可控（见 §4）。
- **防未来（铁律②）**：news 采集须**时间钳制到 ≤11:30 冻结**——`deep_analysis_inputs.load_news` 仅按 date 粒度剔除（不足以挡 11:31-11:50 的盘中新闻）。orchestrator 须在采集/切片处强制 `news_asof ≤ <date> 11:30:00`（P2 手跑已用此 cutoff）。**这是本线防未来的头号落点，进测试锁死。**
- **备选**：不自采、直接吃磁盘现有档 → 简单但 Top-N 多为 missing 情绪、深度打折。**否决**（违背对齐盘后）。

### D4 · md 产出 = 新叙事 renderer + 复用 write_picks 表/锚点；双落盘
`render_picks_md_tables` 只渲染 PICKS 锚点 + 买入排序表，**不含逐票深度叙事**。故 orchestrator 加一个薄 renderer `render_noon_deep_md(doc, results, trade_plans, market_ctx)`：
- **第一行**（硬）：`<!-- PICKS: 买入票6位代码,... -->`（= `picks_anchor_line(doc)`，保 `intraday_watch`）；跳过 → `<!-- PICKS: none -->`。
- **开头**：数据时点（11:30 冻结）+ 范围（全A午盘候选池 Top-N，非盯盘集/自选）+ 非投资建议 + **时点自证三时刻**（触发/执行 `date +%H:%M`/采集）+ 相对 13:00 漂移标注（launchd 下应恒 <13:00，但仍自证留痕）。
- **正文**：市场环境（读 market_forecast）→ **逐票深度**（每票从 unit 渲染：type/stance/stance_qualifier/dir_1d+conf/dir_5d+conf/sentiment_quality/key_reason/key_risk/alpha_beta/watch_points + 数据层要点 + D-0 计划）→ **今日可买入排序表**（复用 `render_picks_md_tables`）→ 遇到的问题。
- 逐票深度叙事字段**全部来自 units（deep_analysis 已产），无额外 LLM 调用**。
- **同时**落 canonical `data/analysis/<date>/每日选股.json`（`write_picks`，机读、下游 web/复盘/回测复用），与 md 同源（`--picks-anchor` 断言两者选票一致）。
- **写哪个文件名**：沿用 `日内_<date>.md`（承接 noon SKILL 语义位）。PICKS 语义 = 全A午盘买入票 → 下午 `intraday_watch` 监控对象随之变（**期望行为**，换线时统筹连 watch 一起核，同午盘改造设计 Q1）。

### D5 · D-0 交易计划 = 复用 `compute_trade_plan`
每只买入候选调 `intraday_screen.compute_trade_plan(code, quote(11:30快照), as_of)` → 进场触发/止损位(距离%)/止盈位(距离%)/波动锚(ATR14→半日振幅→缺省)/了结纪律（当日收盘无条件平仓 + 盘中触止损止盈即走 + 次日跳空破止损开盘走）。渲染进 md D-0 表。**quote 源 = `noon_screen_snapshot.json`（11:30 冻结，close 不覆盖）**。

### D6 · 模型口径 = DeepSeek（think 关起步），Top-N = 5
- 主 provider 走 `get_client("deep_analysis")`（注册表主 provider，DeepSeek 网关）；think 关（P2 双跑证近端不改结论，5 日边界票才分歧，省耗时）。可 `--provider`/`--think` 覆盖联调。
- **千问臂当前不可跑**（本机仅配 `LLM_*` DeepSeek 网关，未配 `QWEN_*`，P2 已记）→ 口径先 DeepSeek，待千问网关就绪可切 provider（deep_analysis 已支持）。
- **Top-N = 5**（对齐盘后 3-5 只 + 时间预算）。可 `--top` 调。

### D7 · 与现有午盘任务并存 / 替代
| 任务 | 触发 | 产物 | 本线关系 |
|---|---|---|---|
| `com.stock.intraday_noon`（snapshot） | 11:31 | 11:30 全A冻结快照 | **上游依赖**（我读它的 `noon_screen_snapshot.json`） |
| `com.stock.intraday_screen`（选股台账） | 11:32 | `日内全A_<date>.md` + `noon_candidates_stage1.json` | **上游依赖**（我读它的 stage1 候选） |
| `daily-stock-noon-analysis`（Claude SKILL） | 午休 ~11:45 | `日内_<date>.md`（盯盘集深度） | **本线替代对象**——我产同名 `日内_<date>.md`（全A候选池深度） |
| **`com.stock.intraday_deep`（本线，新）** | **~11:50** | `日内_<date>.md` + `每日选股.json` | 新增 |
- **并存 vs 替代**：本线 headless 产 `日内_<date>.md` 与 Claude noon SKILL 产 `日内_<date>.md` **二选一**（同名，不能同时写）。**推荐替代**：换线时统筹停用/改造 Claude noon SKILL，改由 launchd headless 产出。切换是统筹/用户动作（本线只出 job 草案 + headless 跑通证据，不动 live 调度）。
- **过渡期并存方案**（可选）：headless 先写 `日内深度_<date>.md`（新名）与 Claude `日内_` 并跑对比一段，稳后再抢 `日内_` 名 + 接 watch。**默认走推荐替代**，是否要过渡期并跑请统筹拍。

---

## 4. 时间预算 + 13:00 可行性（review 重点①）

### 4.1 时间线（launchd 免漂移）
| 阶段 | 起止 | 耗时 | 依据 |
|---|---|---|---|
| 上游 stage1 候选就绪 | 11:32→~11:50 | ~18min | intraday_screen 11:32 触发 + no_llm 阶段1（**~11:50 为设计推算，未实测，见校准 TODO**） |
| 本 job launchd 触发 | ~11:50 | — | plist 排在 screen 后 |
| 候选门控读取 | | <1s | P2 手跑 read-candidates <1s |
| **Top-N 消息面自采（5 只）** | | ~1-4min | message(news 采集，P2 5 只 ~2s)+ **sentiment（LLM，主耗时，5 只并发）** + events；LLM 情绪单票网络往返，5 只并发估 ~1-3min |
| **deep_analysis（5 只 DeepSeek）** | | **~2-6min（待实测）** | 5 只顺序 extract；单只 DeepSeek 往返估 ~30-70s。**这是 13:00 预算最大未知量，§10 手跑实测校准** |
| write_picks + 渲染 md | | <2s | 纯本地 |
| **合计（~11:50 起）** | | **~5-12min** | → **~11:55-12:05 出**，赶得上 13:00，余量充足 |

### 4.2 token 估
- input：经验 snippets（top_k=8，非全文）+ Top-N per-stock facts + market_forecast + news_ai/sentiment ≈ 每只 ~5-15K，5 只 + 情绪 LLM ≈ **~50-120K**（比 Claude 窗口的 ~150-300K 省，因经验只喂 snippets 非全文）。
- output：5 只结构化 unit + 情绪打分 ≈ **~5-15K**。单轮量级，成本低（DeepSeek 网关不烧 Bedrock）。

### 4.3 13:00 可行性结论（**诚实**）
- **候选源侧**：launchd 免漂移，stage1 ~11:50 就绪（**待实测校准**）。
- **深度侧**：全 headless、无窗口漂移；~5-12min 端到端 → **~12:05 前出，稳赶 13:00**。
- **头号残余风险**：**deep_analysis 5 只 DeepSeek 实际时延未实测**——若单只 >2min（网关慢/重试），5 只可能逼近 10min，仍卡进但余量收窄。§10 手跑实测；若超预算，缓解：降 N=3 / sentiment+deep 并发 / think 关（已默认）。
- **对比 Claude 窗口方案**：Claude 定时任务漂移是**结构性**（桌面 App 挂起数小时，09-03 曾漂 3h16m）→ 无论多快都保证不了 13:00。本 headless 方案从根上消除漂移，这是核心卖点。

---

## 5. DeepSeek 深度对齐盘后规格的缺口 + 补强（review 重点②，诚实）

| 维度 | 盘后 Claude 母本 | headless DeepSeek | 缺口 | 补强 |
|---|---|---|---|---|
| 逐票 9 必填结构 | ✅ | ✅（DEEP_ANALYSIS_SCHEMA 固化 9 字段） | 无 | — |
| 经验库引用 | 读最新版全文 | 读 top_k=8 关键词检索 snippets | 检索可能漏条目 | top_k 可调；关键经验（盘中不盖棺#17/#24、跳空#20、超跌闸门#31）已进 prompt 硬红线 |
| 消息面检索 | baidu_news + **自由 WebSearch** | message 采集器（baidu_news 等），**无自由 WebSearch** | 突发/长尾催化可能漏 | Top-N 自采 news_ai + 三层情绪；缺口如实标 sentiment_quality；后续可挂 WebSearch MCP |
| 数据层深度 | 读 per-stock json（BIAS/RSI/chip/fundflow） | 同（di.assemble 读 record） | **需 noon 注入 per-stock record 就绪**（校准 TODO） | 手跑核实 noon per-stock json 覆盖；缺档标「未取数」不猜 |
| 方向盖棺纪律 | 妖股不盖棺 | ✅（prompt 硬红线 + `_coerce_unit` 兜底） | 无 | 枚举越界保守兜底 + 情绪盲区闸门已在 deep_analysis |
| 表态谨慎度 | — | P2 边界票差 ±1 档 | 轻微偏差 | think 关起步；边界票倾向更谨慎（保守兜底），可接受 |

**结论**：深度**基本对齐**（P2 实证 ≈Claude，9 必填 + α-β + D-0 + 盯点单问齐全），主要缺口是**无自由 WebSearch 检索** + **经验只喂 snippets**——对午盘（时间紧、重数据面 + 结构化消息面）**可接受**，缺口在产物里如实标注（sentiment_quality、经验命中），不硬凑。后续增强路径：挂 WebSearch MCP 给 headless、经验检索升级。**这是「基本对齐、缺口透明」而非「完全等价」，请统筹据此判是否 greenlight 替代。**

---

## 6. launchd job 设计（草案，repo 留痕）

照 `intraday_screen` 派系（bootstrap → 真 wrapper），**新增不改现有**：

### 6.1 `ops/launchd/com.stock.intraday_deep.plist`
- Label `com.stock.intraday_deep`；`ProgramArguments = [/bin/bash, ~/.local/state/stock/bin/stock-launchd-bootstrap.sh, intraday_deep.sh]`。
- `StartCalendarInterval`：Weekday 1-5，**Hour 11 / Minute 50**（排在 screen 11:32 后，等 stage1 就绪；实测校准后可调）。`RunAtLoad=false`。
- `StandardOutPath/ErrorPath`：`~/.local/state/stock/intraday_deep.{out,err}.log`。

### 6.2 `ops/launchd/intraday_deep.sh`（wrapper）
照 `intraday_screen.sh` 抄，改命令：
1. **LLM 网关 env 注入**（**逐字复制** `intraday_screen.sh:14-21`）：source `$HOME/.config/stock/sync.env` → `LLM_API_KEY` 空则 `/bin/zsh -ic` 兜底解析 `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL`。**headless 拿 DeepSeek 网关凭证的唯一途径。**
2. **worktree 卫生**：复用 dailyjob worktree（`$HOME/Documents/projects/worktrees/stock_analysis/dailyjob`），`fetch + reset --hard origin/main`（与 screen 共享同一 dailyjob → 读同一 `data/intraday/<date>/` 候选）。
3. python = `$HOME/.conda/envs/stock_analysis/bin/python`（不 activate）。
4. 日志 `~/.local/state/stock/intraday_deep.log`；锁 `lib/lock.sh::acquire_lock_or_exit`（`intraday_deep.lock`）；`caffeinate -i -m -s -w "$$"`。
5. 调用：`$PY -m tools.run intraday_deep "$@"`（走 tools.run 分发器；新增 `cmd_intraday_deep` → `intraday_deep._main`）。

### 6.3 部署（换线，用户/统筹动作，非本线）
`provision_deploy.sh` 装 plist + bootstrap；用户 `launchctl load` 加载。本线只在 repo 放 plist/wrapper 草案 + 在设计里列部署步骤。

---

## 7. 防未来铁律落点（铁律②，进测试锁死）

1. **数据只用 ≤11:30 冻结**：quote=`noon_screen_snapshot.json`（11:30 冻结价）；候选=stage1（11:30 语境）。
2. **消息面 ≤11:30 时间钳制**（D3 头号落点）：自采 news 强制 `news_asof ≤ <date> 11:30:00`，挡盘中 11:31+ 新闻。
3. **deep_analysis_inputs 已有防未来**：经验版本 ≤pick_date、news_ai/sentiment 剔除 time>pick_date（date 粒度）——**叠加** #2 的 intraday cutoff。
4. **半日 bar 语境**：量能类信号按半日读、不做重量能承诺（prompt 硬红线，deep_analysis 已含）。
5. **妖股/高波动盘中不写方向盖棺**（#17/#24/#27）：deep_analysis prompt + `_coerce_unit` 已保。

---

## 8. 测试计划（约法6，锁「为什么改」语义）

新增 `tests/test_intraday_deep.py`（hermetic，桩 client + tmp_path + 桩候选/快照）：
1. **候选消费**：给桩 stage1 JSON → orchestrator 正确取 Top-N（数据面综合分排序）；退出码 2（未就绪）→ 走轮询/降级分支（可测纯函数），不空跑。
2. **防未来（硬红线）**：桩 news 含 11:31 之后条目 → 断言被 ≤11:30 cutoff 剔除；quote 用 11:30 冻结价。
3. **13:00 预算门控**：orchestrator 有「超预算降级」逻辑（如 N 可降、超时标注）→ 断言降级路径产出仍合规 + 标注。
4. **产物规格**：md 第一行 = PICKS 锚点；逐票含 9 必填 + D-0 计划；`每日选股.json` 过 `validate_picks`（枚举/防未来/情绪盲区闸门/PICKS 一致）；跳过 → `PICKS: none` + status=skipped。
5. **headless 不依赖窗口**：全程注入桩 client、不联网即可跑通（CI 可跑），证明无 Claude 窗口依赖。
6. **不污染**：orchestrator 不改现有 `日内全A_`/候选 JSON/主档（monkeypatch 断言不触发写）。

### 复用现有测试
`test_deep_analysis.py`(23)/`test_write_picks*.py`(24)/`test_model_registry.py` 已锁下层；本线只测 orchestrator 装配 + 防未来 + 产物规格。

---

## 9. 分步 commit 计划（约法9）

1. **本设计文档**（当前）。
2. orchestrator 骨架：`intraday_deep.py`（候选门控 + 装配 + 交易日门控 + `--data-root`/`--top`/`--provider`/`--force` CLI）+ `tools.run` 接线，**先框架后填实现**（约法5）。
3. Top-N 消息面自采 + ≤11:30 防未来 cutoff + 单测。
4. md renderer（逐票深度叙事 + 复用 write_picks 表/锚点）+ D-0 计划接线 + 单测。
5. launchd job 草案（`intraday_deep.sh` + plist）。
6. headless 端到端手跑样例（scratchpad 隔离）+ §4 预算实测回填 + 开发日志。

---

## 10. 端到端验证计划（deliverable ⑤）

- 取最近交易日（09-14），**防未来诚实**手跑：`--data-root <生产 data 只读>`（threaded 进 read_noon_candidates(root=) + deep_analysis(data_root=)）+ `--out-dir scratchpad`（不落生产 docs）。
- 真调 LLM 走 `zsh -ic`（带网关 env）；news 锚定 ≤09-14 11:30。
- **实测**：deep_analysis 5 只 DeepSeek 真实时延（回填 §4.1 最大未知量）+ token + 端到端墙钟 → 证 13:00 可行 + 免漂移（无窗口）+ 深度对齐盘后（产物质量 vs 母本）。
- 产出样例 `scratchpad/日内_2026-09-14.md` + `每日选股.json` 交统筹看。
- **诚实边界**：若 noon per-stock record 未全覆盖 Top-N，数据层字段标「未取数」；样例标「实盘由 intraday_screen noon 注入的 per-stock json 支撑」。

---

## 11. 校准 TODO（换线前须实测，非阻塞设计）

- [ ] **stage1 ~11:50 就绪时刻实测**（当前为 11:32 + ~18min 推算，未硬编码/未实测；同 SKILL 草案 checklist）→ 校准 plist 触发时刻。
- [ ] **noon per-stock record 覆盖**：确认 intraday_screen noon 注入为 Top-N 候选产出 `data/analysis/<date>/<code>.json`（含 signals/chip/fundflow）→ 决定数据层深度实盘可得性。
- [ ] **deep_analysis 5 只 DeepSeek 实际时延**（§4.1/§10 手跑）。

---

## 12. 待统筹拍板 / 未决

- **Q1（D1）**：orchestrator 形态 = 纯 Python 直调 DeepSeek（非 headless Claude 窗口）——确认？（本线推荐，复用度最高、免窗口漂移。）
- **Q2（D3）**：Top-N 消息面**自采**（message+sentiment+events，5 只，≤11:30 cutoff）——确认？还是先吃磁盘现有档（简单但情绪多 missing）？
- **Q3（D4/D7）**：产物写 `日内_<date>.md`（替代 Claude noon SKILL、改 PICKS 语义为全A买入票、影响 watch）——**替代** 还是 **过渡期并跑**（先写 `日内深度_<date>.md` 新名对比一段）？
- **Q4（D6）**：模型口径 DeepSeek + think 关 + Top-N=5——确认？千问网关就绪前先 DeepSeek 可接受？
- **Q5（§5）**：深度「基本对齐、缺口透明（无自由 WebSearch + 经验 snippets）」——是否足以 greenlight 替代？还是要求先挂 WebSearch MCP 补齐再替代？
