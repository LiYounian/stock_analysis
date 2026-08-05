"""UGC 舆情采集:雪球讨论 / 东财股吧(市场评论、炒股大V/博主)。

情绪三层中最难的「舆情」层:强反爬、需登录态、信噪比低。
方式:chrome-devtools MCP 抓取(复用已有浏览器登录态),不自建爬虫硬刚。
只对目标票池抓,先做量化热度(讨论量/情绪比),再做情感打分(见 analysis/sentiment)。
落盘:data/raw/ugc/{code}.json
"""


def fetch_ugc(codes: list[str], limit: int = 50) -> dict[str, list[dict]]:
    """抓取每票近期讨论帖并落盘。

    输出:{code: [{time, author, is_v, text, likes, replies}, ...]}。
    is_v 标记是否大V/加V用户,供加权。
    注意:抓取动作由上层(Claude 主控 chrome MCP)执行,本函数负责解析+落盘。
    """
    raise NotImplementedError("P4 阶段实现")


def compute_heat(code: str) -> dict:
    """基于已抓 UGC 算量化热度指标(不依赖情感打分)。

    输出:{post_count, v_ratio, reply_total, heat_score}。
    """
    raise NotImplementedError("P4 阶段实现")
