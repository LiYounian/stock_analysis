"""大盘预测 v0.5(docs/计划/大盘预测策略.md §4)。

三维因子(技术 + 市场广度 + 消息面)→ 沪深300 / 全A等权代理 的 T+1/T+5 涨跌方向概率 + 档位。
子模块:
  · dataroot   —— worktree 兼容的主档/analysis 数据根解析 + monkeypatch store
  · breadth    —— 市场广度聚合器(扫 master/kline,涨停线按板块;纯本地,可回溯全历史)
  · sentiment  —— 消息面因子(读 analysis/*/sentiment_policy.json 聚合日度净利好度)
  · technical_index —— 指数技术因子(复用 tools.analysis.technical 的算子,向量化 as-of)
  · features   —— 拼特征面板 + 构建标的收盘序列(沪深300 / 全A等权代理)+ 标签
  · predictor  —— 可解释因子加权 / 逻辑回归预测器 → 方向概率 + 五档 + market_forecast.json

防未来函数硬红线:所有因子只用 ≤T 信息;标签是 T+1/T+5 前瞻收益。
非投资建议:测试环境研究模拟。
"""
