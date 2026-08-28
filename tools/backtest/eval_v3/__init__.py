"""eval_v3 — 评测框架 v3(双轨 · 六维 · T+1 入场口径)。

只升级**评测方法**,绝不改任何策略逻辑/权重/strategy.json。核心分层(先框架后实现):

  schema   预测记录契约(统一表:strategy/pred_date/code/direction/rank_score/source/stype/replayable)
  prices   PriceBook(缓存 open/high/low/close + date→idx,供 T+1 入场定价)
  scoring  统一打分层(T+1 入场→各 horizon 实现收益→方向命中/收益质量/超额/显著性;排序型走 rank-IC)
  live_source   从 data/analysis/<日期>/ 读线上实际落盘预测 → 预测记录表(source="live")
  replay_source 用本地 kline 历史复现确定性策略预测 → 预测记录表(source="replay")
  report        双轨 · 六维报告渲染(docs/策略成绩报告.md)

两条评测轨**分开报、不混**:
  · live 观测轨:验"线上系统跑对没",上线仅约 14 交易日,样本薄。
  · 回放回测轨:对确定性/纯技术策略复现历史预测,覆盖近5日/近1月/近1季/近1年及全史。
不可复现的策略(策略0多专家含新闻/LLM/情绪、策略9最强选股含 Tushare 筹码)仅 live 观测、不可回放。

非投资建议。产物只写 worktree 本地。
"""
