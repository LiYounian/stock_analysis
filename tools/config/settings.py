"""全局参数配置。

集中管理路径、采集参数、开关。分析层/采集层统一从这里取,不散落硬编码。
"""
import os
from pathlib import Path

# —— 路径 ——
PROJECT_ROOT = Path(__file__).resolve().parents[2]   # 股票分析/
DATA_RAW = PROJECT_ROOT / "data" / "raw"              # 采集缓存根目录
REPORT_DIR = PROJECT_ROOT / "docs" / "报告"           # 报告产出目录

# —— 行情采集参数 ——
KLINE_PERIOD = "daily"      # daily / weekly
KLINE_ADJUST = "qfq"        # 前复权
KLINE_DAYS = 250            # 默认拉取天数(约一年交易日)

# —— 舆情/新闻采集参数 ——
NEWS_LOOKBACK_DAYS = 7      # 新闻/公告回看窗口
UGC_LIMIT = 50             # 每票 UGC 抓取条数上限

# —— 情感打分(qwen 外包)——
USE_QWEN_SENTIMENT = True   # 情感打分是否走 qwen 外包
QWEN_BATCH_SIZE = 20        # 每批送 qwen 的文本条数

# —— LLM API(全部走环境变量,禁止硬编 url/key,禁止入库)——
# 优先 LLM_* 显式变量;否则回落到用户已有的 内部网关 DeepSeek 环境变量。
# 实际 url/key 值只在用户 shell 环境里(~/.zshrc),代码只引用变量名。
LLM_BASE_URL = os.getenv("LLM_BASE_URL") or os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = (os.getenv("LLM_API_KEY") or os.getenv("LLM_API_KEY")
               or os.getenv("DEEPSEEK_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")   # 用户推荐模型
LLM_TIMEOUT = 60
LLM_MAX_RETRY = 3
# 触点 → provider 路由:重批量走 qwen,精确抽取/摘要走用户 API
LLM_ROUTE = {"extract": "openai_compat", "sentiment": "qwen", "summary": "openai_compat"}
LLM_CACHE = DATA_RAW / "llm_cache"             # 抽取结果缓存,改下游免重复调用

# —— 采集限频(防封)——
FETCH_SLEEP_SEC = 0.5       # 单次请求间隔


def ensure_dirs() -> None:
    """确保缓存/报告目录存在。"""
    for d in (DATA_RAW, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
