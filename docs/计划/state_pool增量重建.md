# state_pool 增量重建（策略11 建池提速）

分支 `feat/state-pool-incremental`。只动 `tools/analysis/conditional_predict.py`，下游 screener 只读 `state_pool.parquet` 索引，列/格式一列不变。

## 现状与瓶颈

`build_state_pool(codes, save=True)` 每天全A（~5200 只）逐票 `load_kline` 全量历史 → 向量化算 3 主维度标签（trend/mom/boll）+ 3 个前瞻收益 r_N/结局日 od_N → 全量 `to_parquet` 覆盖。产物 ~249MB / 8.79M 行 / ~68s。

合成基准（500 票 × 2000 根，`/tmp/bench_pool.py`）拆解成本：

| 步骤 | 500票 | 折合5200票 |
|---|---|---|
| 标签 `_pool_labels`（MA/MACD/RSI/BOLL）| 3.39s | ~35s |
| 前瞻收益 r_N/od_N（shift）| 1.35s | ~14s |
| concat | 0.05s | — |
| 写 parquet | 0.20s(29MB) | ~1.7s(249MB) |
| 读 parquet | 0.11s | ~1s |

标签是大头，且 MACD/RSI 用 **EWM/递归 SMA（无限记忆）**，历史行的标签**不随新 bar 变**（只依赖 t 及以前收盘），只有末端 N 行的前瞻收益会 pending→realized。

## 每行三类列

- **静态标签** `trend/mom/boll`：只依赖 close[0..t]。纯 append 时历史行不变 → 可原样复用。
- **前瞻收益** `r_N`/结局日 `od_N`：`r_N=(close[t+N]/close[t]-1)*100`、`od_N=date[t+N]`。t+N 不存在时 NaN/NaT（末端 pending），新 bar 到来才兑现。
- N（前瞻窗口）= `horizons=(1,5,10)`，池预热 `warmup=THRESHOLDS['指标条件化']['池预热根数']=60`。

## 增量方案

`build_state_pool(codes, ..., save=True, rebuild=False, pool_path=POOL_LOCAL)`：`save=True` 且旧 parquet 存在且非 rebuild → 走增量；否则全算。

对每只 code（旧池有该 code 记录时）：

1. **结构校验**：旧池每个 date 必须仍存在于新 kline（按值映射 date→position）；否则该 code 全量重算（历史被删/插入/重排）。
2. **值校验（除权 backfill 失效兜底）**：对旧池**每一行**按新 kline position **重算全部前瞻收益**，凡旧已兑现的 `r_N`/`od_N` 必须逐值一致；任一不符 → 该 code 全量重算。前瞻收益本就便宜（数组切片），逐行重算不影响提速大头（标签复用），且比抽样锚定更强——能捕获**任意位置**的非等比改写（早期实现用稀疏锚定，单测 `test_qfq_rewrite_recompute` 证明会漏检局部改写，遂改为逐行）。除权前复权若为**整段等比缩放**，比率型 r_N 不变、值本就一致（复用正确、无需重算）；**跨除权日的非等比改写**改变比率 → 被逐行校验命中 → 全量重算。**不靠 mtime**（纯 append 也 bump mtime，会误判全失效）。
3. **复用/回填**：
   - **历史行标签冻结**：旧行 `trend/mom/boll` 原样复用，**不碰 `_pool_labels`**（提速大头）。
   - **前瞻收益回填**：对所有旧行按新 kline position 重算 `r_N/od_N`——已兑现行值不变（校验已保证一致），末端 pending 行经新 bar 到期兑现。口径与全算逐字一致、无未来函数。
   - **新增 bar 行**（旧池无、position≥warmup）→ 需算标签：在 `df.iloc[start:]` 尾窗上跑 `_pool_labels`（`start=max(0, 首个新bar位置 - _LABEL_CONVERGE)`，`_LABEL_CONVERGE=768` 保证 EWM 收敛到 float 噪声下、与全史逐值一致），前瞻收益按 position 全序计算（多为 NaN/pending）。过滤"数据不足"。
   - 复用行 + 新增行按 date 排序拼回，与全算逐行同序。
4. 旧池无该 code / rebuild → 全算 `_full_code_frame`。
5. 输出仍全量写同一 parquet（列/格式/顺序不变，幂等）。

## 无未来函数 & 一致性

- 前瞻收益补算只查 close 表、与全算同公式同 position，**无未来函数**。
- 冻结行直接复用旧值；pending/新 bar 逐 position 重算 → NaN 模式与全算一致。
- 新 bar 标签用尾窗 `_pool_labels`：MA/BOLL 有限窗（≥60）本就精确；MACD/RSI 递归项误差随窗长指数衰减，`_LABEL_CONVERGE=768` 时 (1-α)^768 ≪ float ULP → 离散标签逐值一致。

## CLI

`conditional_predict.py` 加 `__main__`：`--rebuild` 强制全算、`--codes`。生产内联调用 `build_state_pool(codes, save=True)` 默认走增量。

## 实测（合成基准，500 票 × 2000 根，`/tmp/bench_incr.py`；纯 CPU，无磁盘 I/O）

| 场景 | 耗时 | 相对全量 | 逐值一致 |
|---|---|---|---|
| 全量重建 baseline | 4.86s | 1.0x | — |
| 增量·**仅 1 票 append**（稀疏） | 1.45s | **3.3x** | ✓ |
| 增量·**全A 每票各 append 1 根**（生产日常态） | 5.05s | ~1.0x（持平） | ✓ |

折合 5200 票：全量 ~51s、增量全A-append ~53s。

## 关键发现 / 权衡（诚实）

1. **稀疏/部分变更/无新 bar 重跑 → 大赢**：未变 code 全复用（不碰 `_pool_labels`），只做向量化值校验 + 末端收益回填。盘中重跑、部分回补、非交易日重跑属此类。
2. **全A 每票各 append 1 根（生产日常态）→ 持平，未见提速**。根因：`_pool_labels` 的**每次调用固定开销**（多个 Series 构造 + ewm/rolling 初始化）主导，与行数关系不大——768 根尾窗与 2000 根全史耗时几乎一样。每票都有新 bar 时仍需每票各跑一次尾窗 `_pool_labels`，省不下来。
3. **若生产 68s 主要是磁盘 I/O**（读 5200 个 kline parquet），则增量同样要读全 kline（值校验贯穿全史 + 末端收益需 close），**wall-clock 也降不下来**。增量还额外读一次旧池 parquet（~249MB），全A-append 场景可能**略慢**。
4. **要把全A-append 真正压到个位数秒**：须 (a) 每票只读 kline 尾部（`load_kline_recent`）而非全史，(b) 侧存 MACD/RSI 的 **EWM/递归SMA 末态 sidecar**，使新 bar 标签走 O(1) 递推、彻底不调 `_pool_labels`。本轮**不做**——单行复刻 `_pool_labels` 的离散标签逻辑（valid_ma / 金叉死叉需前一根 / RSI 播种 / BOLL ddof=0 / percent_b NaN）会与 `_pool_labels` 产生**双份实现、易漂移**，威胁"逐值一致"这条硬约束。记为后续优化（需配 sidecar↔全算 的逐值锁测）。

## 落地建议（待用户拍板）

- 现状：`build_state_pool(save=True)` 在旧池存在且非 rebuild 时**自动走增量**，逐值一致、失效安全。
- 日线 pipeline（全A 每票 +1 根）用增量约等于全量甚至略慢 → 可选：**日线继续用 `rebuild=True`**（干净 baseline），把增量留给盘中重跑 / 部分回补路径；或投入第 4 点的 sidecar 改造换取日线个位数秒。二选一请用户定。

## 测试（tests/test_state_pool_incremental.py）

- `test_state_pool_incremental_equals_full`：全量重建 vs 读旧+增量 → 逐值 + NaN 一致（核心锁）。
- `test_new_bar_appended`：某 code append 新 bar → 增量与全量一致，且 `_pool_labels` 只在尾窗上调用（非全史）。
- `test_pending_matures`：旧池 pending 行经新 bar 到期 → 正确兑现。
- `test_qfq_rewrite_recompute`：改写历史前复权价 → 值校验捕获 → 全量重算，结果与全量一致。
- `test_append_only_reuses`：kline 未变重跑 → `_pool_labels` 零调用（monkeypatch 抛异常证明），输出与旧一致。
- `test_rebuild_flag`：`rebuild=True` 强制全算。
