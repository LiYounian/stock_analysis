"""全局参数配置。

集中管理路径、采集参数、开关。分析层/采集层统一从这里取,不散落硬编码。
"""
import os
from pathlib import Path

# —— 路径 ——
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 股票分析/
DATA_RAW = PROJECT_ROOT / "data" / "raw"              # 采集缓存根目录
DATA_MASTER = PROJECT_ROOT / "data" / "master"        # 滚动主档(每股一份长历史,非按日期分区)
REPORT_DIR = PROJECT_ROOT / "data" / "reports"        # 报告/产物产出目录(不入库)

# —— 行情采集参数 ——
KLINE_PERIOD = "daily"      # daily / weekly
KLINE_ADJUST = "qfq"        # 前复权
KLINE_DAYS = 250            # 默认拉取天数(约一年交易日)
# 日筛/分析只加载近史尾部(省内存):主档现含多年(为回测),但日筛最长回看仅 ~251 根
# (MA200+52周高)。取 500(约2年)= 冗余 ~2x,不降级任何日筛信号。回测仍走 load_kline 全历史。
DAILY_KLINE_ROWS = 500

# —— Tushare 可选数据源(全 A 盘后批量日线 + 筹码;免费源之上的可选增强)——
# token 仅从环境变量读,不入库、不打印。未配 → TUSHARE_ENABLED=False,链路与现状完全一致。
# 配了且实际读得通 → 全 A 日增量优先走 Tushare(daily + daily_basic 换手),取不到静默回退免费源;
# 「最强」策略的筹码获利比例(cyq_perf)为 Tushare 独有,免费源拿不到。
TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "")
TUSHARE_ENABLED = bool(TUSHARE_TOKEN)

# —— 舆情/新闻采集参数 ——
NEWS_LOOKBACK_DAYS = 7      # 新闻/公告回看窗口
UGC_LIMIT = 50             # 每票 UGC 抓取条数上限

# —— 新闻扩召回 + LLM 相关性初筛(collectors.news_recall；仅调用方 fetch_news(recall=True) 时生效)——
# 默认关:只在 screenall/pool 的 collect_message 那批(选出并集∪自选，~125 只)由调用方开，
# 补「不挂到个股、但对该股重要的行业/宏观/管制类」消息，再用 LLM 关思考做宁严相关性初筛。
NEWS_RECALL_ENABLED = os.getenv("NEWS_RECALL_ENABLED", "false").lower() in ("1", "true", "yes")
NEWS_RECALL_KEYWORD_CAP = int(os.getenv("NEWS_RECALL_KEYWORD_CAP", "6"))       # 每票扩召回主题关键词上限(每词一次网络请求)
NEWS_RECALL_CANDIDATE_CAP = int(os.getenv("NEWS_RECALL_CANDIDATE_CAP", "30"))  # 每票扩召回候选条数上限(控 LLM 初筛量)

# —— 情感打分(qwen 外包)——
USE_QWEN_SENTIMENT = True   # 情感打分是否走 qwen 外包
QWEN_BATCH_SIZE = 20        # 每批送 qwen 的文本条数

# —— LLM API(url/key 走环境变量,禁止硬编 url/key,禁止入库;model 写死)——
# 只认 LLM_BASE_URL + LLM_API_KEY 两个变量;实际值只在本机 shell 环境(~/.zshrc),代码只引用变量名。
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = "deepseek-v4-pro"   # 写死(env 不再设 LLM_MODEL;要换模型改这里)
LLM_TIMEOUT = 60
LLM_MAX_RETRY = 3
# —— LLM 抽取提速(I/O 型,有界并发 + 送 LLM 条数上限)——
LLM_EXTRACT_WORKERS = int(os.getenv("LLM_EXTRACT_WORKERS", "8"))   # 逐条抽取并发度(ThreadPool)
NEWS_EXTRACT_MAX = int(os.getenv("NEWS_EXTRACT_MAX", "40"))        # 单票送 LLM 抽取的最近条数上限(原文仍全量落盘)
# 关思考模式开关:实测对当前 deepseek-v4-pro 中性(网关本就不花时间思考),
# 为将来换带思考模型自动生效预留;走 extra_body={"enable_thinking": False}。
LLM_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "true").lower() in ("1", "true", "yes")
# 触点 → provider 路由:重批量走 qwen,精确抽取/摘要走用户 API
LLM_ROUTE = {"extract": "openai_compat", "sentiment": "qwen", "summary": "openai_compat"}
LLM_CACHE = DATA_RAW / "llm_cache"             # 抽取结果缓存,改下游免重复调用

# —— 采集限频(防封)——
FETCH_SLEEP_SEC = 0.5       # 单次请求间隔(串行兜底路径)
# 方案B 兜底并发(仅主档缺失时逐只 akshare 回退用):有界线程池 + jitter。
# 默认 1 = 保持串行(不激进并发,避免打封);需要提速时调 8–12。
FETCH_WORKERS = int(os.getenv("FETCH_WORKERS", "1"))
FETCH_JITTER_SEC = 0.2      # 并发路径每请求前随机抖动上限(秒)

# —— 数据新鲜度(store 层用)——
RAW_STALE_DAYS = 3          # raw 缓存超过此天数(或无采集元数据)视为陈旧,促使重采

# —— 低频数据源新鲜度门控(避免每日全量逐票拉一致预期/股东户数,省网络+防限流)——
# 阈值以「日历天」为交易日的稳健代理(is_stale 用采集时刻的日历天差)。缓存 ≤ 阈值天视为
# 新鲜 → 当日跳过重拉;无缓存/无元数据一律视为陈旧 → 首采不会漏。可用环境变量覆盖。
CONSENSUS_STALE_DAYS = float(os.getenv("CONSENSUS_STALE_DAYS", "5"))   # 一致预期(周级变,~3-5交易日)
HOLDER_STALE_DAYS = float(os.getenv("HOLDER_STALE_DAYS", "28"))        # 股东户数(季度级,~20交易日≈28日历天)

# —— 情绪数据新鲜度(event 情绪引擎用,date-pin + 新鲜度标注)——
# 回退策略:A2=可识别回退(默认,窗口内回退旧 raw 但标「陈旧」,超窗标「无数据」);
#          A1=严格锁定(锁定日无 raw 即「无数据」,绝不回退旧数据)。
SENTIMENT_FRESHNESS_MODE = os.getenv("SENTIMENT_FRESHNESS_MODE", "A2")
# A2 允许回退的窗口(以 raw 分区/采集周期为「交易日」代理计数);超此距离标「无数据」。
SENTIMENT_MAX_STALE_DAYS = int(os.getenv("SENTIMENT_MAX_STALE_DAYS", "3"))
# —— 消息持续性研判(结构性 vs 短暂 + 印证强度)——
# 只对根源消息(公司行为层新闻)逐条 LLM 研判并附加到情绪 event;附加、可选,不动净情绪口径。
# 回测/批量若不需要该分量可置 false 省 LLM 调用(下游读不到字段=优雅退化)。
SENTIMENT_PERSISTENCE_ON = os.getenv("SENTIMENT_PERSISTENCE_ON", "true").lower() in ("1", "true", "yes")

# —— 存储后端(store 层分析侧读写走哪个后端)——
# file(默认):现文件后端,产物落 data/analysis/<日期>/。
# db:SQLAlchemy 后端,记录/视图入库(SQLite 起步,可换 Postgres/MySQL,由 DB_URL 决定)。
# raw 采集缓存(kline/fundamental…)恒走文件,不入库。
STORE_BACKEND = os.getenv("STORE_BACKEND", "file")
# 数据库连接串(SQLAlchemy URL)。默认单文件 SQLite,零运维;
# 换库只改此环境变量,如 mysql+pymysql://user:pwd@host:3306/db、postgresql+psycopg://...
DB_URL = os.getenv("DB_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'app.db'}")

# —— 定时调度(进程内 APScheduler,tools.scheduler 用)——
# 开关 + 各任务刷新间隔(分钟)。间隔 <=0 表示禁用该任务。全部可用环境变量覆盖。
SCHED_ENABLED = os.getenv("SCHED_ENABLED", "false").lower() in ("1", "true", "yes")
SCHED_FULL_INTERVAL_MIN = int(os.getenv("SCHED_FULL_INTERVAL_MIN", "1440"))   # T1 盘后全量:默认每日
SCHED_BACKFILL_INTERVAL_MIN = int(os.getenv("SCHED_BACKFILL_INTERVAL_MIN", "0"))  # T5 兜底补数:默认关
SCHED_FULL_ALL = os.getenv("SCHED_FULL_ALL", "true").lower() in ("1", "true", "yes")  # 全池 vs 开发子集
SCHED_MISFIRE_GRACE_SEC = int(os.getenv("SCHED_MISFIRE_GRACE_SEC", "3600"))   # 错过触发的宽限(补跑)
# SEPA+VCP 监控:工作日 11:35 午间 / 15:35 收盘。默认关,本地部署设 SCHED_SEPA_ENABLED=true。
SCHED_SEPA_ENABLED = os.getenv("SCHED_SEPA_ENABLED", "false").lower() in ("1", "true", "yes")

# —— 展示端数据同步(B 期:本地签名上传 → 展示端 ingest 落库)——
# 全部走环境变量,禁硬编 URL/域名/密钥,禁入库。展示端与本地端各配所需项。
# 钢印用对称 HMAC-SHA256 起步,sig_alg 字段留位;展示端支持"当前+旧"双密钥轮换窗口(按 key_id 选,验不过再逐一试)。
SYNC_INGEST_URL = os.getenv("SYNC_INGEST_URL", "")               # 本地端:展示端 ingest 地址,如 https://<host>:8802/ingest
SYNC_INGEST_PORT = int(os.getenv("SYNC_INGEST_PORT", "8802"))    # 展示端:ingest 服务端口(与展示 web 8801 分开)
SYNC_INGEST_TOKEN = os.getenv("SYNC_INGEST_TOKEN", "")           # 两端:Bearer 鉴权令牌
SYNC_SIGNING_KEY = os.getenv("SYNC_SIGNING_KEY", "")             # 两端:当前 HMAC 共享密钥
SYNC_KEY_ID = os.getenv("SYNC_KEY_ID", "k1")                     # 本地端:当前密钥标识(随信封上送)
SYNC_SIGNING_KEY_OLD = os.getenv("SYNC_SIGNING_KEY_OLD", "")     # 展示端:轮换窗口内的旧密钥(可空)
SYNC_KEY_ID_OLD = os.getenv("SYNC_KEY_ID_OLD", "k0")            # 展示端:旧密钥标识
SYNC_REPLAY_WINDOW_S = int(os.getenv("SYNC_REPLAY_WINDOW_S", "300"))   # 展示端:防重放时间窗口(秒)
SYNC_MAX_AGE_DAYS = int(os.getenv("SYNC_MAX_AGE_DAYS", "90"))    # 展示端:时效保留窗口(天),更早的产物拒收
SYNC_SOURCE_ID = os.getenv("SYNC_SOURCE_ID", "local")           # 本地端:来源标识(随信封上送)
# —— ingest 硬化(展示端 ingest 服务用)——
SYNC_RATE_MAX = int(os.getenv("SYNC_RATE_MAX", "120"))          # 速率限制:窗口内最大请求数(按 token 计)
SYNC_RATE_WINDOW_S = int(os.getenv("SYNC_RATE_WINDOW_S", "60")) # 速率限制:滑动窗口(秒);超限 429
SYNC_MAX_BODY_BYTES = int(os.getenv("SYNC_MAX_BODY_BYTES", str(32 * 1024 * 1024)))  # 请求体上限(默认 32MiB);超限 413
SYNC_NONCE_KEEP_S = int(os.getenv("SYNC_NONCE_KEEP_S", "86400"))  # nonce 保留秒数(定时清理早于此的,>防重放窗口即安全)

# —— 票池写模式(方案2:本地=直采重建,远端=入队提案)——
# 本地默认 direct = 现状零行为变化;远端 stock-web unit 设 POOL_WRITE_MODE=enqueue → 网页加/删
# 只写 pool_pending 提案表(不碰会被 reset --hard 抹掉的 config/stock_pool.json),本地闭环消化。
POOL_WRITE_MODE = os.getenv("POOL_WRITE_MODE", "direct")           # direct / enqueue
POOL_PENDING_SOURCE = os.getenv("POOL_PENDING_SOURCE", "remote")   # 入队提案的 source 标识


def ensure_dirs() -> None:
    """确保缓存/报告目录存在。"""
    for d in (DATA_RAW, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
