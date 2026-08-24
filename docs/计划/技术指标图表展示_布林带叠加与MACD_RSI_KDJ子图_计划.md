# 第一步 · 技术指标图表展示(布林带叠加 + MACD/RSI/KDJ 子图)计划

> 状态:**待用户审阅** · 分支 `claude/strategy-discuss-planning-feac47`(已 ff 到 origin/main e8da0e2)
> 关联:这是"技术指标 → 消息面融合"分步推进的**第一步**。第二步(消息面融合)另见 `三面融合与断点_事件条件化预测_计划.md`,第一步完成后再上。

## 1. 背景与目标

用户定的分步:**先把技术指标这一步彻底做扎实(算出来 + 页面展示清楚),再上消息面融合。**

现状核对结论:
- **算:五个指标(均线/布林带/MACD/RSI/KDJ)全都已在 `tools/analysis/technical.py` 算出,口径对齐通达信,有单测。**
- **展示的缺口:**
  1. 蜡烛图(`web/static/kline_echarts.js`)只叠了 MA5/20/60 + 成交量子图,**布林带没画、MACD/RSI/KDJ 没有子图**;
  2. 图表数据视图(`tools/analysis/chart.py::build_chart`)只序列化了 `dates/OHLC/ma5/20/60/volume`,**没有 boll/macd/rsi/kdj 序列**。

目标:**在现有蜡烛图上叠加布林带上中下轨,并在下方新增 MACD / RSI / KDJ 三个联动子图**,让用户在一张图里看全这几个指标。

## 2. 数据源结论(前期探查已定)

- 现有免费源(腾讯/新浪/东财/baostock)**都只给原始 OHLCV,不给算好的指标**。
- Tushare 有 `stk_factor` 接口能直接返回算好的指标,但**继续自己本地算**——理由:全链路单一通达信口径 + 已有单测锁定,改用外部接口会引入第三方口径,造成"历史(本地算)/实时(接口拉)"漂移,且新增网络/限流/积分依赖。
- **可选加分项(不入主线):** 起子 agent 一次性拉 `stk_factor` 与本地 `technical.compute` 逐位对差,做"口径已对齐"的体检背书。(本轮已并行发起。)

## 3. 架构约束(硬)

`chart.py` 文件头明确:**展示层只读、不算、不 import 分析器**。故所有指标序列**必须在分析层 `chart.py::build_chart` 里预计算写进视图 JSON**,前端 `kline_echarts.js` 只读该 JSON、**不得在 JS 里用 close 现算**。

## 4. 设计(先定输入输出,再填实现)

### 4.1 分析层 `tools/analysis/chart.py`

**改 `build_chart(code, limit)` 的输出契约**,在现有 key 之外新增(全部在 full df 上算、再 `tail(limit)`,与 ma 同法):

| 新增 key | 来源(technical.py) | 说明 |
|---|---|---|
| `boll_mid` / `boll_up` / `boll_low` | `ta.boll(close)` 的 mid/upper/lower | 叠加主图 |
| `dif` / `dea` / `macd_hist` | `ta.macd(close)` 的 dif/dea/macd | MACD 子图(hist 为柱) |
| `rsi6` / `rsi12` / `rsi24` | `ta.rsi(close, w)` | RSI 子图 |
| `kdj_k` / `kdj_d` / `kdj_j` | `ta.kdj(kline)` 的 k/d/j | KDJ 子图 |

- 同步扩 `_EMPTY` 字典(新增 key 给空 list),保证缺数据时向后兼容。
- 数值走现有 `col()` 的 `None`/`round(_,2)` 处理;MACD/KDJ 视需要 round 到 3 位。
- **向后兼容:** 旧 key 全保留;前端对缺失的新 key 用 `connectNulls`/存在性判断跳过,老视图不报错。

### 4.2 前端 `web/static/kline_echarts.js`

从"2 grid(K线+成交量)"扩到"**5 grid**":K线 / 成交量 / MACD / RSI / KDJ。全部照抄现有成交量子图的模式:

- **布林带**:复用已有 `maSeries()` 工厂,加 `boll_up/boll_mid/boll_low` 三条线到主图(grid0/yAxis0);用浅色 `areaStyle` 在上下轨间做淡填充,直观体现"放口/缩口"。
- **MACD 子图**(grid2):`macd_hist` 用 `type:"bar"`(正红负绿)+ `dif`/`dea` 两条线。
- **RSI 子图**(grid3):`rsi6/12/24` 三条线 + 70/30 参考线(markLine)。
- **KDJ 子图**(grid4):`kdj_k/d/j` 三条线 + 80/20 参考线。
- **联动**:新增的 xAxisIndex 全部加入 `axisPointer.link` 与两个 `dataZoom` 的 `xAxisIndex` 数组;`legend.data` 补齐新序列名(用户可点图例开关任一条线)。
- **grid 布局(初稿,实现时微调)**:grid0 top30/height42% · grid1 50%/9% · grid2 62%/10% · grid3 75%/10% · grid4 87%/10%。

### 4.3 模板 `web/templates/stock.html`

- `#klineChart` 容器高度 `460px` → **约 820~880px** 以容纳 5 个子图。
- 标题文案更新为"蜡烛图 + 布林带 + 成交量 + MACD/RSI/KDJ"。
- 若 `klineData` 注入是整份 chart 视图,则新字段自动带出,无需改注入逻辑(实现时确认)。

## 5. UX / 交互把关

- **配色沿用 A 股惯例**:涨红 `#ef232a` / 跌绿 `#14b143`;MA/BOLL/RSI/KDJ 各线用现有低饱和色板,避免刺眼。
- **布林带**用上下轨间淡填充表达通道宽窄(放口/缩口一眼可见),中轨虚线。
- **图例开关**:ECharts 图例原生支持点击隐藏/显示线序列(MA、BOLL、RSI/KDJ 各线),用户可按需精简,零额外成本。
- **子图折叠(可选 v1.1)**:进一步做"MACD/RSI/KDJ 面板整块显隐 + grid 自动重排"(需动态重算 grid 位置),v1 先不做,列为增强。
- 移动端:容器变高后确认 dataZoom 滑块与竖排子图在窄屏可读(验证阶段用响应式检查)。

## 6. 测试(守则 6)

新增 `tests/test_chart.py`(或扩现有):
- **契约**:`build_chart` 返回含全部新 key,且每条序列长度 == `dates` 长度;元素为 `None` 或 `float`。
- **向后兼容**:旧 key(`ma5/ma20/ma60/volume/OHLC`)仍在。
- **数值一致性**:`build_chart` 末根的 boll/macd/rsi/kdj 与 `technical.compute` 同源函数在同一 K 线上算的末值一致(锁"展示层数字 == 分析层数字",防口径漂移)。
- **空数据**:无 K 线时返回 `_EMPTY`(含新 key 空 list),不报错。

## 7. 验证(用户要求:本地看效果 + 远端确认)

1. 本地起 web,打开个股页,**截图**确认:布林带叠加正确、三子图渲染且与主图缩放/十字光标联动。
2. 检查浏览器控制台无报错、图表数据请求正常。
3. 响应式(窄屏/暗色)扫一眼。
4. 远端(线上视图)再确认一遍,截图给用户。

## 8. 不做什么(scope 边界)

- 不改指标算法/口径(只做展示)。
- 不接消息面(那是第二步)。
- 不改数据采集/落盘节奏。
- 子图折叠重排、指标参数可调 UI 等列为后续增强,不进本轮。

## 9. 工作量(agent 视角)

- 预估 token:input ~150K~250K(读现有 chart/technical/js/模板 + 参考 sepa 实现),output 中等(chart.py +~40 行、kline_echarts.js option 重排 ~150~250 行、模板微调 + 测试)。
- 预估 agent 执行:约 1.5~3 小时(读码 + 改 3 文件 + 补测试 + 本地/远端验证)。
- 无新依赖、无架构改动;主要风险仅在多 grid 布局百分比与联动数组对齐,属调参。
