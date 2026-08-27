# state_pool sidecar + tail-read 深度重构 · 设计方案（待审）

> 承接 `docs/计划/state_pool增量重建.md` §4「关键发现」第 4 点。上一轮做了"冻结复用 + 末端回填 + 逐行值校验"，
> 实测：**稀疏重跑大赢（3.3x），但全A每票各 append 1 根（生产日常态）持平甚至略慢**。根因是 `_pool_labels`
> 的每次调用**固定开销**（多 Series 构造 + ewm/rolling 初始化）主导——每票都有新 bar 时仍要每票各跑一次尾窗
> `_pool_labels`，省不下来。本方案设计 sidecar（侧存 MACD/RSI EWM 末态，新 bar 标签走 O(1) 递推、彻底不调
> `_pool_labels`）+ tail-read，把日常态压到个位数秒的可行性、正确性、失效兜底与测试全部论证清楚。
>
> **本文只出设计，不改生产代码。** 附带的所有数字来自 `/tmp/bench_sidecar.py` / `/tmp/bench_exact.py` /
> `/tmp/bench_valcheck.py` 在**真实数据**（`data/master/kline/` 5212 票、`state_pool.parquet` 249MB / 8.79M 行）
> 上的实测，非拍脑袋。

---

## 0. 实测事实（决定路线的硬数字）

在真实数据上按 500 票测、线性折合 5212 票（`/tmp/bench_sidecar.py`，纯本机、含磁盘）：

| 环节 | 500 票 | 折合 5212 票 | 说明 |
|---|---|---|---|
| 全量 kline 读盘 I/O | 0.91s | **~9.4s** | `pd.read_parquet` 全历史，avg 1882 根/票 |
| `_pool_labels` 全历史 CPU | 3.54s | **~37s** | 现全算的**大头** |
| `_pool_labels` 768 尾窗 CPU（现增量新 bar 路径） | 2.19s | **~23s** | 每票各跑一次，固定开销主导 → 省不动 |
| **sidecar O(1) 递推 append 1 bar（全票）** | 0.008s | **~0.08s** | 见下，实测**几乎免费** |
| 前瞻收益全行重算（`_forward_returns`，值校验核心） | 0.51s | **~5.3s** | 纯 numpy 切片，比 shift 版 (~14s) 快 |
| 旧池读盘（一次） | 0.51s | ~0.5s | 249MB / 8.79M 行 |
| 全量写盘（一次） | 1.82s | ~1.8s | 249MB |

两条**颠覆性**发现（都被实测钉死，改变了任务书原本的假设）：

1. **瓶颈是标签 CPU（~37s），不是 I/O（~9.4s）。** 单靠 sidecar 把标签 CPU 打到 ~0，就吃掉了大头。
   任务书原设想"tail-read 才是关键"——实测**站不住**：I/O 只占 ~9.4s。

2. **tail-read 在当前存储下基本省不到 I/O。** `pyarrow.ParquetFile` 显示每票主档 = **单 row-group**
   （`/tmp/bench_exact.py`：`row groups: 1, rows: 2099`）。pandas/pyarrow 读单 row-group 必须整文件解压，
   `.tail(800)` 只在解压后切片，省不到解压成本。实测"只读 date+close 两列 + tail(800)" = 0.69s（vs 全读 0.91s），
   省的是**列裁剪**不是**尾部**。**真要省 kline I/O，必须动存储层**（row-group 分块写 / 侧存尾档），非 append-only 改 `conditional_predict.py` 能达成。

3. **O(1) 递推是 bit-exact，不是"收敛到 ULP 内"。** `/tmp/bench_exact.py`：从 `close[:n-1]` 建末态、递推 1 根，
   MACD 柱 / RSI12 与 `ta.macd`/`ta.rsi` 全量重算 **diff = 0.000e+00**。这比现增量的 768 尾窗（只保证 (1-α)^768 < ULP
   的近似收敛）**更强**——只要末态用 float64 精确落盘，递推与全算逐位相等。

---

## 1. 核心岔口：tail-read 打破"全史逐行值校验"这个除权兜底

现状（已合入 main）的除权兜底：读**全史 close**，对旧池**每一行**按新 kline position 重算全部前瞻收益，
凡旧已兑现的 `r_N`/`od_N` 必须逐值一致；任一不符 → 该 code 全量重算。这能捕获**任意位置**的非等比改写。
只读尾部就做不到这件事。下面把除权改写的机理讲清，再比三条路。

### 1.1 A股 qfq 改写机理（决定漏检窗口）

- 主档 `KLINE_ADJUST = "qfq"`（前复权），`data/master/kline/<code>.parquet` 存**前复权全历史**、date 升序。
- 日常 append（`append_master_kline`）：只并入当日新 bar，**历史行不动** → 历史 close 不变。
- **除权发生**（新分红/送转）→ 采集层重取 qfq 全史并 `put_master_kline` **整表覆盖**：在**新除权日**插入一个
  复权因子断点，**该日之前**所有 qfq close 被同一因子 f 再乘一遍、**该日之后**不变。
- 对比率型 `r_N = close[t+N]/close[t] - 1`：
  - t、t+N **同在断点前**：(f·a)/(f·b)=a/b，**不变**（整段等比 → 复用本就正确，无需检测）。
  - t、t+N **同在断点后**：不变。
  - **跨断点**（t 在前、t+N 在后）：比率被 1/f 改变 → **这才是要检测的**。
- 关键推论：**一次"新鲜"除权只改动跨新除权日的 r_N**。新除权日必在近端（正因刚除权才重取）→ **所有被改的 r_N
  都落在尾部窗口** `[新除权日位置 − N, 末尾]` 内。深史行（两端都在断点前）r_N **不变**。
- **漏检风险的真正来源**：厂商**修正深史**的老除权/错误（在历史深处插/改断点）——此时被改 r_N 在深史，
  **尾读窗口看不到**。此类事件罕见但非零。

### 1.2 三条路线对比

| 维度 | 甲：全读 close 值校验 + sidecar O(1) 标签 | 乙：sidecar 侧存历史校验值，纯 tail-read | 丙（混合）：tail-read 值校验 + 存储层完整性戳 |
|---|---|---|---|
| 省什么 | **标签 CPU ~37s→~0**（大头） | 标签 CPU + kline I/O | 标签 CPU + kline I/O |
| 不省什么 | kline I/O ~9.4s、全量写 ~1.8s | — | 全量写 ~1.8s |
| 除权检测正确性 | **全位置精确、零漏检**（与现状同） | **有漏检窗口**：只覆盖尾读窗口，深史修正漏 | 尾窗值校验 + 存储层 history-hash 戳兜底深史 |
| sidecar 需存 | EWM/SMA 末态 + MA/BOLL 尾巴（见 §2） | 甲的全部 **＋** 历史 close 的滚动校验签名 | 甲的全部 **＋** 依赖存储层 meta 增 `history_hash` |
| 失效/回退 | 值校验不过 → 该 code 全算 + 重建 sidecar | 签名不过 → 全算；深史漏检**无法回退**（危险） | 戳变 → 全算；戳需存储层配合 |
| 实现复杂度 | **中**（只加 sidecar + 抽离散化） | 高（自造校验签名 + 论证漏检边界） | **高**（要改 `tools/store/repo.py`，跨模块） |
| 漂移风险 | **单一**：只在指标递推，离散化复用同段代码 | 同甲 + 签名口径漂移 | 同甲 |
| 预估 wall-clock（5212 票） | **~17s**（9.4 读 + 5.3 校验 + 0.1 递推 + 0.5 旧池 + 1.8 写 + concat） | **~5–7s**（需存储层分块读才成立） | ~5–7s（同乙前提） |
| 能否个位数秒 | **否**（I/O+写地板 ~11s） | 是（但需存储层改 + 担漏检） | 是（需存储层改） |

### 1.3 推荐：**先落路线甲，个位数秒作为需存储层配合的第二阶段（乙/丙）**

理由：
1. **甲吃掉真瓶颈**（标签 37s→0），实测把日常态从 ~68s 压到 **~17s（~4x）**，**且零除权漏检、逐位一致、漂移风险单点**。
   这是"高价值、低风险、纯 `conditional_predict.py` + sidecar 文件"能拿到的最大收益。
2. **个位数秒的最后一公里卡在 kline I/O（9.4s）+ 全量写（1.8s），两者都要动存储层**（row-group 分块 / 尾档缓存 /
   分片池文件），**不是 append-only 改分析层能达成的**——这点任务书原假设（"tail-read → 个位数秒"）被实测推翻。
3. 路线乙纯 tail-read 会引入**深史修正漏检且无法回退**的正确性风险，违背"逐值一致"硬约束的精神；路线丙用存储层
   `history_hash` 戳兜底可保正确，但**跨模块、需存储层拍板**。因此把 I/O 优化**降级为第二阶段**、单独立项，
   不与 sidecar 绑死。

> **需拍板 A**：是否接受第一阶段止步 **~17s（4x）**、把"个位数秒"留给需改存储层的第二阶段？还是坚持一步到位、
> 现在就动 `tools/store/repo.py`（分块 row-group + `history_hash` meta），承担跨模块改动与回归面？
> 我推荐**先甲**——用低风险 4x 兑现日常态提速，第二阶段（乙/丙）拿实测的 I/O 地板数字再评估投入产出。

---

## 2. sidecar 设计（路线甲）

### 2.1 存储格式与落点

- **单张 parquet 表** `data/backtest_local/state_pool_sidecar.parquet`，与 `state_pool.parquet` **并排**，
  gitignore、只写 worktree（同池产物纪律）。
- **每票一行**（~5212 行，体积 ~数 MB，读写各 <0.1s）。**不用**每票一文件（5212 个碎 json 会重演存储层刻意规避的
  "按日期/按票分区的跨日返工"，且 5212 次 open 的 fs 开销远超单表）。
- 表 schema（一行一票）：

| 列 | 类型 | 含义 |
|---|---|---|
| `code` | str | 主键 |
| `last_date` | datetime64[ns] | 该票 sidecar 对应的**末根 bar 日期**（= 建 sidecar 时 kline 末行；位置锚点） |
| `ema_fast` | float64 | MACD EMA(span=12, adjust=False) 末值 |
| `ema_slow` | float64 | MACD EMA(span=26, adjust=False) 末值 |
| `dea` | float64 | DEA = EWM(dif, span=9, adjust=False) 末值 |
| `macd_bar_last` | float64 | 末根 MACD 柱 `(dif-dea)*2`（新 bar 判金叉/死叉的"前一根"） |
| `rsi_up` | float64 | `_sma_cn(涨幅, 12, 1)` 末值（avg gain） |
| `rsi_down` | float64 | `_sma_cn(跌幅, 12, 1)` 末值（avg loss） |
| `prev_close` | float64 | 末根 close（算下一根 diff 用；也是递推 EMA 的锚） |
| `tail_close` | list<float64> | 末尾 **≥60 根** close（供 MA5/10/20/60 + BOLL20 有限窗**精确**重算；不涉递推、不漂移） |
| `schema_version` | int | sidecar 结构版本 |
| `param_hash` | str | 见 §2.4，指标/阈值/horizons/warmup 的口径指纹 |

> **注**：MA/BOLL 是**有限窗**（rolling），本身无限记忆问题——sidecar 存尾巴 close、新 bar 到来时用
> `close.rolling(w)` 精确重算即可，**不需要**存 MA/BOLL 的"递推末态"，也就没有它们的漂移风险。
> 需要存末态的只有 **MACD 的 3 个 EWM 项 + RSI 的 2 个递归 SMA 项**（无限记忆）。

### 2.2 各末态的播种口径（必须与 `technical.py` 逐字一致）

- **MACD**（`ta.macd`，`ewm(span, adjust=False)`）：α_fast=2/13、α_slow=2/27、α_signal=2/10。
  `adjust=False` 首值 = 首根 close（fast、slow 同 → dif[0]=0 → dea[0]=0）。递推 `e_t = α·x_t + (1-α)·e_{t-1}`。
- **RSI**（`ta.rsi(close,12)` → `_sma_cn(x,12,1)`）：`y_t=(1·x_t+11·y_{t-1})/12`；**首个非 NaN 值自身作种子**；
  `diff[0]=NaN`（`close.diff()`）→ `up[0]=down[0]=NaN`，首个有效在 i=1。`denom==0` → rsi=50。
  ⚠ `_sma_cn` 对 NaN 输入是"沿用前值 prev"——递推时须复刻这条（正常无 NaN close 不触发，但停牌/缺口要照顾）。
- 建 sidecar 时（全算或 fallback 后）**直接从 `ta.macd`/`ta.rsi` 的完整输出取末值**，而非另写一套累加——
  **保证末态与全算同源**（见 §3 防漂移）。

### 2.3 O(1) 递推：新 bar 标签怎么产出

对某票有 `k` 根新 bar（`c_1..c_k`，`c_0=prev_close`）：
1. 逐根递推 MACD 5 项、RSI 2 项（bit-exact，实测 diff=0）；每根得 `macd_bar_j`、`rsi12_j`，`prev` 用上一根。
2. MA/BOLL：`np.append(tail_close, 新close序列)` 后对每个新 bar 取窗口末值算 `ma5/10/20/60`、`percent_b`
   （`boll` 的 `std(ddof=0)`、`percent_b` 的 width==0→NaN 语义照 `ta.boll`）。
3. 把 `(ma5,ma10,ma20,ma60, macd_bar_j, macd_bar_{j-1}, rsi12_j, percent_b_j)` 喂给**共享离散化函数**
   `_labels_from_indicators(...)`（见 §3）→ `(trend, mom, boll)`。
4. 前瞻收益 `r_N/od_N`：仍按 §1 的 `_forward_returns` 全序 position 算（新 bar 多为 pending/NaN）。
5. 递推完，用**末根**的 5+2 项 + `macd_bar_last` + `prev_close` + 新尾巴 close 覆盖写回该票 sidecar 行。

**实测**：整套递推（全票各 append 1 根）~0.08s（5212 票），相比 768 尾窗 `_pool_labels` ~23s，**~290x**。

---

## 3. 防漂移核心机制（硬约束：sidecar 递推标签 ⟷ 全算 `_pool_labels` 逐值一致）

**唯一可持续的做法：抽出"离散化"为单一函数，两条路径都调它——而非复制粘贴 `np.select` 阶梯。**

现 `_pool_labels` 里"连续指标 → 离散标签"这段（`valid_ma`/多头空头纠缠、金叉死叉需前一根、RSI 强弱中、
BOLL 破/触/中性 + `percent_b` 的 NaN 语义）是纯函数，无隐藏依赖，可整段抽离：

```
# 设计意图（伪代码，落地时放 conditional_predict.py，_pool_labels 与递推路径共用）
def _labels_from_indicators(ma5, ma10, ma20, ma60, macd_bar, macd_bar_prev, rsi12, percent_b):
    # ← 现 _pool_labels 内那段 np.select 原样搬进来（既能吃 Series 也能吃标量/小数组）
    ...
    return trend, mom, boll

def _pool_labels(df):                 # 全算：ta.* 算连续指标 → 调 _labels_from_indicators
def _labels_recur(sidecar, new_close):# 递推：O(1) 出连续指标 → 调 _labels_from_indicators（同一段）
```

- **离散化只有一份实现** → 阈值语义（`动量RSI强=55/弱=45`、`触轨上=0.8/下=0.2`、破轨 >1/<0、金叉需 prev≤0&bar>0…）
  **不可能双份漂移**。改阈值只改一处，两路自动同步。
- 唯一可能漂的是**连续指标本身**（递推 vs 全算）——已被 `/tmp/bench_exact.py` 证明 **bit-exact（diff=0）**，
  且由 §5 的"逐值锁测"长期把守。
- **落地要求（写给实现窗）**：抽 `_labels_from_indicators` 时**只做移动、不改逻辑**，用现有
  `tests/test_state_pool_incremental.py` 的全量一致锁测先证明"抽离散化后全算输出 0 变化"，再接递推路径。

---

## 4. 失效条件与回退（版本戳 + 值校验双层）

| 失效场景 | 检测手段 | 回退动作 |
|---|---|---|
| 除权 qfq 改写（跨断点非等比） | **全史 `_forward_returns` 值校验**（路线甲，同现状） | 该 code 全算 `_pool_cols` + 重建该行 sidecar |
| 历史被删/插入/重排 | `searchsorted` 结构校验（旧 date 须仍在新 kline 且位置严格递增，同现状） | 同上 |
| `last_date` 不在新 kline / kline 变短 | 位置锚点校验 | 该 code 全算 + 重建 sidecar |
| warmup / horizons / 阈值 / 指标 span/window 变更 | `param_hash` 不匹配 | **全体** rebuild（sidecar 整表作废重建） |
| `schema_version` 升级 | 版本号不匹配 | 全体 rebuild |
| sidecar 文件缺失 / 某票无行 | 读不到该票末态 | 该 code 全算 + 建 sidecar 行（首建自然路径） |

- `param_hash = hash( (5,10,20,60), macd(12,26,9), rsi=12, boll(周期20,倍数2.0,ddof=0),`
  `触轨上0.8/下0.2, 动量RSI强55/弱45, warmup, horizons ) `。任一口径变 → 指纹变 → 整表作废。**防"改了阈值忘了作废 sidecar"**。
- **回退是安全默认**：任何校验不过一律退全算，sidecar 只是加速旁路，**永不作为唯一真源**。产物 parquet 列/格式/
  顺序与全量逐值一致（下游 screener 只读索引，不受影响）。
- **深史除权漏检（仅当未来走路线乙/丙）**：路线甲无此问题（全读全校验）。若第二阶段切 tail-read，须**同时**上
  存储层 `history_hash` 戳（丙）兜底，否则违反"逐值一致"精神——这是路线乙被否的关键。

---

## 5. 测试设计（只设计，写进 `tests/test_state_pool_sidecar.py`，本轮不落地）

| 测试 | 断言（锁住"为什么改"的语义） |
|---|---|
| `test_labels_from_indicators_extract_noop` | 抽 `_labels_from_indicators` 后，`_pool_labels` 全量输出与抽取前**逐值+NaN 相同**（防抽离散化时手滑改逻辑） |
| **`test_recur_labels_equals_full`（核心逐值锁）** | 随机若干票，sidecar 递推标签 vs 全算 `_pool_labels` 对**每个新 bar** 的 trend/mom/boll **逐值相同**；连续指标 diff==0 |
| `test_recur_bitexact_indicators` | 递推的 MACD 柱 / RSI12 与 `ta.macd`/`ta.rsi` 全算末值 **diff==0.0**（不是 allclose，是精确相等） |
| `test_new_bar_O1_no_pool_labels` | append 新 bar 时 monkeypatch `_pool_labels` 抛异常，证明新 bar 路径**零调用** `_pool_labels`（真 O(1)） |
| `test_qfq_rewrite_detected_fallback` | 改写历史前复权价（跨除权日非等比）→ 值校验命中 → 该 code 全算 + 重建 sidecar，结果与纯全量一致 |
| `test_param_hash_invalidation` | 改 warmup / 阈值 / horizons → `param_hash` 变 → 整表 rebuild，不误用旧 sidecar |
| `test_schema_version_bump` | sidecar 结构版本升级 → 旧 sidecar 作废重建 |
| `test_sidecar_missing_first_build` | 无 sidecar 首建：全算 + 落 sidecar，产物与纯全量一致 |
| `test_tail_close_finite_window_exact` | MA/BOLL 用 sidecar 尾巴 close 重算 = 全史 rolling 末值（有限窗必等，防尾巴存少了） |
| `test_pending_matures_with_sidecar` | 旧 pending 行经新 bar 到期兑现，值与全量一致（沿用现增量语义） |
| `test_stale_close_nan_carry` | 停牌/NaN close 下 `_sma_cn` 的 prev-carry 递推与 `ta.rsi` 全算一致（照顾 §2.2 的 NaN 语义） |

> 若第二阶段上 tail-read（乙/丙），追加：`test_tail_read_equals_full`（尾读产物 = 全读产物）、
> `test_deep_qfq_rewrite_caught_by_hash`（深史改写被 `history_hash` 戳命中并回退）。

---

## 6. 预估提速（带实测支撑）

| 场景 | 现状 | 本方案（路线甲） | 依据 |
|---|---|---|---|
| 全A 每票 append 1 根（生产日常态） | ~68s | **~17s（~4x）** | 9.4 读 + 5.3 值校验 + 0.1 递推 + 0.5 旧池 + 1.8 写 + ~concat |
| 标签 CPU 分项 | ~37s | **~0.08s（~290x）** | `/tmp/bench_sidecar.py` [3] vs [6] |
| 稀疏/无新 bar 重跑 | 已 3.3x | 更快（递推近 0） | 递推 + 少量值校验 |

**能否到个位数秒？路线甲：否，地板 ~17s（I/O 9.4 + 值校验 5.3 + 写 1.8 不可压）。** 要个位数秒须第二阶段动存储层：
- kline I/O 9.4s → 需 row-group 分块写 + 尾读，或侧存"尾档"缓存（乙/丙，跨模块）；
- 值校验 5.3s → tail-read 后只校验尾窗（~0.5s），但引入深史漏检、须配 `history_hash` 戳；
- 全量写 1.8s → 需池文件分片（按 code 前缀）只重写变化分片，否则 249MB 整写是地板。
三者齐活理论 ~5–7s。**投入产出与正确性风险见 §1.3 需拍板 A。**

---

## 7. 仍不确定 / 需统筹或用户拍板

- **需拍板 A（路线选择）**：第一阶段止步 ~17s（4x，低风险、纯分析层 + sidecar），把个位数秒留给需改
  `tools/store/repo.py` 的第二阶段？还是现在就一步到位动存储层？**我推荐先甲。**
- **需拍板 B（写盘地板）**：249MB 全量写 ~1.8s 是否可接受为地板？若要进一步压，须把 `state_pool.parquet`
  **分片**（按 code 前缀）——但下游 screener 现在按单文件读索引，分片要同步改下游读法（跨模块）。
- **需拍板 C（RSI NaN 语义）**：`_sma_cn` 对 NaN 输入"沿用前值"。停牌/缺口 close 是否可能出现在主档 qfq 序列里？
  若主档已剔停牌则此路径不触发，递推可简化。**假设**：主档为连续交易日 qfq、无内嵌 NaN close；若不成立，
  递推须显式复刻 prev-carry（已在 §2.2 标注，测试 `test_stale_close_nan_carry` 兜底）。
- **假设 D**：master 主档 = 单 row-group（已实测 000001 为 1 个 row-group）。若少数大票被分成多 row-group，
  tail-read 的存储层方案才有增量价值——第二阶段需先普查 row-group 分布。
- **假设 E**：`_labels_from_indicators` 可无损从 `_pool_labels` 抽出（已读代码确认是纯 np.select、无隐藏依赖）。
  落地时以 `test_labels_from_indicators_extract_noop` 先证明零行为变化再接递推。
