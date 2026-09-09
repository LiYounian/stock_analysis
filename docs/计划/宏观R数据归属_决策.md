# 宏观 R 数据归属 · 决策文档

> 版本:v1 · 2026-09-09 · 状态:**已拍板(2026-09-09,数据源窗)** → 采纳**方案 A**(数据线新建 `macro.py` + `kind="macro"`,归数据线)。实施时机:阶段1(E/V/S)不阻塞,按推荐**等 REVS 阶段2 启动时再做宏观 collector**(若统筹要求提前排期另议)。
> 背景:origin/main 18aa528 合入 REVS 四因子(R/E/V/S)设计。R = Regime 宏观环境,**阶段2**才需要,
> 需 PMI/CPI/信用利差 等宏观序列。要拍板:这些宏观数据**由数据采集线统一新建 collector 产出**
> (供 REVS 消费)vs **REVS 模块自建 macro.py 自拉**。本文只做归属决策,不设计宏观指标本身。
> 依据:REVS 设计文档 `docs/计划/2026-09-09_REVS四因子模型_设计与价值论证.md` + collector/store/analysis 三层现状核查。

---

## 一、REVS-R 的宏观需求(事实)

- **要哪些**:PMI(制造业月频,akshare `macro_china_pmi`)、CPI(月频 `macro_china_cpi_monthly`)、
  信用利差(`bond_china_yield` 自算,口径未定)。
- **怎么用**:R 明确是**轨道二 Overlay 择时闸**——三指标各自打分→加权 regime 分→分档→控"整体出不出票 / TopK 大小 / 仓位",**不进个股横截面**(设计文档已论证 R 作截面常数不改变排序)。
- **防未来硬红线**:PMI/CPI 滞后发布,必须按**实际发布日**对齐;信用利差可日频。
- **现状**:设计文档自陈"现有数据管线**完全没有**宏观序列,是本方案唯一新数据接入";全仓 grep
  `macro_china_pmi|macro_china_cpi|bond_china_yield` **零命中**,无 `macro.py`。
- **阶段无关性**:阶段1(E/V/S)**无新数据**,宏观只在阶段2需要 → **本决策不阻塞阶段1**。

## 二、既有架构惯例(判断依据)

标准链路(以 `tools/pipeline/regime.py` 为范本):**collector 拉数 → store 落盘 → pipeline 编排 →
analysis 纯计算消费**,三层不互相 import、只经 store 交换。现有 regime 轮子
(`pattern_screener/regime.py`)纯量价+宽度、无任何宏观输入。

## 三、两方案对比

| 维度 | 方案A:数据线统一新建 collector | 方案B:REVS 自建 macro.py |
|---|---|---|
| 动的文件 | 新建 `tools/collectors/macro.py` + `store.repo` 注册 `kind="macro"` + REVS 经 store 消费 | 仅 REVS 侧(analysis/strategy 内直连 akshare) |
| 是否守分层 | ✅ 守 collector→store→analysis 惯例 | ❌ analysis 层直接联网抓数,违背"输入由 pipeline 备好、analysis 纯计算" |
| 落盘/as_of 复用 | ✅ 落 store(仿 `equity_financing` 伪 code `_market`),带发布日、可 as_of 防未来、他策略共享 | ❌ 不落盘或各自落,as_of 对齐各写各的、易漏 |
| 复用价值 | ✅ 宏观 regime 是"可复用基础设施",外溢价值超出 REVS(大盘预测/其他择时都能用) | ❌ 锁死在 REVS 内 |
| 数据线一致性 | ✅ 与"数据获取层整顿"同盘,统一 retry/fallback/口径 | ❌ 游离于数据线治理之外,又一个"analysis 私自抓数"的口子 |
| 脏活风险 | 信用利差口径、akshare 宏观接口历史深度/稳定性需实拉验证(collector 侧承担) | 同样的脏活,但塞进 REVS 计算逻辑里,耦合更重 |
| 落地成本 | 略高(多一个 collector + kind 注册) | 略低(就地写),但技术债 |

## 四、推荐:方案 A(数据线统一产出宏观 R)

**理由:**
1. **守分层**:项目所有 regime/预测模块都遵循"输入由 pipeline 备好、analysis 纯计算";让 REVS 破例
   直连抓数,是开"analysis 私自联网"的坏头(正是本轮数据整顿要收敛的反模式)。
2. **防未来最稳**:宏观按发布日对齐是硬红线,落 store + as_of 闸门是项目现成的防未来机制;
   REVS 自拉难保证不引入未来函数。
3. **复用外溢**:宏观 regime 是基础设施,大盘预测/其他择时都能吃,归数据线一次建、多处用。
4. **与数据整顿同盘**:统一走 retry/fallback/口径归一,不再多一个游离数据源。
5. 设计文档 §9 落地清单本身也倾向 A(新 collector + `kind="macro"`),本决策与之一致。

**落地要点(A 采纳后,阶段2 再实施):**
- 新建 `tools/collectors/macro.py`:akshare PMI/CPI + 债券收益率自算信用利差;**按发布日对齐**;
  接口挂了有 fallback(接 `retry_call`)。
- `store.repo` 注册 `kind="macro"`;单序列全市场共享 → 仿 `equity_financing` 用伪 code `_market`
  落盘,每条带**披露/发布日**供 as_of 闸门。
- REVS regime 合成经 `store.get_raw("macro", "_market", ...)` 消费,与 collector 解耦。

## 五、决策记录

- [x] **采纳方案 A**:数据线新建 `macro.py` + `store` 注册 `kind="macro"`,归数据采集线(用户 2026-09-09 拍板)。
- [ ] ~~方案 B(REVS 自建)~~ — 未采纳(破分层)。
- **实施时机**:归属定为 A;阶段1 无新数据不阻塞,按推荐**等 REVS 阶段2 启动时再实施**宏观 collector(统筹如需提前排期另议)。

**下一步**:归属已定,A 的宏观 collector 详细设计(指标口径/发布日对齐/kind 落盘契约)等阶段2 启动时另起文档。
未决脏活:信用利差口径、akshare 宏观接口历史深度/稳定性,均需实拉验证。
