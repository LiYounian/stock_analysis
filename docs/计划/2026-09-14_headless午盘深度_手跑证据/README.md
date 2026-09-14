# headless 午盘深度选股 · 端到端手跑证据（2026-09-14）

> 线：headless 午盘深度分析·launchd 免漂移（方案 b）。⚠️ 测试环境研究模拟，非投资建议。
> 设计：`docs/计划/2026-09-14_headless午盘深度选股_launchd免漂移_设计.md`

## 跑法（真调 DeepSeek，生产数据只读隔离）

- 真调 LLM 走 `zsh -ic`（带网关 env，非交互 Bash 无网关）；DeepSeek，think 关。
- **读根 = 生产只读**：`--data-root` 指向 scratchpad，其 `data/analysis` 符号链接到主仓
  `data/analysis`（真实 per-stock record + sentiment + market_forecast），`data/intraday` 放
  主仓 `noon_screen_snapshot.json`（11:30 冻结）符号链接 + 合成 stage1 候选（真实 code、综合分为
  合成排序——主仓当日未落 stage1/日内全A_ 台账，故手跑合成候选排序，per-stock 数据全真）。
- **写根 = scratchpad 隔离**：`--write-root` + `--out-dir` 指 scratchpad，`--no-collect`（只读现有
  news_ai/sentiment，绝不写生产）。cutoff（≤11:30）开。
- 5 只候选：002528 / 600125 / 000978 / 000722 / 002909。

## 实测结果（13:00 可行性硬证据）

| 指标 | 实测 | 说明 |
|---|---|---|
| 逐票 DeepSeek 时延 | 7.9 / 8.1 / 7.9 / 8.8 / 10.5 s | think 关、顺序跑 |
| deep 合计（5 只） | **43.2 s** | 单票中位 ~8s |
| 端到端墙钟 | **43.8 s** | 含候选读取 + 装配 + 校验 + 渲染（deep 占绝对大头） |
| 退出码 | 0 | 5 票全 OK（无 LLM 失败） |

**13:00 可行性结论（实测校准）**：DeepSeek 单票 ~8s（远快于设计估的 30-70s）。即便 ~12:00 才开工，
5 只深度 ~44s → ~12:01 出；叠加生产侧自采 Top-N 消息面（message+sentiment+events，~1-4min）总计仍
**<5min，赶 13:00 余量极大**。免漂移（纯 headless、无 Claude 窗口）从根上消除结构性漂移风险。
→ 若换线时 N 增大或网关变慢，仍有 `--top` 降档 / 并发 DeepSeek 的余量。

## 深度质量（对齐盘后规格）

样例见 `日内深度_2026-09-14.sample.md`。逐票走完整深度 SOP：定性分票 + 数据层（bias20/RSI/vol_ratio/
资金流/筹码获利盘/户数变动，引真实 record 值）+ α/β 拆分 + 盯点单问化（含供给面"存续可转债/定增/解禁"
固定一问）+ 退出/止损条件 + 方向前提。**DeepSeek 正确规避 *ST/超买妖股**（002528 *ST 资不抵债、
000978 RSI6=98.4 连板脱离基本面）——保守纪律生效，与盘后母本"妖股不盖棺方向、基本面证伪高分候选"一致。

- **本样例 0 买入**：候选为合成任选（非真实 stage1 数据面高分票），DeepSeek 逐票判为观望/规避属合理
  （2 只 *ST/超买本应规避）。**买入排序表 + D-0 计划 + PICKS 锚点（买入票）的渲染路径由
  `tests/test_intraday_deep.py::test_end_to_end_headless` 确定性覆盖**（桩 client 产买入票，断言
  PICKS=买入票、buy_rank 连续、D-0 表、canonical 校验过）。
- **生产隔离已核**：主仓 `data/analysis/2026-09-14/日内深度选股.json` 与
  `docs/每日分析/选股/日内深度_2026-09-14.md` 均未生成（本手跑零写生产）；写入全落 scratchpad write-root。

## 已知点（follow-up）

- **market_context 已修**：手跑首版键名不匹配显示"未取到定性字段"；已改读 market_forecast/v1 真实键
  （选股用β基准/targets.hs300/breadth_snapshot/分歧标记），实测输出：
  `β基准背景：hs300；广度：涨3126/跌2223、above_MA20=0.2911；分歧：proxy为个股β基准…`。
  上方 `.sample.md` 是修前跑的旧样例（市场环境段落为占位），逻辑已在代码修正、测试覆盖。
- news_ai 当日缺（主仓 news_ai/ 空）→ 消息面主要靠 sentiment 层；生产侧自采（`--collect`，默认开）会补
  news_ai，手跑为隔离走 `--no-collect`。
