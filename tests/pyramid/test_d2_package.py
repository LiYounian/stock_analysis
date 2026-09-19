"""A6 决策包禁裸 dict + ★重点口径(focus_score·排除过热A/拐点A)、A12 过程文件落盘 的锁。

D4：render_package 文本内无 `{'`（裸 dict/json）。
D5：save_package 落 data/analysis/<as_of>/金字塔决策包_*.md，断言文件存在。
数据无关：market_overview 用 monkeypatch 假 _load；render/save 用合成 pkg。
"""
import os
import types

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


# ── W3 消息真伪辅证 新闻digest：紧凑(计数+最强1~2条·无url/长摘要) + 三态降级 + 落消息面panel ──
def _fake_news_res(freshness, fields):
    """仿 news_raw ToolResult：_新闻digest_lines 只用 .fields / .freshness。"""
    return types.SimpleNamespace(freshness=freshness, fields=fields)


def test_新闻digest_紧凑含计数不含url长摘要():
    res = _fake_news_res("fresh", {
        "条数": 5, "source_used": "baidu_news",
        "利好": 3, "利空": 1, "中性": 1, "未标注": 0,
        "news": [
            {"标签": "中性", "标题": "例行公告披露", "来源": "交易所", "时间": "2026-09-17 15:00",
             "url": "http://x/a", "摘要": "很长很长的摘要正文不该进 digest" * 10},
            {"标签": "利好", "标题": "签下大额海外订单落地放量" + "拖长标题" * 10, "来源": "证券时报",
             "时间": "2026-09-16 09:30", "url": "http://x/b", "摘要": "长摘要"},
            {"标签": "利空", "标题": "被立案调查", "来源": "公司公告", "时间": "2026-09-15 18:00",
             "url": "http://x/c", "摘要": "长摘要"},
        ],
    })
    lines = P._新闻digest_lines(res)
    assert len(lines) <= 3                       # ≤3 行（header + 最多 2 条）
    head = lines[0]
    assert "新闻digest" in head
    assert "利好3/利空1/中性1" in head           # 计数结构入 header
    assert "闭卷辨真伪依据" in head              # 措辞对闭卷模型成立（不暗示能下钻）
    body = "\n".join(lines)
    assert "http" not in body                    # 不带 url（原文全量仍只在 news_raw.fields）
    assert "很长很长的摘要正文" not in body       # 不带长摘要
    # 最强序：非中性(利好/利空)优先 → 中性"例行公告"不入 top2
    assert "利好" in body and "利空" in body
    assert "例行公告披露" not in body
    # 标题截断到 30 字 + 省略号
    assert "…" in body


def test_新闻digest_三态降级():
    miss = P._新闻digest_lines(_fake_news_res("missing", {"条数": 0, "news": []}))
    assert len(miss) == 1 and "无新闻覆盖" in miss[0]
    zero = P._新闻digest_lines(_fake_news_res("fresh", {"条数": 0, "source_used": "news", "news": []}))
    assert len(zero) == 1 and "0 条新闻" in zero[0]


def test_render_package_digest落消息面panel内():
    pkg = _synth_pkg()
    pkg["候选卡片"][0]["新闻digest_lines"] = [
        "【消息真伪辅证·新闻digest】近5条 利好3/利空1/中性1（全量原文在 news_raw/baidu_news；此 digest 即闭卷辨真伪依据）",
        "[利好] 签下大额海外订单｜证券时报·2026-09-16",
    ]
    txt = P.render_package(pkg)
    assert "【消息真伪辅证·新闻digest】" in txt
    assert "利好3/利空1/中性1" in txt
    # 落在【四·消息面】之后、【经验纪律·跨面】之前（消息面 panel 内、非其它面）
    assert txt.index("【四·消息面】") < txt.index("【消息真伪辅证·新闻digest】") < txt.index("【经验纪律·跨面】")
    assert "http" not in txt.split("【消息真伪辅证·新闻digest】", 1)[1].split("【经验", 1)[0]
    assert "{'" not in txt and '{"' not in txt   # 全包仍禁裸 dict


# ── 大盘定调段：读 market_forecast·三面齐全 + 效力caveat原样 + 缺数据降级 + 防未来守卫 ──
def _synth_mf(as_of="2026-09-17"):
    """合成 market_forecast/v1（对齐 09-17 真实结构）：proxy/hs300 双基准、
    5日分歧触发、breadth/sentiment/fundflow 快照、含关键诚实约束的 notes。"""
    return {
        "schema": "market_forecast/v1", "as_of": as_of, "model": "composite",
        "选股用β基准": {"默认": "proxy", "背景": "hs300",
                    "说明": "个股 β 读 proxy(≈中小盘);hs300 仅权重股背景;分歧时以 proxy 为准。"},
        "分歧标记": {"触发": True, "维度": {
            "1": {"触发": False, "类型": "一致"},
            "5": {"触发": True, "类型": "权重搭台中小盘偏弱",
                  "hs300_p_up": 0.5873, "proxy_p_up": 0.5029,
                  "hs300_direction": "偏多", "proxy_direction": "震荡", "方向档背离": True},
        }, "说明": "hs300 与 proxy 背离;勿把权重偏多读成个股偏多。"},
        "targets": {
            "hs300": {"name": "沪深300", "as_of": "2026-09-16", "适用范围": "权重股/大盘β",
                      "horizons": {
                          "1": {"p_up": 0.5094, "direction": "震荡", "prob_bucket": "方向中性"},
                          "5": {"p_up": 0.5873, "direction": "偏多", "prob_bucket": "上行概率偏高"},
                      }},
            "proxy": {"name": "全A等权代理指数", "as_of": as_of, "适用范围": "个股/中小盘β基准",
                      "horizons": {
                          "1": {"p_up": 0.5312, "direction": "震荡", "prob_bucket": "方向中性",
                                "factor_contrib": {"技术": -0.2165, "广度": -0.1677, "消息面": -0.0256, "资金流": 0.0}},
                          "5": {"p_up": 0.5029, "direction": "震荡", "prob_bucket": "方向中性",
                                "factor_contrib": {"技术": -0.0657, "广度": -0.1683, "消息面": 0.0219, "资金流": 0.0}},
                      }},
        },
        "breadth_snapshot": {"total": 5563.0, "adv": 2575.0, "dec": 2819.0,
                             "limit_up": 58.0, "limit_down": 6.0, "net_adv": -0.0439,
                             "above_ma20_ratio": 0.2804, "below_ma20_ratio": 0.7194, "median_pct": -0.07},
        "sentiment_snapshot": {"se_net": 178.0, "se_ratio": 0.918, "se_bull": 58.0, "se_bear": 2.0, "se_n": 69.0},
        "fundflow_snapshot": {"margin_date": "2026-09-16", "融资余额": 1333197305694.0,
                              "融资买入额": 89446018949.0, "融资融券余额": 1351858684135.0,
                              "note": "SSE市场级两融,盘后披露,已滞后至as_of前一交易日(防未来函数)"},
        "notes": ("档位=上行概率分位(方向口径),不是涨跌幅。⚠️ 整体方向命中~55%,较纯惯性有约 +4.8pp 的"
                  "统计边际(显著),但多空收益价差≈0(无经济 alpha)。每天真正参与判别的只有技术+广度两维:"
                  "资金流权重=0(kill-switch),消息面被自动降权到≈0。勿把高概率读成能赚钱。"),
    }


def test_大盘定调_两面结构():
    """接口收紧：只产 方向面 + 广度情绪资金面（源自 market_forecast）；板块强弱不产·仅衔接。"""
    txt = P.render_大盘定调(_synth_mf(), as_of="2026-09-17")
    assert "### 大盘定调" in txt
    assert "大盘词表" in txt
    # 方向面：proxy/hs300 + p_up + 方向档 + 分歧标记
    assert "方向面" in txt and "proxy" in txt and "hs300" in txt
    assert "p_up=0.503" in txt and "上行概率偏高" in txt
    assert "⚑分歧标记[触发·权重搭台中小盘偏弱]" in txt
    assert "方向档背离=True" in txt
    # 广度情绪资金面
    assert "广度情绪资金面" in txt
    assert "涨2575/跌2819" in txt and "涨停58/跌停6" in txt
    assert "净广度-0.044(多空均衡)" in txt
    assert "站上MA20 28.0%" in txt
    assert "多空net178" in txt
    assert "融资余额13332亿" in txt
    # 板块强弱不在本卡产出·仅衔接（避免与 render_boards 双源）
    assert "板块强弱不在本卡产出" in txt
    assert "#### 板块强弱" not in txt  # 不作为独立产出面
    # 全程禁裸 dict
    assert "{'" not in txt


def test_大盘定调_因子贡献归方向面且标clear_target_horizon():
    """因子贡献归方向面（在广度情绪资金面之前）、标清 target+horizon、资金流权重=0 具体化。"""
    txt = P.render_大盘定调(_synth_mf(), as_of="2026-09-17")
    assert "因子贡献" in txt
    assert "target=proxy" in txt          # 标清 target
    assert "· 1日 " in txt and "· 5日 " in txt  # 标清 horizon（proxy 双 horizon 都带）
    assert "资金流0.000" in txt            # 资金流权重=0 具体化佐证效力标注
    # 归方向面：因子贡献出现在 广度情绪资金面 之前
    assert txt.index("因子贡献") < txt.index("广度情绪资金面")


def test_大盘定调_效力caveat原样在位():
    """最关键诚实约束：段尾原样 surface notes 关键短语（防未来有人精简掉即挂）。"""
    txt = P.render_大盘定调(_synth_mf(), as_of="2026-09-17")
    assert "⚠️效力诚实标注" in txt
    assert "无经济 alpha" in txt
    assert "每天真正参与判别的只有技术+广度两维" in txt
    assert "勿把高概率读成能赚钱" in txt
    assert "+4.8pp" in txt


def test_大盘定调_缺market_forecast降级():
    txt = P.render_大盘定调(None, as_of="2026-09-17")
    assert "缺 market_forecast" in txt
    assert "降级中性" in txt
    assert "回退 sector_focus 薄读数" in txt


def test_大盘定调_防未来整体丢弃():
    """mf.as_of 晚于包 as_of → 整份预测丢弃 + 降级中性（不静默读未来）。"""
    txt = P.render_大盘定调(_synth_mf(as_of="2026-09-20"), as_of="2026-09-17")
    assert "防未来丢弃" in txt
    assert "降级中性" in txt


def test_大盘定调_防未来单target丢弃():
    """单基准 as_of 晚于包 as_of → 该基准丢弃、另一基准仍渲（targets 各自守卫）。"""
    mf = _synth_mf(as_of="2026-09-17")
    mf["targets"]["hs300"]["as_of"] = "2026-09-18"  # hs300 晚于包
    txt = P.render_大盘定调(mf, as_of="2026-09-17")
    assert "hs300" in txt and "晚于" in txt and "防未来丢弃" in txt
    assert "全A等权代理指数" in txt  # proxy 正常渲


def test_render_package_含大盘定调卡():
    """决策包整体渲染：market_forecast 可用→大盘卡为主读数、降级中性主句让位、板块轮动保留。"""
    pkg = _synth_pkg()
    pkg["市场定调"]["market_forecast"] = _synth_mf()
    txt = P.render_package(pkg)
    assert "### 大盘定调" in txt
    assert "⚠️效力诚实标注" in txt
    # 位置：市场定调 heading < 大盘定调 < 消息面
    assert txt.index("## 市场定调") < txt.index("### 大盘定调")
    # 可用时降级中性薄读数主句让位（定调依据仅兜底出现）
    assert "定调依据：" not in txt
    # 板块轮动(sector_focus)始终保留
    assert "板块轮动：" in txt
    assert "{'" not in txt


def test_render_package_缺market_forecast兜底薄读数():
    """决策包整体渲染：缺 market_forecast → 大盘卡降级 + 保留 sector_focus 薄读数兜底。"""
    pkg = _synth_pkg()  # _synth_pkg 无 market_forecast
    txt = P.render_package(pkg)
    assert "缺 market_forecast" in txt and "降级中性" in txt
    assert "当前市场风险偏好" in txt  # 兜底薄读数保留
    assert "定调依据：" in txt


# ── 决策包 canonical 钉死：多快照漂移消症状（钉死同一份 + 指纹检测底层漂移）──────
class _Row:
    """仿骨架 row 对象：消费方只用 len；内部字段仅证明序列化会把它替成 None。"""

    def __init__(self, code, 骨架分):
        self.code = code
        self.骨架分 = 骨架分
        self.子分 = {"量价自证": 1.0}
        self.来源标签 = ["K线过闸"]


def _make_build(counter):
    """假 build_package：每调一次骨架分 +10（模拟底层重跑漂移），skel 带 row 对象验序列化。"""
    def _build(as_of, root=None, top_n=15, **kw):
        counter["n"] += 1
        base = 60.0 + 10 * counter["n"]
        ov = {"as_of": as_of, "风险偏好": "中性", "重点板块": [], "规避板块池": [],
              "全板块": [], "market_forecast": None}
        skel = {"池规模": 4, "参与票数": 4, "计分票数": 3, "排序票数": 2,
                "否决票数": 1, "数据缺失票数": 1, "权重": {},
                "排序键": "x",
                "排序": [_Row("600000", base), _Row("600001", base - 1)],
                "排雷否决": [_Row("600002", 0.0)]}
        cards = [{"code": "600000", "名称": "浦发银行", "as_of": as_of,
                  "骨架分": base, "子分": {"量价自证": 80.0}, "来源标签": ["K线过闸"],
                  "面块": {"基本面": ["【x】blk"]}, "新闻digest_lines": ["【digest】x"]}]
        return {"as_of": as_of, "市场定调": ov, "骨架": skel,
                "候选卡片": cards, "top_n": top_n}
    return _build


def _canon_path(tmp_path, as_of="2026-09-17"):
    return tmp_path / "data" / "analysis" / as_of / P._CANONICAL_NAME


def test_canonical_钉死同一份跨调用(tmp_path, monkeypatch):
    """① 底层每算一次都漂，但连续两次 get_canonical_package 返回同一份、build 只跑一次、文件落盘。"""
    counter = {"n": 0}
    monkeypatch.setattr(P, "build_package", _make_build(counter))
    p1 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    p2 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    assert p1["候选卡片"][0]["骨架分"] == p2["候选卡片"][0]["骨架分"]  # 钉死
    assert counter["n"] == 1                                          # build 只跑一次
    assert _canon_path(tmp_path).exists()


def test_canonical_rebuild覆盖出新分(tmp_path, monkeypatch):
    """② rebuild=True 强制重算并覆盖（骨架分推进）。"""
    counter = {"n": 0}
    monkeypatch.setattr(P, "build_package", _make_build(counter))
    p1 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    p2 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, rebuild=True, log=lambda *_: None)
    assert p2["候选卡片"][0]["骨架分"] > p1["候选卡片"][0]["骨架分"]
    assert counter["n"] == 2


def test_canonical_topn不符触发重算(tmp_path, monkeypatch):
    """③ 已存 top_n=8，请求 top_n=15 → 触发重算并按新 top_n 落盘。"""
    counter = {"n": 0}
    monkeypatch.setattr(P, "build_package", _make_build(counter))
    P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    p2 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=15, log=lambda *_: None)
    assert p2["top_n"] == 15
    assert counter["n"] == 2


def test_inputs_fingerprint_随内容变且排除canonical自身(tmp_path):
    """④ 指纹随底层 json 内容变化；canonical 自身文件不参与指纹。"""
    d = tmp_path / "data" / "analysis" / "2026-09-17"
    d.mkdir(parents=True)
    (d / "sector_focus.json").write_text('{"a":1}', encoding="utf-8")
    fp1 = P._inputs_fingerprint(str(tmp_path), "2026-09-17")
    assert fp1
    (d / "sector_focus.json").write_text('{"a":2}', encoding="utf-8")
    fp2 = P._inputs_fingerprint(str(tmp_path), "2026-09-17")
    assert fp1 != fp2
    # canonical 自身写进同目录也不改变指纹
    (d / P._CANONICAL_NAME).write_text('{"top_n":8}', encoding="utf-8")
    assert P._inputs_fingerprint(str(tmp_path), "2026-09-17") == fp2


def test_canonical_底层漂移不rebuild仍返钉死且告警(tmp_path, monkeypatch):
    """⑤ 生成后改底层输入、不 rebuild → 仍返钉死包（骨架分不变、不重算）且 log 漂移告警。"""
    counter = {"n": 0}
    monkeypatch.setattr(P, "build_package", _make_build(counter))
    d = tmp_path / "data" / "analysis" / "2026-09-17"
    d.mkdir(parents=True)
    (d / "sector_focus.json").write_text('{"a":1}', encoding="utf-8")
    p1 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    s1 = p1["候选卡片"][0]["骨架分"]
    (d / "sector_focus.json").write_text('{"a":999,"more":true}', encoding="utf-8")  # 底层漂
    logs = []
    p2 = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=logs.append)
    assert p2["候选卡片"][0]["骨架分"] == s1     # 仍返钉死包
    assert counter["n"] == 1                     # 没重算
    assert any("底层输入已漂" in m for m in logs)  # 漂移告警


def test_canonical_row对象落盘reload后render不抛(tmp_path, monkeypatch):
    """⑥ skel 含 row 对象 → 序列化替 [None]*n；reload 后 render_package 不抛、张数用标量键正确。"""
    counter = {"n": 0}
    monkeypatch.setattr(P, "build_package", _make_build(counter))
    pkg = P.get_canonical_package("2026-09-17", root=str(tmp_path), top_n=8, log=lambda *_: None)
    assert pkg["骨架"]["排序"] == [None, None]   # row 对象已替成可 json 的 None
    assert pkg["骨架"]["排雷否决"] == [None]
    txt = P.render_package(pkg)                   # 不抛
    assert txt.startswith("# 金字塔决策包")
    assert "计分3=排序2+否决1" in txt             # 计数走标量键，非误读 None 列表
    # 从磁盘 reload 同样可渲染
    import json
    with open(_canon_path(tmp_path), encoding="utf-8") as f:
        reloaded = json.load(f)
    assert P.render_package(reloaded).startswith("# 金字塔决策包")
