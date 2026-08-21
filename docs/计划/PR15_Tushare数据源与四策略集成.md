# PR#15 提取与集成 · Tushare 可选数据源 + 四策略并入现有框架

> 状态:**待审**（动手前）· 日期:2026-08-21 · 分支:`claude/pr15-tushare-strategies-cc5437`
> 一句话:把外部 PR#15 里真正有价值的两处贡献（Tushare 取数 + 4 个选股思路）**重做**到当前 main 上——Tushare 作**可选数据源**（读得通才用、否则静默回退免费源），4 个策略**并入现有 screener→view→/selection 综合面板**框架（而非另起独立页），并补齐文档 / 单测 / A-B 回测。

---

## 〇、本方案的关键判断（已拍板）

调研了 PR#15 的两个真贡献（`c8050a5` 文档 + `44a46f3` 代码）与我们现有框架后，5 个判断的处置如下(J2/J3 已由用户 2026-08-21 拍板):

| # | 判断 | 处置(已定) |
|---|---|---|
| J1 | PR 把 4 策略做成**独立 `screen_*.py` + 独立网页**;我们的正规约定是**规则型 screener → `put_view` → `/selection` 综合面板**（S01/S02 那套) | ✅ 按我们的约定重做,复用 `_view_picks_section` 零胶水渲染,**不建独立页** |
| J2 | **拉揉搓 ≈ 现有 S01「趋势深跌反包」**(均线多头 + 0.9~1.2×52周高 + 近10日阳>阴 + 当日深跌≥4%收阳,四条几乎逐条重合) | ✅ **合并,不新增重复策略**。在 `docs/策略/` 记录与 S01 的等价性;若"少 MA5 门槛"变体有价值,做成 S01 的一个可选参数模式(不单列策略) |
| J3 | **量价 3 个子信号**(单日放量 / 低位放量 / 连续放量)与 S02「放量后缩量回踩」**互补而非重复**(S02 买放量*之后*的缩量回踩;量价买放量突破*本身*) | ✅ **拆成 3 个独立策略** S04/S05/S06,各一个 view / 面板 section / 回测;**不与 S02 去重删除** |
| J4 | PR 的**单日放量**用 Tushare `daily_basic.turnover_rate` 取换手;但换手率我们的 master **免费就有**(baostock/akshare-spot 已填 `turnover` 列) | ✅ 单日放量**直接读 master 的 `turnover` 列**,不依赖 Tushare;仅当该列 NA 时该票跳过 |
| J5 | PR 的 Tushare 采集**另起一个 factor_scaled 复权 master**,与我们的 **qfq master 口径不一致**,混存会污染 | ✅ **只保留一个 qfq master**;Tushare 作为**可选源接进现有 `_fetch_one_with_source` / `master_sync` 回退链**,产出 qfq 一致数据;丢弃 PR 的独立 factor_scaled master 与没人用的 `daily_basic/float_share` |

> 净结果:**真正 Tushare-only 的只有「最强」策略**(筹码获利比例 `cyq_perf`,免费源拿不到)。其余策略纯 OHLCV / 免费换手即可跑,Tushare 只带来"全市场单次批量取数"的效率红利与"最强"这一新能力。
>
> **最终策略清单(formal S-id):** S03 最大范围 · S04 单日放量 · S05 低位放量 · S06 连续放量 · S07 最强(Tushare-only);拉揉搓并入 S01。

---

## 一、原始需求(统筹 / 任务书原话)

- 数据源:Tushare 作可选源 —— daily 全市场 OHLC+amount+pct_chg、adj_factor 复权、daily_basic 换手率/流通股本,放 `tools/collectors/`(不许放 pipeline/策略层)。
- 口子:`TUSHARE_TOKEN` 配了**且实际读得通** → 用 Tushare;没配或读不通 → **自动回退**现有免费源(baostock 主档 / 腾讯-新浪),**不报错**;网页给**提示(非报错)**,标明当前数据源 / 是否已回退。
- 策略整合(4 个:最大范围 / 最强 / 拉揉搓 / 量价):**接进现有策略框架**(注册 + screenall + `/selection` 综合面板 + 统一 view schema),**不要**另起独立 `screen_*.py` + 独立页面。去重:量价"放量"与 S02 比对;最大范围 / 拉揉搓的"近52周高+均线多头"与 trend-template / 动量比对,重复的合并、不重复的作新策略。
- 「最强」硬依赖 Tushare 筹码获利比例 → 做成**仅 Tushare 可用时才出**,否则跳过 + 页面提示(别用免费源硬凑)。
- 策略取值一律走我们自己的数据/派生(`load_kline_recent` / 指标),别在策略里 ad-hoc 重算或重复取数。
- 架构整理:复用现有(近史加载器、指标、view schema、K线模板);重复的轮子合并掉。
- 规矩:token 只从 env 读、不入库;push 前脱敏("同事 key(WANXIANG)"若带进来必须去掉);commit 不加 AI 署名;分步 commit;每策略单测 + A/B 历史回测报告 + 回执;变更记录写 `docs/日志/开发日志.md` + 本文件,策略文档补 `docs/策略/`。

> 脱敏预检结论:已全量 grep 两个真提交的 diff,**没有任何 "WANXIANG"/万象、硬编码 token/key、真实密钥**;token 一律 `os.getenv("TUSHARE_TOKEN","")`。唯一 PII 是 git author(提交元数据,不进文件,重做时用我们自己身份即可)。即"要脱敏的东西本就不在这两个提交里",但我仍会在重做后再扫一遍。

---

## 二、要实现哪些功能(可验收)

### 数据源
- [ ] F1:新增 `tools/collectors/tushare_daily.py`,提供 `is_configured()` + 全市场日线批量取数 `fetch_daily_all(trade_date)`(返回 `_STD_COLS` schema,vol 手→股 ×100、amount 千元→元 ×1000)+ 筹码 `fetch_chip(trade_date)`(`cyq_perf`,供最强)。token 仅 env 读。（验收:无 token 时 `is_configured()` 为 False 且不抛;有 token 但网络断时上层回退不报错。）
- [ ] F2:把 Tushare 接进 `market._fetch_one_with_source` 的 `sources` 链 + `master_sync.sync_master` 的批量增量路径,`TUSHARE_ENABLED` 关时链路与现状完全一致;开时优先 Tushare、失败静默 fallthrough 到 baostock/akshare/腾讯-新浪。（验收:`meta["source"]` 正确记 `tushare`/`baostock`/`akshare_spot`;关开关跑 `screenall` 结果不变。）
- [ ] F3:网页在**页脚/头部**加**中性徽标**(非报错):`行情来源:Tushare / 免费源(已回退)` + 最新交易日,读已落盘的 `meta["source"]`。（验收:未配 token 时显示"免费源",不出现任何红色报错。）

### 策略(并入 screener→view→/selection)
- [ ] F4:S03「最大范围选股」——纯 OHLC。规则:`C/HHV(C,250)≥0.82` ∧ `C≤HHV×1.10` ∧ 站上 MA10/20/50 ∧ 32日内涨>6%≥1次 ∧ 当日回撤≤4% ∧ 非北交所(前缀 8/4 排除,保留 002)∧ `C>LOW`。复用 `indicators.highest_high/ma`。（验收:单测锁 250日高82%线、32日大阳计数、北交所排除保留 002、历史不足不选。）
- [ ] F5:量价拆成 **3 个独立策略**(各自 view / 面板 section / 回测;共用一个 `screen_volume.py` 模块里的 3 个 `run_*_screen`):
  - **S04 单日放量**:`turnover>1.7×前值` ∧ `C>前收×1.03` ∧ MA200上行 ∧ MA50>MA200(换手读 master `turnover`,见 J4;NA 跳过)。
  - **S05 低位放量**:站上 MA5/10/20/30/200 ∧ 上穿30周线(动态、无未来函数)∧ 近10日最大量。
  - **S06 连续放量**:连续两日走高 ∧ 较前两日各涨>4% ∧ 量递增 ∧ 站上 MA20/50/200 ∧ MA5,MA10>MA20。
  （验收:三策略各有单测 + 各自 view;换手 NA 的票在 S04 下被跳过而非误选。）
- [ ] F6:**S07 最强选股**——**仅 Tushare 可用时出**。规则:六均线多头 ∧ 11日内≥2日涨≥5% ∧ 52周高 90%~120% ∧ (筹码获利比 winner_rate>95% ∨ `HIGH≥cost_95pct`)。无 token/取不到筹码 → **不产出该 view + 面板提示"需 Tushare"**,不用免费源硬凑。（验收:无 token 时该 section 显示提示、不报错;有 mock 筹码时单测命中。）
- [ ] F7:**拉揉搓 → 并入 S01,仅文档记录**(用户 + 统筹 2026-08-21 定)。在 `docs/策略/` S01 相关文档补一段"拉揉搓 ≈ S01 四条规则等价"说明;**不加代码 / 不加 view / 不加 section / 不做 MA5 变体**。
- [ ] F8:4(或3)策略并入 `run_screen_all` 的 `screeners` 列表 + `/selection` 的 `_strategyN_section` + `_combined_section` + `selection.html` section。统一 view schema `入选清单:[{code,明细}]`。所有阈值进 `tools/config/strategy.py` `THRESHOLDS`。

### 文档 / 测试 / 回测
- [ ] F9:每策略一份 `docs/策略/*.md`(规则 / 口径 / 阈值来源指向 strategy.py)+ 更新 `docs/策略/README.md` 索引。
- [ ] F10:每策略单测(`tests/test_screen_sNN.py`)+ A/B 历史回测报告(`docs/计划/SNN回测报告_YYYYMMDD.md`,含开/关某条件的对比)。
- [ ] F11:`docs/日志/开发日志.md` 记"他原来实现了什么 → 我们改成什么样";本文件回填偏差;README 合并 `c8050a5` 的 Tushare 说明(去掉独立页那部分,改指综合面板)。

---

## 三、实现计划

### 3.1 分期 / 步骤(分支内分步 commit)
1. **数据源层**(F1/F2/F3)——先把 Tushare 可选源 + 回退 + 网页徽标打通,`TUSHARE_ENABLED` 关时零影响。→ commit + 回执。
2. **纯 OHLC 策略**(S03 最大范围、S04 量价)——不依赖 Tushare,先落地 + 单测。→ commit + 回执。
3. **Tushare-only 策略**(S05 最强)——含无 token 时的跳过 + 面板提示。→ commit + 回执。
4. **拉揉搓处置**(按 J2 结论)。→ commit。
5. **/selection 面板接线 + 文档 + A/B 回测报告**。→ commit + 回执。
6. 重做后再扫一遍脱敏 → `push origin` 存档 → 交统筹验收合 main。

### 3.2 涉及模块

| 模块 / 文件 | 改动 | 说明 |
|---|---|---|
| `tools/collectors/tushare_daily.py` | 新建 | Tushare 取数(daily 批量 + cyq_perf 筹码);env-only token;返回 `_STD_COLS` |
| `tools/collectors/market.py` | 改 | `_FETCHERS` 加 `_fetch_tushare`;`DEFAULT_SOURCES` 前置 tushare(gated) |
| `tools/collectors/master_sync.py` | 改 | 批量增量路径加 Tushare 优先分支 + try/except fallthrough,新增 `mode="tushare_spot"` |
| `tools/config/settings.py` | 改 | `TUSHARE_TOKEN` / `TUSHARE_ENABLED`(仿 `NEWS_RECALL_ENABLED` 惯例) |
| `tools/config/strategy.py` | 改 | `THRESHOLDS["最大范围选股"/"量价放量"/"最强选股"]` + FORMULAS 文本 |
| `tools/pipeline/screen_max_range.py` `screen_volume.py` `screen_strong.py` | 新建 | 仿 `screen_s02.py`:`load_kline_recent` + `indicators.py` + `run_*_screen()→put_view` |
| `tools/run.py` | 改 | `screeners` 列表 append 3~4 项 + import |
| `web/data_access.py` | 改 | `_strategyN_section` + `selection_page` + `_combined_section` |
| `web/templates/selection.html` `base.html` | 改 | 新 section(复用 strategy2 结构)+ 页脚数据源徽标 |
| `requirements.txt` | 改 | 放开 `tushare>=1.4.0`(注:可选,未装不影响关态) |
| `tests/test_screen_max_range.py` 等 | 新建 | 每策略单测 |
| `docs/策略/*.md` `docs/日志/开发日志.md` `README.md` | 改/新建 | 文档 |

**不做 / 丢弃**(相对 PR):独立 `web/templates/strategy_*.html` 页与 `/strategy*` 路由;PR 的 factor_scaled `bootstrap_master`;`daily_basic/float_share`(无人消费);`scheduler.py` 里未注册的死代码 `_run_max_range`;terse 一行流写法(改为与 main 一致)。定时任务(scheduler)默认**不开**,只留开关(仿 PR 的默认 OFF)。

### 3.3 接口 / 契约
```
# 采集
tushare_daily.is_configured() -> bool
tushare_daily.fetch_daily_all(trade_date: str) -> pd.DataFrame   # _STD_COLS
tushare_daily.fetch_chip(trade_date: str) -> pd.DataFrame        # code,winner_rate,cost_95pct
market._fetch_tushare(code, start, end, adjust) -> pd.DataFrame  # 融入 _fetch_one_with_source

# 策略(每个规则型 screener 统一)
run_max_range_screen(codes, as_of=None, fetch=True) -> dict      # put_view 入选清单:[{code,明细}]
run_volume_screen(codes, as_of=None, fetch=True) -> dict
run_strong_screen(codes, as_of=None, fetch=True) -> dict|None    # 无 Tushare 返回 None/带提示
```
view schema 沿用既有 `{as_of,策略,扫描数,有效样本,入选数,入选清单:[{code,明细}],规则,防未来函数}`,不改契约。

### 3.4 工作量估(agent 视角)
- Token:input ~150–200K / output ~40–60K 量级(含 3 个调研 subagent 已花的 ~240K + 后续实现 + 回测数据往返)。
- Agent 工时:~2.5–4 小时(含单测 / A-B 回测跑数 / 审阅来回)。可按 3.1 的 6 步拆轮。

## 四、关联
- 对应 `docs/工程进度.md`:选股策略扩充(S03–S05)+ 数据源可选化。
- 依赖:`indicators.py`(近史指标)、`load_kline_recent`(近史加载)、`_view_picks_section`(面板渲染)、`_fetch_one_with_source`(源回退)。
- 待用户拍板:J2(拉揉搓合并/独立)→ 若合并需同步 `docs/策略/选股策略.md`。

## 五、验收 / 测试标准
- 预期效果:①未配 token 时 `screenall`/网页与现状**完全一致**、无报错、页脚显示"免费源";②配了 token 且通 → 页脚显示"Tushare"、`meta.source=tushare`;③S03/S04 在无 Tushare 下正常出票;④S05 无 Tushare 时面板显示"需 Tushare"而非空报错;⑤拉揉搓不产生与 S01 重复的冗余票。
- 测试:每策略 `tests/test_screen_*.py` 断言**锁语义**(如"32日大阳计数≥1才可能入选""换手 NA 必跳过单日放量""无 token 时 strong 返回提示不抛");回退单测断言"Tushare 抛错时 `_fetch_one_with_source` 落到 baostock 且不冒泡"。A/B 回测报告对每策略给"开/关关键条件"的入选数与前瞻收益对比。

---

> 完成后回填:实际与计划偏差(哪些改了、为什么、遗留什么)→ 同步开发日志。
