# 展示端 B 期:ingest 收录接口 + 钢印 + 本地签名上传(开发简报)

> 状态:待开发 · 日期:2026-08-07 · 权威需求:[展示端与数据同步.md](展示端与数据同步.md)（第五节安全设计、4.3 契约、八节决策为准）
> 一句话:把"本地端产物送到展示端"从 **A 期的 git 携带 + 导入器**,升级为 **本地端签名上传 → 展示端验签后直接落库**,数据不再随 git 走。

本简报是给**新 worktree / 新分支**的执行交接。读完 + 读权威需求即可开工,**绝不反问**。

---

## 一、现状(B 期在此之上开发,别重造)

已经落地、**不要改**的地基:

- **store DB 后端**(合作者)：`tools/store/backend_db.py`（SQLAlchemy，SQLite 起步，`STORE_BACKEND`/`DB_URL` 切换），表 `record(date,code,json)` / `view(date,name,json)` / `code_view(date,name,code,json)`，主键即 (日期×键)，`_upsert` = 删+插(**天然幂等**)。分析侧读写经 `tools/store/repo.py` 门面分发(raw 恒文件)。
- **文件产物 → DB 导入器**(A 期，本简报作者写的)：`tools/sync/import_to_db.py`。读 `data/analysis/<日期>/` 的 record / view / code_view 三类文件、调 store 公开 API 幂等落库。**B 期的上传打包应复用它枚举产物的逻辑**(见下 P3)。
- **展示端只读 web**：`STORE_BACKEND=db` 从库读、端口 **8801**、部署见 [部署说明.md](../参考/部署说明.md) 的「远端部署」节。展示端**只读、不算、不触网**。
- **注意**：早期有过一版 `tools/store/backends/` 后端抽象 P1，**已作废废弃**,以 `backend_db.py` 为准,不要去找/复活它。

B 期要补的是"**网络传输 + 安全**"这段:本地签名上传 → 展示端 ingest 验签落库,替代 git 携带数据。

---

## 二、数据流(B 期终态)

```
本地端(算完) 
  └─ tools/sync/upload.py:枚举某日产物 → 组信封 → HMAC 钢印(带 key_id/ts/nonce)→ HTTPS POST
                                         │
                                         ▼  公网(TLS)
展示端 tools/sync/ingest.py(独立 FastAPI 服务,自己的端口)
  token 鉴权 → HMAC 验签 → 时间戳+nonce 防重放 → 时效校验 → 幂等 upsert(走 store DB 后端)→ ingest 审计
                                         │
                                         ▼
                                   展示端 DB ── web(8801) 只读展示
```

A 期用 git 搬数据是过渡;B 期上线后,**数据不再进 git、也不再需要导入器**。

---

## 三、范围与验收(P2 / P3 / P4)

### P2 展示端 ingest 服务（`tools/sync/ingest.py` + `sign.py` + `audit.py`）

独立 FastAPI 应用(**不要动 `web/app.py`**),`POST /ingest` 收"某日产物包"。按顺序过五关,任一关失败都要落审计并返回明确错误码:

- **P2-1 token 鉴权**:`Authorization: Bearer <token>` 比对环境变量令牌(`hmac.compare_digest`)。缺/错 → **401**。（验收:无 token / 错 token 401;对 200。）
- **P2-2 HMAC 验签(钢印)**:见第四节。载荷任一字节被改 → 签名不符 → **403**。（验收:篡改一字节即拒。）
- **P2-3 防重放**:`meta.ts` 时间戳 + `meta.nonce`。`|now-ts| > 窗口`(默认 300s) → **409**;`nonce` 已见过 → **409**。（验收:重放同一请求被拒。）
- **P2-4 时效校验**:`meta.date` 晚于服务器当天(未来数据)→ **422**;早于保留窗口下界(默认 90 天)→ **422**;同 `date` 已有更**新** `generated_at` 时旧包覆盖请求 → **409**。（验收:未来/过老/旧盖新均拒,窗口内且更新的放行。）
- **P2-5 幂等落库 + 审计**:验签通过 → 逐条 `record` 过 `tools/contracts` 的 `validate_record`(契约真源,§4.3)→ 经 store 公开 API(`STORE_BACKEND=db`,复用 `import_to_db` 的落库方式)幂等 upsert record/view/code_view;**每次 ingest(成功或失败)落一条 `ingest_audit`**。（验收:同包上传两次 DB 结果一致无重复;一次成功+一次失败各有一条审计。）

审计/防重放的表(**B 期自己在 `tools/sync/audit.py` 建,不要动 `tools/store/`**,同一个 `DB_URL`):
```
ingest_audit(id, at, source, key_id, date, rows, verify_ok, result, msg)
seen_nonce(nonce PK, at)                     -- 定期清理过期 nonce
snapshot(date PK, generated_at, source, ingested_at)   -- 供 P2-4 旧盖新判断
```

### P3 本地端上传工具（`tools/sync/upload.py`）

- **P3-1 打包**:`python -m tools.sync.upload --date YYYY-MM-DD` 枚举该日 record + view + code_view(**复用 `import_to_db` 的产物枚举**,别另写一套),组成第四节的信封。（验收:生成含该日全部展示数据的载荷。）
- **P3-2 签名上传**:对规范化载荷字节 + ts + nonce 做 HMAC(信封带 `key_id`),HTTPS POST 到展示端。（验收:展示端验签通过入库;重复 nonce 被拒。）
- **P3-3 失败重试/续传**:网络失败**指数退避**重试;按票分片,部分失败只重发失败项。（验收:人为让第 3 票失败,重跑只补第 3 票。）
- **P3-4 回执**:本地落一份可读回执(成功/失败票数、服务端返回)。（验收:跑一次有回执。）

### P4 ingest 部署/运维（补 [部署说明.md](../参考/部署说明.md)）

- ingest 服务常驻(systemd),**独立端口**(建议 `SYNC_INGEST_PORT` 默认 8802,与展示 8801 分开);公网必须 **TLS**(nginx/caddy 反代 + 证书,或本简报暂用第五节说明的最简方案)。
- 密钥全走环境变量注入;`ingest_audit` 留存;`seen_nonce` 定期清理;数据保留天数清理策略。
- 写清"本地端配哪些环境变量、展示端配哪些"。

---

## 四、钢印 / 安全设计(核心,权威见 §5)

**信封格式**(HTTPS POST body,JSON):
```json
{
  "meta": {"date":"YYYY-MM-DD","generated_at":"<iso>","source":"<本地端标识>",
            "ts":"<iso或epoch>","nonce":"<uuid>","key_id":"<密钥标识>",
            "sig_alg":"HMAC-SHA256","sig":"<hex>"},
  "records":    {"<code>": <record dict>, ...},
  "views":      {"panel": {...}, "screen": {...}, ...},
  "code_views": {"chart": {"<code>": {...}}, "news_ai": {"<code>": [...]}, ...}
}
```

**签名**(`tools/sync/sign.py`,本地端签、展示端验共用):
- 规范化字节 = 除 `meta.sig` 外整个信封的**确定性 JSON**(`sort_keys`、固定分隔符、UTF-8 编码)。
- `sig = HMAC_SHA256(key, 规范化字节)`。验签端用同 key 重算比对(`compare_digest`)——通过即证明"来自持密钥方 **且** 一字节未改"。
- **对称 HMAC 起步**;`sig_alg` 字段留位,将来可切非对称(Ed25519,展示端只存公钥)。
- **双密钥轮换窗口**:展示端可同时持"当前 key + 旧 key",按信封 `key_id` 选 key 验;两把都试通过一段时间,平滑轮换(§8 Q7)。

**防重放**:`ts` 超窗口(默认 300s)拒;`nonce` 落 `seen_nonce`、重复拒。
**时效**(与钢印是两件事:钢印管真不真,时效管新不新):按 `meta.date`/`generated_at` 判未来/过老/旧盖新。
**密钥管理**:签名密钥、鉴权令牌**全走环境变量、禁硬编、禁入库**(沿用 `LLM_*` 既有约定);缺环境变量时服务启动即报错。

---

## 五、`settings.py` 新增配置块(只加环境变量名,绝不写密钥值)

在 `tools/config/settings.py` **文末**加一段清晰注释块,全部 `os.getenv` 读:
```
SYNC_INGEST_URL        # 本地端:展示端 ingest 地址(https://.../ingest)
SYNC_INGEST_TOKEN      # 两端:Bearer 鉴权令牌
SYNC_SIGNING_KEY       # 两端:当前 HMAC 共享密钥
SYNC_SIGNING_KEY_OLD   # 展示端:轮换窗口内的旧密钥(可空)
SYNC_KEY_ID            # 本地端:当前密钥标识(默认如 "k1")
SYNC_REPLAY_WINDOW_S   # 展示端:防重放时间窗口秒(默认 300)
SYNC_MAX_AGE_DAYS      # 展示端:时效保留窗口天(默认 90)
SYNC_SOURCE_ID         # 本地端:来源标识
SYNC_INGEST_PORT       # 展示端:ingest 服务端口(默认 8802)
```

---

## 六、严格文件边界(只准动这些)

- **新增** `tools/sync/`：`ingest.py`(服务)、`sign.py`(签/验共用)、`audit.py`(审计+防重放+snapshot 表)、`upload.py`(本地上传);可复用现有 `tools/sync/import_to_db.py` 的产物枚举/落库。
- **改** `tools/config/settings.py`:仅文末加第五节配置块。
- **改** `docs/参考/部署说明.md` + 本简报同目录相关文档(P4)。
- **绝不动**:`tools/store/`(合作者地基,只调其公开 API 落库)、`tools/scheduler.py`、`tools/analysis/`、`tools/collectors/`、`tools/screener/`、`tools/backtest/`。**尽量不改** `web/`(ingest 是独立服务,不要塞进 `web/app.py`)。

---

## 七、Git 铁律

- 从**最新 main** 建分支(建议 `feat/display-ingest`),在**自己的 worktree** 里做;分步 commit;可 `git push -u origin` 备份。
- **绝不**在 main 上直接开发、绝不动别的分支/worktree;**绝不**把 `data/` 移出 git;运行库 `*.db`/`*.sqlite` 不入库。
- commit message 中文清晰;**绝不加任何 AI 署名 / Co-authored-by / Generated 行**。
- 每期做完 **合并回 main(--no-ff)** 由统筹窗口决定;子分支只管开发 + 报告。

---

## 八、测试 + 交付

- 单测(`tests/`,`pytest -q` 全绿不回归)必须覆盖:**无 token→401 / 签名篡改→403 / 过期 ts→拒 / 重放 nonce→拒 / 未来日期→拒 / 过老日期→拒 / 旧盖新→拒 / 幂等 upsert 无重复 / 每次 ingest 必有审计 / sign 往返 + 密钥轮换验通过**。用 FastAPI TestClient(`httpx` 已装)驱动 ingest;DB 指临时 SQLite 隔离(`backend_db.reset_engine()`)。
- 断言要锁住"为什么改"的语义(防未来重写时无意删掉安全规则)。
- **脱敏**:代码/文档/测试不得出现本机绝对路径、真实用户名、公司/网关名、任何密钥值。
- **完成一期就报告**:分支 / 改动文件 / 测试结果 / 关键设计决策 / 是否碰了 store 或 web(应没有)。

---

## 九、关键提醒(易踩坑)

1. **别复活作废的 P1 后端抽象**;落库一律走 `backend_db` / `repo` 现有公开 API。
2. **ingest 是独立服务、独立端口**(8802),不要动展示 web(8801)。
3. **幂等已由 backend_db `_upsert` 保证**;你要额外做的是"旧 generated_at 不许盖新"(靠 `snapshot` 表)。
4. **审计要覆盖失败路径**——鉴权/验签/时效失败也要落一条 audit,不能只记成功。
5. **契约校验**:record 落库前过 `validate_record`,防污染数据进库(投资相关,数据可信是硬要求)。
