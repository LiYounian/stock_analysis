# 图表库调研:TradingView Charting Library(Advanced Charts)是否引入

> 状态:v1.0 · 2026-08-07 · 调研文档(不改代码、不接入)
> 范围:评估 [tradingview/charting-library-examples](https://github.com/tradingview/charting-library-examples) 背后的
> **TradingView Charting Library**(现官方名 **Advanced Charts**,需授权的专业版)是否值得本项目引入,替换/补充现有 ECharts K 线。
> 结论先行,后附授权门槛、接入成本、能力对比、部署可行性、最小接入步骤。

---

## 0. TL;DR + 推荐

**推荐:保持 ECharts,暂不引入 TradingView Advanced Charts。**

一句话理由:Advanced Charts 的核心价值是**给"人"做主动技术分析**(画线、切周期、叠加指标、保存布局);而本项目是**"后端预生成静态日线快照 + 只读展示 + 结论已算好"**的架构,这些交互能力**基本用不上**;换来的却是三重实打实的成本——(1) 需**申请授权**、私有分发;(2) **免费授权只允许"公网、免登录、免费"的服务**,与我们展示端计划的 **token 鉴权**直接冲突,内部/带鉴权使用需转**付费** Trading Platform 授权;(3) 库文件**不得进公共代码仓、项目不得开源**,且要**自托管**(不能走公共 CDN),接入还要写一层 Datafeed 适配。**投入产出比不成立。**

**触发重估的条件**(满足任一再回头看):
- 展示端要做成**面向公众、免登录、免费**的 K 线浏览器(此时免费授权门槛消失);
- 明确需要**用户侧交互分析**(画趋势线/斐波那契、自选叠加指标、切分钟/周/月周期、保存个人布局);
- 需要**海量 symbol 搜索 + 多品种对比**的交易台式体验。

在那之前,ECharts 完全够用,且零授权、可 CDN、可开源、与现有"列式 chart 视图"直连。

---

## 1. 它是什么 / 授权(头号门槛)

### 1.1 三条产品线要分清
| 产品 | 授权 | 用途定位 | 与本项目关系 |
|---|---|---|---|
| **Lightweight Charts** | 开源 Apache-2.0(需标注 TradingView) | 轻量、免费、可商用 | 之前三选一比较过,已知项 |
| **Advanced Charts**(即老名 Charting Library) | **免费但需申请授权**,条款严格 | 面向**公司**、部署在**公网免费服务**上的完整专业图表 | **本次调研对象** |
| **Trading Platform** | **付费**商业授权 | 在 Advanced Charts 之上加下单/交易台功能 | 私有/带鉴权场景的合规出路 |

> `charting-library-examples` 仓库本身是 MIT,但那只是**示例代码**;README 明确:"All of them would obviously require a granted access to the library"——**真正的库文件需单独申请授权**才能拿到。

### 1.2 获取方式
- 到 TradingView 官网 Advanced Charts 页面**提交申请**→ 审核通过后获得**私有分发**的库文件(通常是私有 GitHub 仓授予访问,或下载包)。
- 不是 `npm install` 就能拿到的公开包;**需人工申请、审批,周期不确定**(具体时长/材料**未公开,建议后续人工确认**)。

### 1.3 免费授权的硬性条款(**关键约束,决定能不能用**)
根据官方 Free Advanced Charts Agreement 与官网说明,免费授权要求(逐条对照本项目):

| 免费授权条款 | 本项目现状 | 是否冲突 |
|---|---|---|
| 仅授予**公司**用于**公网 Web 项目**,**不**用于个人/学习/测试/内部 | 工具目前是内部+合作者使用 | ⚠️ 可能不满足 |
| 必须是**免费对公众开放**的服务(free offering) | 展示端规划**公网可访问**,但… | 需确认是否"免费公众服务" |
| **访客无需登录**即可访问("available to any visitor without the need to log in") | 展示端规划用 **token 鉴权**(见 `docs/计划/展示端与数据同步.md`) | ❌ **直接冲突** |
| 必须**保留 TradingView 归属标识**可见 | — | 可接受 |
| 库文件**不得**托管于任何**公共代码仓**;项目**不得开源** | 本仓为**私有**仓(满足"非公共仓");但限制了未来开源 | ⚠️ 部分受限 |

**结论(授权层):** 我们展示端计划的 **token 鉴权** 与"免登录公众访问"这条免费授权硬要求**正面冲突**。若坚持带鉴权/内部使用,免费授权**不适用**,只能走**付费 Trading Platform 授权**(费用**未公开,需商务洽谈确认**)。这是"不引入"的**第一决定性理由**。

---

## 2. 接入成本

### 2.1 它要求的数据接口(Datafeed API / UDF)
Advanced Charts 不直接吃数组,而是要你实现一个 **Datafeed 对象**(JS),库通过回调向它要数据:

| 方法 | 必需? | 作用 | 我们要提供什么 |
|---|---|---|---|
| `onReady` | ✅ | 返回 `DatafeedConfiguration`(支持的周期、交易所、品种类型、能力位) | 固定配置:仅日线(`D`),A 股市场 |
| `resolveSymbol` | ✅ | 返回 `LibrarySymbolInfo`(时区、交易时段、价格精度、支持周期) | 每只票的元信息(名称、精度、`Asia/Shanghai` 时区、A 股时段) |
| `getBars` | ✅ | 返回某时间范围的历史 K 线(升序 `Bar[]`) | 把我们的 chart 视图转成 `Bar[]` |
| `searchSymbols` | 视需要 | 品种搜索联想 | 可用我们的票池;无搜索需求可给最简实现 |
| `subscribeBars`/`unsubscribeBars` | **可选** | 实时推送订阅 | **我们不需要**(静态日线),给空实现即可 |

**好消息:实时订阅是可选的**。官方文档确认:"subscribeBars and unsubscribeBars are optional for static charts … A chart displaying only historical daily bars requires no streaming implementation—getBars alone suffices."→ **我们的静态离线日线数据完全喂得进去。**

### 2.2 `Bar` 形状 vs 现有 chart 视图(数据映射)
- TradingView `Bar` = `{ time, open, high, low, close, volume }`,**一根一对象、按时间升序**;`time` 为 **Unix 毫秒时间戳**,**日线对齐到 00:00:00 UTC 当交易日**;价格必须是数字。
- 我们的 chart 视图(`web/data_access.py::get_kline`)是**列式**:
  `{dates:[], open:[], high:[], low:[], close:[], ma5:[], ma20:[], ma60:[], volume:[]}`。

**映射表:**

| 我们的字段 | TradingView 侧 | 转换 |
|---|---|---|
| `dates[i]`(`YYYY-MM-DD`) | `bar.time` | 解析为当日 **00:00 UTC** 的 Unix **毫秒**戳 |
| `open/high/low/close[i]` | `bar.open/high/low/close` | 直接取,保证是 number |
| `volume[i]` | `bar.volume` | 直接取 |
| `ma5/ma20/ma60[i]` | **无直接位置** | ⚠️ 库自带指标引擎,MA 由库自算;我们预算的 MA 若要显示需走**自定义 study/overlay**,否则**闲置浪费** |
| 无 | `symbolInfo`(时区/时段/精度) | 需**新造**:A 股 `Asia/Shanghai`、时段 `0930-1130,1300-1500`、价格精度 2 位 |

**适配层工作量估计:**
- 一个 JS `Datafeed` 适配器(onReady/resolveSymbol/getBars/searchSymbols + 空的 subscribe):**约 150–300 行**;
- 一个后端数据出口:要么复用现有"chart 视图"JSON、由前端转 `Bar[]`(纯前端适配,后端零改),要么加一个 **UDF 风格**只读接口。走前者最省——**后端可零改**;
- symbolInfo 的 A 股时区/时段/精度**需新造一份映射**(小工作量,但要对);
- 预算的 MA 与库自带指标**语义重叠**,要决定"用库的指标"还是"塞自定义 study",增加纠结成本。

> 净评估:接入**技术上可行且不算大**(数据能喂进去),但纯属为"换个渲染器"付出的**额外适配 + 自托管 + 授权**成本,且我们后端已算好的 MA/信号在它的指标体系里**位置尴尬**。

---

## 3. 能力增益(vs 现有 ECharts)

| 能力 | ECharts(现状) | Advanced Charts | 本项目**用得上?** |
|---|---|---|---|
| 蜡烛图 + 成交量 | ✅ 已实现 | ✅ | 二者都够 |
| 均线叠加(MA5/20/60) | ✅ 后端预算、直接画 | ✅ 库自算(我们预算的会闲置) | 用得上,ECharts 已满足 |
| A 股涨红跌绿 | ✅ 已定制 | ✅ 可配 | 用得上,已满足 |
| **画线工具**(趋势线/斐波/标注) | ❌(需自撸) | ✅ 强项 | ❌ 只读展示,不需要 |
| **50+ 内置指标**(MACD/KDJ/RSI…) | 需自己画(我们已在后端算好判定) | ✅ 前端可切换 | ❌ 判定已在后端算好并给结论 |
| **多周期切换**(分/周/月) | 需自备多周期数据 | ✅ 内置 | ❌ 当前仅日线,无多周期数据 |
| **复权**(前/后复权) | 后端处理即可 | ✅ 内置 | ⚠️ 后端处理更可控 |
| **多品种对比叠加** | 需自撸 | ✅ 内置 | ❌ 单票评估页,无对比诉求 |
| **保存/加载个人布局** | ❌ | ✅ | ❌ 无用户账户体系 |
| **品种搜索联想** | ❌(票池有限) | ✅ | ❌ 票池就几十只 |
| 嵌入式浏览器渲染稳定性 | ✅ 单 canvas,已验证稳 | ✅(iframe/独立 bundle) | 二者都行 |
| 授权 / 成本 | ✅ 免费 MIT/Apache 系、可 CDN | ⚠️ 需申请、免费授权限公众免登录、否则付费 | **ECharts 完胜** |

**净结论:** Advanced Charts 多出来的能力**几乎全是"给人做主动分析"的交互功能**——画线、切周期、自选指标、对比、存布局。本项目的产品形态是**"结论已由后端算好、页面只读呈现"**,这些交互**不在需求内**。真正共用得上的(蜡烛+量+均线+涨红跌绿)ECharts **已经全做到了**。

---

## 4. 嵌入 / 部署可行性

- **架构契合度:** 我们是"后端预生成静态数据 + 只读展示"。Advanced Charts 通过**自定义 Datafeed**完全支持静态历史数据(无需实时流),**技术上能跑**。
- **自托管要求:** 库文件必须**自行托管**(自己服务器发静态 bundle),**不能用公共 CDN**,也**不得放进公共代码仓**。这与 ECharts"CDN 一行 `<script>` 引入"的轻便相比,是明显退步。
- **公网展示端的授权风险(见 §1.3):** `docs/计划/展示端与数据同步.md` 规划展示端用 **token 鉴权**。免费 Advanced Charts 授权要求"访客免登录即可访问",**带 token 鉴权即不满足免费授权** → 需付费授权。这是部署层面的**硬约束**,不是技术问题而是合规问题。
- **私有仓 OK 但限制开源:** 本仓 `stock_analysis` 为私有仓,满足"不进公共仓";但引入后**该项目将永久不能开源**,是一项长期隐性约束。

---

## 5. 结论与建议

**明确推荐:【保持 ECharts】。** 不引入 TradingView Advanced Charts。

**理由汇总(按权重):**
1. **授权冲突(决定性):** 展示端的 token 鉴权 vs 免费授权"免登录公众访问"要求正面冲突;内部/带鉴权使用需转**付费**授权,费用需商务洽谈(未公开)。
2. **能力错配:** 它的增量价值(画线/多周期/自选指标/对比/存布局)全是**交互分析**能力,本项目"只读展示、结论预算"的形态**用不上**;真正需要的(蜡烛+量+均线+涨红跌绿)ECharts 已满足。
3. **额外成本:** 需申请授权 + 私有分发 + 自托管(不能 CDN)+ 写 Datafeed 适配层 + A 股 symbolInfo 映射;且后端已算好的 MA/信号在其指标体系里位置尴尬。
4. **长期约束:** 引入后项目不得开源、库不得进公共仓。

**不推荐"混合试点"的理由:** 混合(主图 ECharts、某页试点 TradingView)并不能规避 §1.3 的授权门槛——**只要页面用了它、且展示端带鉴权,授权问题照样存在**;反而多维护一套渲染栈。故不建议试点,除非"公众免登录 K 线浏览器"这一新产品形态被明确立项。

---

## 6. 若将来要引入的最小接入步骤(备查,当前不执行)

前提:先确认满足免费授权(公众、免登录、免费)**或**已购付费授权。

1. **申请授权**,获得私有库文件;阅读并确认 License 条款(尤其鉴权/开源/归属)。
2. **自托管** charting library 静态 bundle 到自己服务器(不进 git 公共仓、不走公共 CDN)。
3. 写 **Datafeed 适配器**(JS):`onReady`(仅日线 `D`)、`resolveSymbol`(A 股 `Asia/Shanghai` + 时段 + 精度)、`getBars`(把 chart 视图列式数据转 `Bar[]`,`time` 对齐 00:00 UTC 毫秒戳、升序)、`searchSymbols`(读票池)、`subscribeBars/unsubscribeBars` 给**空实现**(静态无实时)。
4. 决定 **MA 策略**:用库自带 MA 指标(丢弃后端预算的 MA),或塞自定义 study 显示后端 MA(二选一,避免双份)。
5. 用 Widget 构造器初始化图表,`disabled_features`/`enabled_features` 裁掉不需要的交互(减小复杂度)。
6. 在展示端保留 **TradingView 归属标识**;更新 `docs/设计/数据结构说明.md` 说明数据映射。
7. 加前端渲染回归验证(与 ECharts 版对比同一只票同一日的蜡烛/量/均线一致)。

---

## 7. 参考链接
- TradingView Charting Library 示例集(README 明确需授权):https://github.com/tradingview/charting-library-examples
- Advanced Charts 官网(申请入口 / 免费授权说明):https://www.tradingview.com/advanced-charts/
- Free Advanced Charts Agreement(免费授权协议 PDF):https://s3.amazonaws.com/tradingview/charting_library_license_agreement.pdf
- Datafeed API 文档(必需方法 / Bar 形状 / 实时可选):https://www.tradingview.com/charting-library-docs/latest/connecting_data/Datafeed-API/
- Datafeed 常见问题:https://www.tradingview.com/charting-library-docs/latest/connecting_data/Datafeed-Issues/
- Lightweight Charts(开源对照项,Apache-2.0):https://github.com/tradingview/lightweight-charts

> 注:免费/付费授权的**具体费用、申请周期、审核材料**官方**未完全公开**,以上条款依据官网与公开授权协议整理;真正引入前**建议由商务/法务人工向 TradingView 确认**当前条款。
