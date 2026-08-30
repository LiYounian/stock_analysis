# 统一风控 veto 汇聚器 · 设计与接入计划

分支:`feat/risk-veto-aggregator`(基于 main 3dd9ca2)

## 背景:两轴各接各的出口
1. **财报红旗(财报质量轴)**:`config.strategy.redflag_adjust(base, high_flag_count)` 纯函数;
   动作=降权(排序分=综合分−罚分)或否决沉底。消费侧:`web/data_access`(选股页重排)、
   `tools/pipeline/screen_council`(全A策略0)。
2. **龙虎榜否决(市场微结构轴)**:`tools/backtest/lhb_veto.verdict_from_events / entry_veto_asof`
   产 `Verdict`(triggered/reason/n_recent);盘后披露 T+1 生效(`list_date < as_of` 严格)。
   **尚未接入任何选股排序**。

## 目标
把两轴收进**同一个风控出口**:正交 OR 合成(任一轴触发即否决/降权),统一两种动作语义
(红旗=降权减罚分、龙虎榜=否决/离场),给一致的 `Verdict集 → 排序分调整` 映射。
**向后兼容红线**:无龙虎榜数据(verdict=None/未触发)时,输出与现状 `redflag_adjust` 完全一致。

## 设计:分两层(镜像红旗现有分层,守依赖方向)

### A. 纯映射层(config 层,零依赖,web 可 import)
`tools/config/strategy.py`:
- 新增 `THRESHOLDS['风控汇聚']`:总开关 + 龙虎榜轴(启用/模式降权|否决/触发罚分/按条数加权/
  条数上限/否决沉底保留展示/窗口天数/最小净买占比)+ 两轴合成罚分上限。
- 新增纯函数 `risk_veto_adjust(base_score, high_flag_count, lhb_verdict=None, cfg=None)`:
  - **财报轴**:读 `财报.红旗接入`(与现状同口径,**不受汇聚总开关影响** → 红旗永不回归)。
  - **龙虎榜轴**:读 `风控汇聚.龙虎榜`,受 `风控汇聚.启用` 总开关门控(可整体关停新轴)。
  - **OR 合成**:降权轴罚分求和后封顶;否决 = 任一轴否决;剔除 = 任一轴否决且不保留展示。
  - 返回统一 dict:`应用/模式/罚分/否决/剔除/原始分/排序分/高危数/归因/各轴`
    (`高危数` 等键向后兼容红旗消费侧)。
- 保留 `redflag_adjust/redflag_penalty` 不动(现有测试/消费侧兼容)。

### B. 数据生产层(analysis 层,可 import backtest)
`tools/analysis/risk_veto.py`(新):
- `lhb_verdict_asof(code, as_of, ...) -> dict | None`:调 `lhb_veto.entry_veto_asof` 取 as-of
  无未来函数裁决,`.to_dict()`;无快照/未触发返回轻量 dict 或 None。
- 供 `serialize`(把 verdict 挂进 record['lhb_veto'],web 只读取用)与 `screen_council` 用。

## 接入闭环
1. `serialize.build_record`:挂 `rec['lhb_veto']`(as_of 入选否决裁决;缺 → None)。契约宽容新键。
2. `screen_council.run_council_screen`:排序改调 `risk_veto_adjust`(带 lhb verdict)。
3. `web/data_access`:`_rerank_scored`/`_demote_flagged` 改调 `risk_veto_adjust`,
   lhb verdict 从 `recs[code]['lhb_veto']` 读(纯数据,不 import 分析器,守 §9.3)。
4. `run.py`:加"收盘后采当日龙虎榜"步骤(T 落盘、T+1 由 `list_date < as_of` 生效)。
   **不自动启用 cron**;采集代码就位由用户拍板接入定时。

## 防未来函数(红线)
- 龙虎榜 `list_date < as_of` 严格小于(lhb_veto 内核已锁,汇聚器不放松)。
- 汇聚器纯函数只吃 (base, count, verdict, cfg),不触数据/网络。

## 测试(tests/test_risk_veto.py)
OR 合成 / 单轴触发(仅红旗、仅龙虎榜)/ 两轴叠加剂量封顶 / 无龙虎榜=红旗现状回归 /
禁用总开关=红旗现状 / 否决 OR / 剔除 OR / 纯函数同入参同出参(防未来)。
"""
