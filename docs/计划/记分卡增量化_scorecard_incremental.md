# forward_scorecard 增量化(方案 B:双 mtime 校验 + 只重算失效行)

> 分支 `feat/scorecard-incremental`。只改 `tools/backtest/forward_scorecard.py` + 新增测试。
> store / market / conditional_predict / repo **零改动**。

## 痛点(根因)
`build_scorecard()` 每天全量双层循环 `for d in list_dates(): for rec in iter_records(d):`,
每 (日期,股票) 行都从零重算再整表覆盖写。主瓶颈是逐行 `_tilt_labels`
(`ta.compute`+`conditional_scenarios`+`direction_view`,单行 ~35ms × 2338 行 ≈ 82s);
次要是逐行重建 `kdates` 列表 + 线性 `.index` 查找;还有逐行 `label_persist`。

## 字段可变性(决定能否复用缓存行)
- **写定即冻结**(依赖不可变 record + t 时点及之前的 kline):
  `name,trend,senti,pred_dir,persist*,signal,p_cond_N,dir_cond_N,p_adj_N,dir_adj_N`
- **pending→final 只翻一次**:`r_N,hit_N,hit_cond_N,hit_adj_N`;
  仅当该行 `r_N` 仍 NaN(pending)时刷新;`hit_cond_N/hit_adj_N` 只需
  `sign(r_N)` × 缓存里已有的 `dir_cond_N/dir_adj_N`,**无需重跑 tilt**。
- 一旦某行所有 `r_N` 非空 → 整行永久冻结。

## 方案 B(mtime 校验 + 增量三分支)
1. `run()`:build 前若 `--out` CSV 已存在,`pd.read_csv(out, dtype={"code": str})` 载入 `prev`,
   记 `csv_mtime = os.path.getmtime(out)`,传给 `build_scorecard`。首次无 CSV / `--rebuild` → 全量。
2. `build_scorecard(..., prev=None, csv_mtime=None)`:`{(date,code): row}` 索引 prev。逐 record 判失效:
   - **失效条件(任一为真即重算整行,走现有全算路径)**:
     ① prev 无此行;② record json 的 mtime > csv_mtime(record 被回补/修正);
     ③ **值校验(替代原"K线 parquet mtime",见下"修订")**;
     ④ CSV 列集与当前 schema/`--horizon` 不匹配(缺列)→ 整表回退全量。

### 修订(2025-… 复工):规则③从 mtime 改为**值校验**
**因由**:用 K线 parquet mtime 判失效太钝——每日给主档 append 当日新 bar 也 bump mtime,
但**未改历史前复权价**(老行仍有效);而除权/回补重写才真改历史价。mtime 分不清二者 →
当日有交易的 code 全部历史行都被误判失效走全算,日常提速几乎不兑现。
**改法**(`_kline_price_stable`,仍只动 forward_scorecard.py):对 prev 命中且 record 未失效的复用候选,
从当前 kline(`_kline` 已缓存)取价**重算某个已到期 r_N**(只价格查表,**绝不调 `_tilt_labels`**),
与 prev 存的同一 r_N 用相对容差 `abs(a-b) <= 1e-6*max(1,|b|)` 比对:
- 一致 → 前复权价链未变 → 冻结/刷新复用,跳过昂贵 tilt;
- 不一致 / 现在取不到该窗口 → 除权/回补改写 → 该行全算;
- 全 pending(无已到期 r_N 可校验,prev 未存锚定价)→ 保守全算(该类行仅当日最新一批,量极小)。
**注意(遗留)**:r_N 是两日收盘价之比,对**均匀等比的前复权再缩放不变**(close[idx]、close[idx+N]
同乘一个因子,比值不变)→ 值校验对"纯等比 backfill"不敏感。技术/倾斜列多为收益率/比值口径
(同样对等比缩放不变),故此情形复用通常仍正确;若 tilt 里存在绝对价位阈值特征,等比再缩放会被漏检。
非等比的历史价纠错(真正会改结论的)则必被 r_N 比对捕获。返回值收成 **python bool**(防 `np.bool_ is True` 恒假)。
   - **未失效且全 r_N 非空** → 直接复用 prev 行(跳过 kline / tilt / persist)。
   - **未失效但仍 pending** → 只做到期刷新:读 kline(`_kline` 缓存)取 idx 算 `r_N`,
     用**缓存里的** `pred_dir/dir_cond_N/dir_adj_N` 补 `hit_N/hit_cond_N/hit_adj_N`,**不调 `_tilt_labels`**。
3. 次要热点:`_kline` 扩成同时缓存 `{date_str: idx}` 映射,替换逐行列表重建 + 线性 `.index`。
4. 输出仍全量覆盖同一 `--out`(幂等不变),重活降为 O(新增 + pending + 失效)。
5. 新增 `--rebuild` flag:强制忽略 prev 全量重算(排障/纠偏兜底)。

## mtime 取路径(store 零改动,复用其私有 helper)
- record: `store._record_path(code, date)`;kline: `store._master_path(code)`。
- 取不到路径 / 文件不存在(如 DB 后端、主档缺失回退 raw)→ **保守判定失效**(宁重算不漏)。
- 无未来函数:pending 刷新沿用与全算完全相同的 `close[idx+N]/close[idx]` 收益口径。

## 测试清单(tests/test_forward_scorecard_incremental.py,合成数据 + 临时目录,不碰生产)
- `test_incremental_equals_full`:全量从零 vs 喂 prev 增量 → 逐值 + NaN 模式一致(核心回归锁)。
- `test_pending_matures`:prev 某 r_5 pending,kline 走出 t+5 → 增量补 r_5/hit_5 且与全量一致。
- `test_frozen_skips_compute`:monkeypatch `_tilt_labels` 抛异常,prev 全到期 → 增量仍绿(证明冻结行未触碰重算)。
- `test_stale_record_recompute`:record json mtime 改到 csv_mtime 之后 → 该行重算而非复用。
- `test_stale_kline_recompute`:某 code parquet mtime 新于 csv_mtime → 该 code 行重算。
- `test_rebuild_flag`:`--rebuild` / `rebuild=True` 强制全量。
