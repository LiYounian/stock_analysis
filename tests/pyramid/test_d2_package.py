"""A6 决策包禁裸 dict + ★重点口径(focus_score·排除过热A/拐点A)、A12 过程文件落盘 的锁。

D4：render_package 文本内无 `{'`（裸 dict/json）。
D5：save_package 落 data/analysis/<as_of>/金字塔决策包_*.md，断言文件存在。
数据无关：market_overview 用 monkeypatch 假 _load；render/save 用合成 pkg。
"""
import os

import pytest

from tools.pyramid import d2_compose as C
from tools.pyramid import d2_package as P


# ── A6 ★重点：focus_score 降序，排除 过热A/拐点A ──────
def _fake_load(focus, regime):
    def _l(root, as_of, fname):
        return focus if "focus" in fname else regime
    return _l


def test_市场定调_重点按focus排除过热A拐点A(monkeypatch):
    focus = {
        "风险偏好": {"风险偏好": "中性", "广度档": "中性", "净广度": -0.04},  # dict！
        "宏观情景": "中性", "宏观净方向": "中性",
        "规避板块池": [],
        "重点板块池": [
            {"板块": "电子", "focus_score": 0.88},
            {"板块": "电力设备", "focus_score": 0.76},
            {"板块": "公用事业", "focus_score": 0.69},   # 拐点A → 排除
            {"板块": "农林牧渔", "focus_score": 0.49},   # 过热A → 排除
            {"板块": "计算机", "focus_score": 0.73},
        ],
    }
    regime = {"板块": [
        {"板块": "电子", "冷热标签": "正常活跃", "拥挤档": "B", "动量_截面分位": 0.5},
        {"板块": "电力设备", "冷热标签": "过冷", "拥挤档": "B", "动量_截面分位": 0.3},
        {"板块": "公用事业", "冷热标签": "拐点", "拥挤档": "A", "动量_截面分位": 0.9},
        {"板块": "农林牧渔", "冷热标签": "过热", "拥挤档": "A", "动量_截面分位": 0.95},
        {"板块": "计算机", "冷热标签": "正常活跃", "拥挤档": "B", "动量_截面分位": 0.4},
    ]}
    monkeypatch.setattr(P, "_load", _fake_load(focus, regime))
    ov = P.market_overview("2026-09-17", root=None, n_focus=5)
    # ★按 focus_score 降序：电子0.88 > 计算机0.73... 且排除拐点A/过热A
    assert ov["重点板块"] == ["电子", "电力设备", "计算机"]
    assert "公用事业" not in ov["重点板块"]  # 拐点A 排除
    assert "农林牧渔" not in ov["重点板块"]  # 过热A 排除
    assert ov["风险偏好"] == "中性"          # dict 已取内层标量
    # 电子(focus 最高)必被标 ★（旧 bug：动量口径漏标电子）
    assert "电子" == ov["重点板块"][0]


# ── A6 / D4 决策包禁裸 dict + 四面结构锁 ──────────────
def _synth_pkg():
    ov = {
        "as_of": "2026-09-17",
        "风险偏好": {"风险偏好": "中性", "广度档": "中性"},  # 故意塞裸 dict，验证 _txt 兜底
        "广度档": "中性", "净广度": -0.0439, "涨停": 58, "跌停": 6,
        "依据": "广度档=中性(净广度=-0.0439、涨停58/跌停6、权重搭台中小盘偏弱);宏观净方向=中性→最终中性",
        "宏观情景": "中性", "宏观净方向": "中性",
        "规避板块池": [], "重点板块": ["电子", "电力设备"],
        "重点口径": "focus_score↓·排除规避与过热A/拐点A",
        "全板块": [
            {"板块": "电子", "冷热标签": "正常活跃", "拥挤档": "B", "板块均涨幅": 1.2,
             "涨停数": 6, "上涨家数占比": 0.6, "动量_截面分位": 0.5},
        ],
    }
    skel = {"池规模": 575, "参与票数": 575, "计分票数": 556, "排序票数": 540,
            "否决票数": 16, "数据缺失票数": 19, "权重": C.WEIGHTS,  # 权重是 dict
            "排序键": "骨架分↓, 量价自证子分↓, 代码↑", "排序": [], "排雷否决": []}
    # 四面重排后卡片形状：卡头元信息 + 面块(面attr→[工具浓缩块])
    cards = [{"code": "600000", "名称": "浦发银行", "as_of": "2026-09-17", "骨架分": 62.7,
              "子分": {"量价自证": 80.0, "板块角色": 55.0, "策略共识": 50.0,
                      "排雷": 100.0, "宏观催化": 40.0},  # 子分是 dict
              "来源标签": ["K线过闸", "多策略"],
              "面块": {
                  "基本面": ["【financial_redflag·③塔身】as_of=2026-09-17\n财报评级 良"],
                  "技术面": ["【price_volume·①塔基】as_of=2026-09-17\n现价: 10.0【口径:主档K线】"],
                  "资金面": ["【unlock_risk·②消息】as_of=2026-09-17\n解禁嫌疑 低"],
                  "消息情绪面": ["【sector_context·④宏观】as_of=2026-09-17\n净催化 正"],
                  "经验": ["【experience_rules·经验】as_of=2026-09-17\n命中0条"],
              }}]
    return {"as_of": "2026-09-17", "市场定调": ov, "骨架": skel,
            "候选卡片": cards, "top_n": 8}


def test_render_package_无裸dict():
    txt = P.render_package(_synth_pkg())
    assert "{'" not in txt          # D4：无裸 dict/json
    assert '{"' not in txt
    # 关键字段确实文字化进去了
    assert "量价自证 80.0" in txt   # 子分文字化
    assert "量价自证=0.3" in txt    # 权重文字化


def test_render_package_统一词表在开头():
    """v2：统一指标词表拼在决策包 prompt 开头（标题后·市场定调前），共性定义喂一次。"""
    txt = P.render_package(_synth_pkg())
    assert "## 统一指标词表" in txt
    assert txt.index("## 统一指标词表") < txt.index("## 市场定调")  # 位置在市场定调前
    # 词表带关键指标 + 其区间（从档位表现渲）
    assert "市盈率 PE(TTM)" in txt and "低≤15 / 中≤30" in txt
    assert "消息覆盖与可信度" in txt


def test_render_package_四面板齐全带名():
    """四面结构锁：卡头带股票名 + 四面板头齐全 + 经验尾块。"""
    txt = P.render_package(_synth_pkg())
    # 卡头带股票名 + code + as_of
    assert "浦发银行（600000）" in txt
    assert "as_of=2026-09-17" in txt
    assert "【卡头·元信息】" in txt
    # 四面板头齐全（固定顺序·全展开）
    for h in ["【一·基本面】", "【二·技术面】", "【三·资金面】", "【四·消息面】"]:
        assert h in txt, f"缺面板 {h}"
    # 经验纪律尾块
    assert "【经验纪律·跨面】" in txt
    # 各面工具浓缩块原样入面板（口径随值·拼装层不改）
    assert "现价: 10.0【口径:主档K线】" in txt
    assert "解禁嫌疑 低" in txt


def test_render_package_空面板显式标待填充():
    """Wave1 某面无工具时，面板不静默消失，显式标待 Wave2 填充。"""
    pkg = _synth_pkg()
    pkg["候选卡片"][0]["面块"].pop("资金面")  # 模拟资金面暂无工具
    txt = P.render_package(pkg)
    assert "【三·资金面】" in txt
    assert "待 Wave2 填充" in txt


def test_render_package_市场定调描述性():
    """§8 市场定调输入侧描述性：主句 + 上游依据整句 + 净广度→多空描述。"""
    txt = P.render_package(_synth_pkg())
    # 主句为描述句（synth 的风险偏好留 dict 验证 _txt 兜底；真实数据经 market_overview 取标量）
    assert "当前市场风险偏好" in txt
    assert "净广度-0.044→多空均衡" in txt   # 净广度∈[-0.1,0.1]→均衡
    assert "定调依据：" in txt               # 上游依据整句原样 surface
    assert "涨停58/跌停6" in txt
    assert "板块轮动：" in txt


def test_render_package_计分口径自洽出现():
    txt = P.render_package(_synth_pkg())
    # A11：三口径都印出且可核对（参与=计分+数据缺；计分=排序+否决）
    assert "参与575=计分556+数据缺19" in txt
    assert "计分556=排序540+否决16" in txt


# ── A12 / D5 过程文件落盘 ─────────────────────────────
def test_save_package_落盘存在(tmp_path):
    pkg = _synth_pkg()
    path = P.save_package(pkg, root=str(tmp_path), label="top8")
    assert os.path.exists(path)
    expect = tmp_path / "data" / "analysis" / "2026-09-17" / "金字塔决策包_top8.md"
    assert os.path.abspath(path) == os.path.abspath(str(expect))
    with open(path, encoding="utf-8") as f:
        body = f.read()
    assert body.startswith("# 金字塔决策包")
    assert "{'" not in body  # 落盘内容同样无裸 dict


def test_save_package_默认label用topn(tmp_path):
    pkg = _synth_pkg()
    path = P.save_package(pkg, root=str(tmp_path))  # 不给 label → top{top_n}
    assert path.endswith("金字塔决策包_top8.md")
    assert os.path.exists(path)


# ── 消息面段：板块消息卡 + 逐事件7字段 + 含义/影响取上游 + 防未来 as_of 守卫 ──
def _synth_ov_消息():
    """合成 消息驱动：一强一中板块 + 一条未来事件（应被守卫丢弃）+ 一条国际事件。"""
    return {
        "as_of": "2026-09-18",
        "消息驱动": {
            "as_of": "2026-09-18",
            "利好板块": [
                {
                    "board": "交通运输", "tag": "利好", "强弱": "强",
                    "关键事件": [
                        {"时间": "2026-09-17", "事件": "BDI创新高，欧线集运主力合约飙升超3%",
                         "方向": "利好", "可信度": "可信", "影响程度": "大", "执行度": "高", "来源": "一手"},
                        {"时间": "2026-09-07", "事件": "上港集团8月吞吐量同比下降14.7%",
                         "方向": "利空", "可信度": "可信", "影响程度": "大", "执行度": "高", "来源": "一手"},
                        {"时间": "2026-09-30", "事件": "未来事件·不该出现（时间晚于 as_of）",
                         "方向": "利好", "可信度": "可信", "影响程度": "大", "执行度": "高", "来源": "一手"},
                    ],
                    "依据": "油运运价暴涨引爆板块，地缘溢价共振。",
                    "持续性": "本周持续利好。",
                    "可靠性综述": "一手为主，出处明确。",
                    "龙头候选": [{"code": "601872", "name": "招商轮船", "已动": False},
                               {"code": "600233", "name": "圆通速递", "已动": True}],
                    "跟涨候选": [],
                },
                {
                    "board": "房地产", "tag": "利好", "强弱": "中",
                    "关键事件": [
                        {"时间": "2026-09-18", "事件": "存量房时代来临，世联行三连板",
                         "方向": "利好", "可信度": "可信", "影响程度": "大", "执行度": "中", "来源": "一手"},
                        {"时间": "2026-09-07", "事件": "某公司参加投资者接待日（小事件·中板块应被过滤）",
                         "方向": "中性", "可信度": "可信", "影响程度": "小", "执行度": "高", "来源": "一手"},
                    ],
                    "依据": "公积金扩容政策利好。",
                    "龙头候选": [{"code": "002285", "name": "世联行", "已动": True}],
                    "跟涨候选": [{"code": "600246", "name": "万通发展", "联动依据": "补涨待接力"}],
                },
            ],
        },
    }


def test_消息面段_卡片与逐事件字段():
    ov = _synth_ov_消息()
    txt = P.render_消息面(ov, as_of="2026-09-18")
    # 段头 + 词表 + 两个板块卡 + 国际汇总
    assert "## 市场·国际·板块消息面" in txt
    assert "消息面词表" in txt
    assert "交通运输 —— 利好 · 强" in txt
    assert "房地产 —— 利好 · 中" in txt
    assert "## 国际/宏观事件汇总" in txt
    # 逐事件 7 字段原样（方向/可信度/影响程度/执行度/来源）
    assert "方向利好·可信·影响大·执行高·一手" in txt
    assert "方向利空·可信·影响大·执行高·一手" in txt  # 利空原样保留、不粉饰
    # 国际标签启发式命中 BDI/欧线
    assert "🌐国际/宏观" in txt


def test_消息面段_含义影响取上游不编():
    ov = _synth_ov_消息()
    txt = P.render_消息面(ov, as_of="2026-09-18")
    # 含义/影响 = 上游依据/持续性/可靠性 原样 + 候选个股（render 不编研判）
    assert "含义(上游依据)：油运运价暴涨引爆板块，地缘溢价共振。" in txt
    assert "持续性：本周持续利好。" in txt
    assert "可靠性：一手为主，出处明确。" in txt
    assert "招商轮船601872" in txt and "圆通速递600233(已动)" in txt
    assert "万通发展600246[补涨待接力]" in txt  # 跟涨候选带联动依据
    # A6：禁裸 dict 进 prompt
    assert "{'" not in txt


def test_消息面段_防未来as_of守卫():
    ov = _synth_ov_消息()
    txt = P.render_消息面(ov, as_of="2026-09-18")
    # 时间晚于 as_of 的事件被丢弃，且显式记丢弃数
    assert "未来事件·不该出现" not in txt
    assert "防未来丢弃1" in txt


def test_消息面段_强深挖中精简():
    ov = _synth_ov_消息()
    txt = P.render_消息面(ov, as_of="2026-09-18")
    # 强板块保留全事件（含利空）；中板块过滤掉 影响小 事件
    assert "某公司参加投资者接待日" not in txt
    assert "中板块仅取影响大/中" in txt


def test_消息面段_无数据诚实标注():
    txt = P.render_消息面({"as_of": "2026-09-18"}, as_of="2026-09-18")
    assert "无消息驱动数据落盘" in txt
    assert "不编造" in txt


def test_render_package_含消息面段():
    """决策包整体渲染含 市场·国际·板块消息面 段（合成 pkg 走 render_package）。"""
    pkg = _synth_pkg()
    pkg["市场定调"]["消息驱动"] = _synth_ov_消息()["消息驱动"]
    txt = P.render_package(pkg)
    assert "## 市场·国际·板块消息面" in txt
    assert "## 国际/宏观事件汇总" in txt
    # 消息面段在 市场定调 之后、全板块概览 之前
    assert txt.index("## 市场定调") < txt.index("## 市场·国际·板块消息面") < txt.index("## 全板块概览")
    assert "{'" not in txt  # 全包仍禁裸 dict
