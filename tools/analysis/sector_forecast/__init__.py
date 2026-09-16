"""板块预测系统(P1:板块环境每日面板 + 四角色主表)。

设计见 docs/计划/2026-09-16_板块预测系统_设计.md。**因果 PIT**:一切值只用 ≤date 数据。

模块划分(先框架后填充):
  universe.py    —— 全A 按申万一级聚合的**单次加载器**(一次 load 同喂面板 + 角色识别)
  labels.py      —— 冷热标签(过冷/正常/过热/拐点)预注册阈值 + 分类
  regime_panel.py—— 每日板块环境面板:拥挤/动量(温度计)+ 广度/资金 → sector_regime.json
  roles.py       —— 四角色识别(龙头/中军/补涨/弹性),§5 可计算定义
  roster.py      —— 角色主表 build + 落库 data/sector_roster/<板块>.json
  suggest.py     —— 目标板块自动建议(成交占比 + 涨幅榜 + 政策命中)
  cli.py         —— python -m tools.analysis.sector_forecast --date <YYYY-MM-DD>

诚实边界(P1):
  · 口径 = **申万一级 31 板块**;概念级细分(算力/低空)成分缺,延后 P2+。
  · 个股资金流(fundflow)口径常滞后 as_of ~5 天 → 资金维度优先用**板块级成交额 + 广度**,
    个股 fundflow 仅辅助并带新鲜度标签。
  · 无专门涨停采集器 → 涨停由 pct_chg + 板块限价派生(复用 breadth.is_limit_hit,同一启发式)。

⚠️ 测试环境研究模拟,非投资建议;只读行情、不下单。
"""
