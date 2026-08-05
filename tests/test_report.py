"""report/builder.py 单测(喂假 compute 结果,验证产出与排序)。"""
from tools.config import settings
from tools.report import builder


def _fake_result(score, close=10.0):
    return {
        "n": 70,
        "last": {"close": close, "pct_chg": 1.0},
        "ma": {"ma5": 10, "ma10": 9, "ma20": 8, "ma60": 7, "排列": "多头排列"},
        "macd": {"dif": 0.1, "dea": 0.05, "macd": 0.1, "状态": "金叉"},
        "kdj": {"k": 15, "d": 20, "j": 5, "状态": "超卖"},
        "rsi": {"rsi6": 40, "rsi12": 45, "rsi24": 50},
        "vol": {"量比": 1.8, "状态": "放量"},
        "signal": {"评级": "偏多" if score >= 30 else "中性", "得分": score, "依据": ["测试+" + str(score)]},
    }


def test_portfolio_report_created_and_sorted(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPORT_DIR", tmp_path)
    # 用真实票池里的代码,保证能查到名称/板块
    results = {"002156": _fake_result(60), "000021": _fake_result(-40),
               "688249": _fake_result(10), "999999": {"error": "数据不足", "n": 0}}
    path = builder.build_portfolio_tech_report(results)
    content = open(path, encoding="utf-8").read()
    # 排行第一名应是得分最高的 002156,排在 000021 之前
    assert content.index("002156") < content.index("000021")
    assert "数据不足 1" in content            # 无效票计数
    assert "MACD 金叉" in content              # 异动清单章节


def _fake_fund():
    return {"报告期": "20260331", "营收": 3.7e9, "净利": 2.4e8, "营收增速": 12.5,
            "净利增速": 30.1, "ROE": 8.8, "毛利率": 15.2, "净利率": 6.5,
            "负债率": 41.68, "PE_TTM": 47.98, "PB": 4.31, "总市值": 575.27}


def test_portfolio_report_with_fundamentals(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPORT_DIR", tmp_path)
    results = {"002156": _fake_result(60), "000021": _fake_result(-40)}
    funds = {"002156": _fake_fund(), "000021": _fake_fund()}
    content = open(builder.build_portfolio_tech_report(results, funds), encoding="utf-8").read()
    assert "板块基本面对比" in content
    assert "PE(TTM)" in content
    assert "五、情绪面" in content              # 有基本面时情绪面顺延为第五节


def test_stock_card_created(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPORT_DIR", tmp_path)
    path = builder.build_stock_tech_report("002156", _fake_result(60), _fake_fund())
    content = open(path, encoding="utf-8").read()
    assert "综合评级:偏多" in content
    assert "多头排列" in content
    assert "PE(TTM) 47.98" in content          # 基本面章节
    assert "P2 补" in content                  # 情绪面占位


def test_stock_card_insufficient(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REPORT_DIR", tmp_path)
    path = builder.build_stock_tech_report("002156", {"error": "数据不足", "n": 0})
    content = open(path, encoding="utf-8").read()
    assert "数据不足" in content
