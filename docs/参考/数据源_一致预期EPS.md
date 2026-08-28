# 数据源 · 机构一致预期(前瞻 EPS)

> 新增日期：2026-08（借鉴 a-stock-data §2.2）
> 采集器：`tools/collectors/consensus.py`　落盘 kind=`consensus`
> 一句话：项目现有 `fundamental` 只有**已披露历史财务**，缺「市场对未来的预期」；本源补上前瞻维度。

---

## 1. 为什么做

历史财务是「后视镜」，而股价定价的是**预期**。机构一致预期(当年/次年预测 EPS、覆盖机构数)让分析层能算隐含增速、前瞻 PEG、预期差(预期 vs 已披露)，是成长股筛选与「预期反转」信号的关键输入。

## 2. 数据源

主源东财、兜底同花顺，均经 **akshare**：

| 顺序 | 接口 | 说明 |
|---|---|---|
| 主 | `stock_profit_forecast_em(symbol=code)` | 东财盈利预测，免鉴权、字段较稳 |
| 兜底 | `stock_profit_forecast_ths(symbol, indicator="预测年报每股收益")` | 同花顺(与 a-stock-data 同源 10jqka)，东财无覆盖时启用 |

> **两源皆空 → 落空(降级)，不伪造预期。**
> 年度列/EPS 列/机构数列均按关键词**防御式匹配**(`_year_col`/`_eps_col`/`_inst_col`)，容忍列名漂移：
> EPS 列优先精确名，退化为「含『每股收益』且不含最小/最大」的列。

## 3. 落盘字段

`{"forecast": {年度: {eps, insts}, ...}, <派生摘要>}`，派生摘要 `summarize`：

| 字段 | 含义 |
|---|---|
| `预期EPS当年` | 最早一个 ≥ 今年的预测年度 EPS |
| `预期EPS次年` | 次一预测年度 EPS |
| `预期增速` | 次年/当年 - 1(当年 EPS>0 才算，否则 None) |
| `覆盖机构数` | 当年预测的机构/研报数(**<3 家置信度低**，分析层需谨慎) |

## 4. 更新逻辑(编排)

- **全量**：`run.collect_values` 调 `consensus.fetch_consensus(codes)`，走 `FETCH_TIMEOUT` 短超时。
- **补缺**：`run.collect_values_missing` 对无 `consensus` 缓存的票补采。
- **降级**：单票两源皆空/失败 → 记 log 跳过；**港股** → 落空(`source="none(hk)"`)。

## 5. 读取

```python
from tools.collectors import consensus
rec = consensus.load_consensus("600519")
# rec["预期EPS当年"], rec["预期EPS次年"], rec["预期增速"], rec["覆盖机构数"]
# rec["forecast"] = {"2026": {"eps":.., "insts":..}, "2027": {...}}
```

前瞻 PEG 需现价 → 留分析层：`现价 / 预期EPS当年 / (预期增速*100)`。

## 6. 局限

- 一致预期是机构主观预测，**存在系统性乐观偏差**，宜与已披露财务做「预期差」交叉验证。
- 覆盖机构数少(冷门/小盘股)时预测噪声大，建议 `覆盖机构数 >= 3` 才纳入因子。
- 港股不覆盖。
