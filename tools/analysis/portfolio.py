"""组合层聚合(方案2 第一层产出)。

把 32 只票的技术面 + 情绪面结果按行业/板块聚合,识别主线、板块情绪温度,
并筛出「重点票」交给个股深挖层。
"""


def aggregate(stock_results: dict[str, dict]) -> dict:
    """组合层聚合。

    输入:{code: {technical, sentiment, events, ...}} 全池单票结果。
    输出:{
        industry_stats: 各板块 涨跌/情绪 均值分布,
        hot_theme:      当前最强主线(如 存储/算力),
        sentiment_temp: 组合情绪温度(0~100),
        watchlist:      筛出的重点票列表(见 pick_focus),
    }
    """
    raise NotImplementedError("P3 阶段实现")


def pick_focus(stock_results: dict[str, dict], rule: str = "hybrid") -> list[str]:
    """筛重点票。

    rule 待拍板:momentum(涨幅异动)/ heat(舆情热度)/ event(重大事件)/ hybrid(综合)。
    输出:重点票代码列表。
    """
    raise NotImplementedError("P3 阶段实现")
