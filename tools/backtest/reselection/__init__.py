"""续选/继续持有 vs 无状态重挑(H1)+ gap-fade 入场纪律 vs 降权(H2)回测。

⚠️ 测试环境研究模拟,非投资建议。KPI = 绝对收益(Model A 口径:
D 选 → D+1 估量价入场 → D+1 收盘绝对为正 → D+2 卖出线)。

复用基建(不重造):
  · nextday 口径(限价回踩 marketable 成交 / 涨停不可买 / 10bps 成本 / 全A等权基准 / β)
    ← tools.research.selection_alpha.nextday_kernel(等价性由该 chip 的 tests 锁)。
  · 持续型排名视图打分 ← tools.strategy.momentum.weighted_log_momentum(向量化复刻,
    等价性由本包 tests 锁)+ tools.backtest.backtest_rank._score_council(策略0合议)。
  · winner_rate as-of ← tools.collectors.chip.summarize_asof + tools.backtest.backtest_strong。

防未来(硬红线):任一决策只用 ≤D 数据;限价成交用当日 low/open 判触及;基准=当日全A等权。
产物只写 worktree 本地 data/backtest_local/,不写主检出。
"""
