# 远端架构 · 方案2 / 方案3 · scoping 与推进建议

> 日期:2026-08-27 · 作者:远端架构实现窗(scoping 阶段)· 状态:**待用户拍板**
> 目的:把方案2(远端可写名单双向对齐,中改动)与方案3(远端完整双向节点,重改动分阶段)两份方案稿的**推进岔口**讲清楚,给推荐 + 收敛出「待用户拍板清单」。本阶段**只读代码 + 读文档 + 写建议,不改任何生产代码**。
>
> 已通读:`远端自选池_方案2_远端可写名单双向对齐.md`、`远端自选池_方案3_远端完整双向节点.md`、`远端数据仓库_双向同步方案.md`、`远端数据仓库部署.md`、`远端自动更新与自愈.md`。
> 关键代码现状已由子 agent 逐条核验(见 §五 代码证据附录),本报告结论均基于真实代码 `文件:行号`,非文档二手引用。

---

## 〇、TL;DR(给赶时间的读者)

1. **先答一个先决问题**:本地 Mac 盘中**经常开着**吗?
   - **常开** → 方案1(本地盯盘,统筹进行中)已覆盖"实时";只在"人在外面想顺手加票"是高频常态时,补**方案2**(中改动、零新增服务)。**方案3 是重复投资,不建议**。
   - **经常不开** → 才值得走**方案3**,但**严格分阶段**,先只做 Phase A(部署已就绪的 Phase 1 采集+pull),收益立现(本地省 ~73min 自采)、风险最小。

2. **两个方案有一个共同的前置阻塞点**,先上机核实、否则两条路都无法落地:
   **远端 39.105.83.174 上 ingest(8802)到底部署了没有、当前数据到底走 git 携带还是签名直入 DB。** 部署记录 A 期/B 期对此**自相矛盾**(见 §四.1),方案2 的回传通道、方案3 的 pull 通道都依赖 ingest 在跑。

3. **方案2 规避两个坑的设计成立**(远端只入队 pending 表、采集/重建全回本地),但有**一处文档未定的落地缺口**:标记 pending "已消化"的回执写路由,ingest 侧**无现成端点可复用**(唯一 POST 是 `/ingest`)。本报告 §二.4 给出推荐落地方式(搭 upload 顺带回执,不新开写口)。

4. **方案3 的 Phase 1(远端自采+本地拉)代码确实已就绪**,主要工作是"真机部署+验证",这部分**我在本机做不了**,需在远端服务器上做、需用户协作(§三)。真·新代码集中在实时报价采集器 + 盘中监控告警(§三.2)。

---

## 一、岔口①:下一步做方案2(中)还是直接上方案3(重)?

### 1.1 两者关系(核验后确认)

方案2 是方案3 的**名单对齐子集**——方案3 文档 §〇 明确"名单对齐可**原样复用**方案2 的 pending 表 + 合并策略",方案2 的工作在方案3 里不作废。但要注意二者**解决的是正交的两件事**:

| | 方案2 | 方案3 Phase A |
|---|---|---|
| 解决什么 | 名单**双向对齐**(远端能加/删自选) | 数据**双向**(远端自采→本地 pull) |
| 复用的通道 | ingest `/pull` 扩 `kind=pool_pending` | ingest `/pull` 现有 `kind=kline` |
| 采集在哪 | **回本地** | **搬远端** |

所以"方案2 能否作为方案3 的 Phase 前置?"——**能,但不是必经前置**。方案2 对齐名单、方案3 Phase A 搬数据,两者可独立推进;方案2 的 pending 表要到方案3 **Phase D**(双向一致性)才被复用。若最终要走方案3 全链,方案2 不算白做;但若只想"远端能加名单",做完方案2 即可收手,不必碰方案3。

### 1.2 决策判据:**本地是否常开机**(这是两份文档一致的分水岭)

- 方案3 文档 §八 与双向同步方案 §四 都点明:方案3 的根本收益是"**本地可以不常开机**,远端自给自足产出常鲜数据 + 盘中盯盘"。**若本地盘中能开着,方案1(本地盯盘)成本低得多,方案3 的远端实时/监控是重复投资。**
- 方案2 的收益边界(方案2 §八):"离开本地机、只在手机/远端网页顺手加自选"成为**常态需求**,且能接受"加完到盘后才见完整数据"的时延。

### 1.3 推荐

**分层决策,不要一步跳到重方案**:

- **路线甲(推荐给"本地常开 + 偶尔在外加票")**:方案1 + 方案2 即止。方案2 用中等改动、零新增服务/端口、全复用采集链路补上"远端加名单",不引入远端采集的运维复杂度。
- **路线乙(推荐给"本地经常不开 + 要常鲜数据/盘中盯盘")**:走方案3,但**严格分阶段、每阶段独立可回滚**:
  1. **先只做 Phase A**(部署已就绪的 Phase 1 采集+pull)——低成本验证"远端自采+本地 pull"闭环,顺带把 §四 的网络可达性 + 远端 unit 实际状态一次查清。做完本地就能省掉 ~73min 自采,收益立现、风险最小。
  2. Phase A 稳了、确有"离开本地也要盯盘"强需求 → 再上 Phase B(实时报价)。
  3. 要主动告警 → 才上 Phase C(监控告警),并先把通知渠道授权/去抖定清楚。
  4. Phase D 名单双向直接复用方案2;产物两段式(远端 data-only)为可选,最后再议。

> **无论走甲还是乙,§四.1 的"远端 ingest 实际部署状态"都必须先上机核实**——它是两条路的共同地基。建议把这一步作为**第 0 步**,先于任何编码。

---

## 二、岔口②:方案2 如何规避「reset 抹改动」与「rebuild 打瘪 panel」两个坑

### 2.1 两个坑的代码证据(已核验,现状确认)

- **坑1 · reset 抹改动**:自选真源是 git 跟踪文件 `config/stock_pool.json`(`stock_pool.py:22`)。远端 autoupdate `merge --ff-only` 失败即**无条件** `reset --hard origin/main`(`remote_update.py:161-164`),且**无任何针对单文件的保护/排除特例**(核验确认)。→ 远端直接写该 JSON,下一轮(≤5min)必被抹。
- **坑2 · rebuild 打瘪 panel**:`add_and_collect`(`pool_service.py:83-95`)→ `collect_one` 联网采集 + `rebuild_artifacts`(`:27-52`)对**全池** codes 跑 serialize/chart/panel,再 `put_view("screen",...)`(`:49`,**整表替换**)。远端无 `data/master`+`data/raw` 积累 → 全池重算只能就零星数据算 → panel 视图被收缩结果整表覆盖。

### 2.2 规避设计(方案2 §三的核心,核验后成立)

**一句话:远端只登记名单增量,采集与重建一律回本地。** 拆成四件:

**(a) pending 表(把名单挪出 reset 火线)** — 推荐**选项 A(DB 表)**,不推荐选项 B(把 JSON 移出 git)。
- 远端加/删票时**不写** `config/stock_pool.json`,改写新 DB 表 `pool_pending(code, name, industry, sector, market, op ENUM(add/remove), source='remote', requested_at, status ENUM(pending/consumed))`。DB 不随 git reset,天然持久。
- 选项 B(移出 git 跟踪)被否的关键代码理由:`stock_pool.py:99-108` 的 `_load()` 在**缺文件时会用 `_SEED` 种子(34 只)重新初始化并落盘**(`:106-108`,导入即执行 `:230`)——名单一旦脱离 git、远端首次缺文件会**悄悄回退到 34 只种子池**,危险。选项 A 无此风险。

**(b) 回传半环(远端→本地,复用 `/pull`,不新增端口/服务)**
- 远端 `ingest.py`:`PULL_KINDS`(现 `:194 = ("kline",)`)扩为 `("kline","pool_pending")`,新增 `_pull_pool_pending()` 读 pending 表返回 `status=pending` 行(只返名单元数据,绝不返密钥/配置)。鉴权/限流/验签/时效**完全复用**现有 `/pull` 门禁(`:314-318`:token 401 → 限流 429 → HMAC 403 → 时效 409)。
- 本地 `pull.py`:现 `kind != "kline"` 直接返回不支持(`:174-175`),扩一个 `kind=pool_pending` 分支 → 落本地缓冲 `data/sync_receipts/pool_pending.json`(与现有水位落盘目录一致)。

**(c) 冲突/合并策略(集合语义,易收敛)**
- **加**:并集(幂等;`add_stock` 对重复 code+market 抛 ValueError(`stock_pool.py:203-204`),本地消化 try/except 跳过)。
- **删**:**时间戳裁决**——pending `op=remove` 且 `requested_at` 晚于本地该票最近一次 add 才删,否则忽略(防"本地刚加、远端旧删记录误删")。依赖两端时钟大致同步(NTP,风险低,需注明)。
- **权威归属**:消化后**本地 `config/stock_pool.json` 恢复为唯一真源**,远端 pending 是**提案队列**、非真源。杜绝双写真源。

**(d) 本地侧消化闭环(全复用现有 pool_service + upload)**
```
① python -m tools.sync.pull --kind pool_pending   # 拉远端待办到本地缓冲
② 逐条 add → pool_service.add_and_collect(...)     # 本地有 raw,panel 不塌
   逐条 remove → pool_service.remove_and_cleanup(...) # 已存在且完整(:98-106)
③ python -m tools.sync.upload --date <today>       # 完整产物+新 panel 推回远端
④ 回执:标记远端 pending 行 consumed
```
坑2 因此被绕开:采集与 `rebuild_artifacts` 发生在**本地**(有 `data/master`+`data/raw`),远端只在下次 ingest 收到完整 panel 后刷新展示。

### 2.3 落地缺口:回执写路由**无现成端点**(核验新发现,文档 §3.4④ 未定)

标记 pending "已消化"(consumed)是一次"远端写",但核验确认 **ingest 侧除 `/ingest`(POST 落库)外无任何写路由**(`/pull`、`/audit`、`/audit/view` 全是 GET 只读)。方案2 文档 §3.4④ 只"或"提了两种可能而未拍板。推荐:

- **推荐(ii)搭 upload 顺带回执**:本地消化后,下一次 `tools.sync.upload` 的信封里附带"已消化 pending 清单",由 ingest 在现有 `/ingest` 落库时顺带把对应行标 consumed。**不新开写口 = 不扩攻击面**,复用现成签名鉴权。
- 备选(i)ingest 加一个签名写路由 `/pool_pending/ack`:改动稍大、多一个对外写口,但语义更干净。**风险取舍需用户拍板**(见待拍板清单)。

### 2.4 web 分流改造(核验修正:add + delete 两个路由都要改)

- 核验确认 web 层**已有两个写路由**:`POST /api/pool`(`app.py:208`,委托 `add_and_collect`)与 **`POST /api/pool/{code}/delete`**(`:220-228`,委托 `remove_and_cleanup`)。方案2 文档只强调了 add,**delete 路由同样要纳入分流改造**。
- 用 env 标志 `POOL_WRITE_MODE=enqueue|direct` 区分:本地默认 `direct`(直采,现状),远端 unit 设 `enqueue`(改为写 pending 表)。核验确认现状**无任何区分本地/远端的环境标志**(`app.py`/`settings.py` 均无 POOL_WRITE/read_only),两端代码路径一致——所以这个标志是新增的、干净的分流点,不动读接口。

---

## 三、岔口③:方案3「已就绪只差真机部署验证」vs「真·新代码」的切分 + 本机做不了的边界

### 3.1 已就绪(代码在库,只差真机部署+验证)—— 核验确认

| 组件 | 代码位置 | 核验结论 |
|---|---|---|
| 远端常驻采集入口 | `ops/remote_fetch.py` | ✅ 完整:oneshot(`:28-51`)+ 交易日守卫(`:34-36`)+ `master_sync.sync_master`(`:47-51`)+ `--backfill`(`:40-45`) |
| 采集 systemd 模板 | `ops/systemd/stock-fetch.{service,timer}` | ✅ 在库 |
| 远端拉取端点 | `ingest.py` `GET /pull kind=kline` | ✅ 已实现(`:302`,鉴权链 `:314-318`,`_pull_kline` 读主档文件 `:224-261`) |
| 本地增量拉客户端 | `tools/sync/pull.py` | ✅ 已实现(签名 GET + 水位 `data/sync_receipts/pull_kline.json` + 落主档幂等) |
| ingest 服务 / SEPA timer | `stock-ingest.service` / `stock-sepa*.timer` | ✅ 模板在库 |

**含义**:方案3 的"远端自采 + 本地拉"这一半环,主要工作是**部署+验证,不是写代码**。这大幅降低 Phase A 成本。

### 3.2 真·新代码(零基础或不存在)—— 核验确认

| 组件 | 核验结论 |
|---|---|
| `ops/remote_spot.py` 实时报价采集器 | ❌ 文件不存在,**新建** |
| `spot_quote` 表 + web 实时页/接口 | ❌ 无,**新建** |
| `stock-spot.{service,timer}` unit | ❌ 不存在,**新建** |
| 盘中监控评估器 + `monitor_rule`/`monitor_state` 表 | ❌ 零基础。`run.py:393` 的 `monitor` CLI 是**盘后 SEPA+VCP 扫描**(screener),非盘中实时;`spot` 相关命中均为 `master_sync` 的**当日 bar 增量采集**,非实时报价监控 |
| `stock-monitor` unit | ❌ 不存在,**新建** |
| 通知渠道 webhook 封装 | ❌ 无后端封装(仅 autoupdate 文档示例用 `curl -X POST <webhook>`),**新建** |
| 形态实时化适配层 | 复用 screener/SEPA 判定逻辑,但"输入换成实时快照拼当日 bar"是新适配层 |

### 3.3 需在远端 39.105.83.174 上做、我在本机做不了的边界(明确划出,需用户协作)

以下**全部依赖真机**,scoping/本机开发阶段无法完成,须在远端服务器上执行,且涉及风险操作(装 systemd、跑回填、动安全组)——**一律先报统筹、由统筹对齐用户后再由用户/授权渠道执行**:

1. **上机核实远端到底跑着哪些 unit**(A期/B期矛盾,见 §四.1)——**第 0 步阻塞点**。
2. 首次全A baostock 全量回填(`remote_fetch --backfill`,~40–60min)。
3. 网络可达性真机验证:baostock 回填、腾讯/新浪/东财**实时快照**接口、东财 push2(服务端非浏览器)、akshare 交易日历、高频轮询限频/被封风险。
4. 远端实例规格查询(`free -m`/`df -h`/带宽)→ 定采集+实时+监控+web 叠加的并发。
5. 阿里云安全组放行评估(建议实时页仍走展示 web 内部读 DB,**不新开对外端口**)。
6. systemd unit 安装 + timer 触发 + 健康检查 + 告警 webhook 联通。

---

## 四、岔口④ + 共同前置:通知渠道选型、远端 data-only vs 等本地回推、以及 ingest 部署状态

### 4.1 【共同前置阻塞点】远端 ingest 部署状态自相矛盾,必须先核实

这是**方案2 和方案3 的共同地基**,也是本报告最重要的待核实项:

- **A 期部署记录**(`远端自动更新与自愈.md:102-186`)明确:远端**只跑 stock-web(8801)、未部署 ingest**,数据走 **git 携带 → `REMOTE_POST_UPDATE=tools.sync.import_to_db` 灌库**,autoupdate 每 **5 分钟**一轮。
- **B 期部署记录**(同文件 `:190-303`)又给出了 ingest(8802)签名直入 DB 的完整部署步骤。
- **方案2 §1.1** 却直接假设"数据经 `tools.sync.ingest`(8802)签名直入 DB"——**这与 A 期记录的"git 携带 + import_to_db"矛盾**。

**影响**:
- 若远端**实际仍是 A 期状态(无 ingest)**:方案2 的 `/pull pool_pending` 回传通道、方案3 的 `/pull kline` 拉取通道**都无法工作**,得先按 B 期步骤部署 ingest(+ nginx 自签 TLS + 密钥)。
- 若远端**已升到 B 期(ingest 在跑)**:两方案的通道地基就绪,可直接扩 kind。

→ **待办**:上机 `systemctl list-units | grep stock` + `curl 127.0.0.1:8802/health`,一次性确认远端实际运行哪些 unit、数据走 git 还是 ingest。**这是编码前的第 0 步。**

### 4.2 通知渠道选型(方案3 Phase C,属"代本人发消息"→ 需用户授权)

| 渠道 | 优点 | 缺点 | 备注 |
|---|---|---|---|
| **飞书/企业微信 机器人 webhook** | 与现有告警惯例一致(autoupdate 文档已用 `curl -X POST <webhook>`);群消息;支持 markdown;免维护服务端 | 需用户拿到群机器人 webhook | **推荐**,取决于用户日常用哪个 IM |
| Server 酱 / PushDeer | 极简、推手机 | 第三方中转、有频率限制 | 轻量备选 |
| Telegram Bot | 跨平台 | 境内网络不稳、远端可达性存疑 | 不推荐(与"境内远端"定位冲突) |
| 邮件 SMTP | 无需第三方群 | 实时性差、易进垃圾箱 | 兜底 |

- **推荐**:飞书或企业微信机器人 webhook(具体哪个由用户定,看其常用 IM)。webhook 地址**走 env、不入库**;去抖(同票同规则一交易日一次)必须内建,防刷屏。
- **红线**:发送告警 = 代本人发消息,**须用户显式授权渠道 + 提供 webhook**,不得由 agent 自行接入。

### 4.3 远端算 data-only 还是等本地回推(方案3 产物层)

| 选项 | 做法 | 改动 | 取舍 |
|---|---|---|---|
| **A · 只搬数据,算全回本地** | 远端只存 raw,本地 pull → 本地算(data+LLM 不拆)→ upload 回推展示(= Phase A / 双向同步 Phase 1) | 小、风险小 | 本地不开机时,远端展示**看不到当日分析**(只有原始数据) |
| **B · 远端 data-only 两段式** | 远端跑 data-only 分析(技术/形态/多因子),web 直接显示 data-only,本地补 LLM 回推覆盖(= Phase 2) | 大(pipeline 拆 data 段/LLM 段/merge) | 远端自给自足;用 `generated_at` 新旧裁决(ingest 已有 409 旧盖新拒绝机制)支持"先 data 落地、后补 LLM 覆盖" |

- **推荐:先 A 后 B**。Phase A 先只搬数据、本地算不拆,风险最小、收益立现(省 ~73min 自采)。B 是可选"锦上添花",等 Phase A 稳了 + 确有"本地长期不开还要看当日分析"的强需求再上,不必现在决。

---

## 五、待用户拍板清单(收敛,发回统筹汇总)

> 以下每条都是"需用户拍板"或"需用户上机核实"的岔口。scoping 窗不自行拍板风险决策。

**P0 · 共同前置(编码前必须先做,阻塞两个方案)**
1. **上机核实远端 39.105.83.174 的实际部署状态**:跑着哪些 unit?数据走 git 携带(A 期)还是 ingest 签名直入 DB(B 期)?ingest(8802)在不在?→ 决定两方案的通道地基是否就绪。**属上机操作,需用户/授权渠道执行。**

**P1 · 路线选择(决定后续全部工作量)**
2. **本地 Mac 盘中经常开着吗?** → 常开:走"方案1+方案2 即止"(推荐路线甲);经常不开:走"方案3 分阶段"(推荐路线乙,先 Phase A)。
3. 若走方案2:确认采用**选项 A(DB pending 表)**、回执用**方式(ii)搭 upload 顺带**(不新开写口)——还是要更干净的独立 ack 写路由(多一个对外写口)?
4. 若走方案3:确认**严格分阶段、先只做 Phase A**,Phase B/C/D 待 Phase A 稳了再逐个拍板?

**P2 · 方案3 专有(走乙路线才需要)**
5. **通知渠道**:飞书 / 企业微信 / 其他?请提供对应群机器人 webhook(不入库,走 env)。—— 属"代本人发消息",需显式授权。
6. **远端产物层**:先 A(只搬数据、算全回本地)、B(远端 data-only 两段式)暂不做——确认?
7. 远端实例规格(CPU/内存/带宽/磁盘余量)是多少?→ 定实时+监控并发前需用户提供或授权上机查。

**风险操作边界(scoping 窗不执行,需统筹对齐用户)**:所有远端 systemd 安装、baostock 回填、安全组改动、push、动配置,一律先报统筹。

---

## 六、代码证据附录(子 agent 核验,`文件:行号`)

- `remote_update.py:161-164` ff-only 失败→无条件 `reset --hard`;无单文件保护特例(核验)。默认重启 stock-web:8801 + stock-ingest:8802(`:238`/`:37-43`)。
- `ingest.py:194` `PULL_KINDS=("kline",)`;`/pull` 鉴权链 `:314-318`;`_pull_kline` 读主档文件 `:224-261`(`:247 repo.get_master_kline`);**除 `/ingest` 外无写路由**(`/pull`/`/audit`/`/audit/view` 全 GET)。
- `pull.py:174-175` `kind!="kline"` 返回不支持;水位落 `data/sync_receipts/pull_<kind>.json`(`:43-44`)。
- `pool_service.py:83-95` `add_and_collect`=add→collect_one→rebuild;`:27-52` `rebuild_artifacts` 全池 serialize/chart/panel + `put_view("screen",...)` 整表替换;`:98-106` `remove_and_cleanup` 存在且完整。
- `web/app.py:208-217` `POST /api/pool`→`add_and_collect`;`:220-228` **`POST /api/pool/{code}/delete` 已存在**→`remove_and_cleanup`;无 POOL_WRITE/read_only 标志(核验)。
- `stock_pool.py:22` 真源 `config/stock_pool.json`;`:99-108` 缺文件用 `_SEED`(34 只,`:40-84`)重建;`add_stock:203-204` 重复码抛 ValueError。
- `ops/remote_fetch.py:28-51` 完整(oneshot+守卫+master_sync+backfill);**`ops/remote_spot.py` 不存在**;无 `monitor_rule`/`spot_quote`/webhook 告警模块;`ops/systemd/` 有 stock-fetch/ingest/sepa/autoupdate/web/nonce-cleanup,**无 stock-spot / stock-monitor**。

---

## 附:本报告相对任务书/方案稿的修正与新发现

1. **方案2 回执写路由无现成端点**(§2.3):文档 §3.4④ 未定,核验确认 ingest 唯一写口是 `/ingest`。→ 推荐搭 upload 顺带回执。
2. **web 已有删除路由**(§2.4):方案2 文档只提 add,实际 `POST /api/pool/{code}/delete` 已存在,分流改造须一并覆盖。
3. **ingest 部署状态 A期/B期矛盾 + 方案2 §1.1 与 A 期记录冲突**(§4.1):升级为 **P0 共同前置阻塞点**,编码前必须上机核实。
4. 其余方案稿论断(reset 抹改动、rebuild 打瘪 panel、Phase 1 代码已就绪、实时监控零基础)均核验属实。
