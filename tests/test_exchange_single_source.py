"""**代码→交易所** 单一真源(`tools.config.exchange`)的判据锁 + 各源委托锁 + 防复发闸。

## 这组测试为什么存在

这条规则曾在项目里被独立实现 12 遍,各自演化 → 2026-09-03 北交所现行 920 段
被**同时判错 3 处**(`fa4c37b`),整段 333 只(主档 6.1%,且主档里北交所全部在这一段)
静默取不到数据。同一天顺带查出另外 3 处同类错(新浪串号 / baostock 静默空 /
东财把 900 沪B 判成深市)。

所以这里锁三件事:
  1. **判据只有一份**,且各代码段路由正确(尤其 920 必须赢过 9,而 900 沪B 仍归沪);
  2. 各数据源**只做薄委托**,并各自写明"判不出时"的兜底口径(它们彼此不同);
  3. **防复发闸**:`tools/`、`web/` 下不允许新增"自己判前缀"的实现,白名单是显式的。

断言里的表格值都是**真调接口实测**得来(见各断言旁注),不是推断——重写这些数字前
先自己去调一遍接口。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

from tools.collectors import baostock_src, financial, gtimg_quote, market, news
from tools.config import exchange


# ═══════════════════════════════ 一、真源判据 ═══════════════════════════════

# (代码, 期望交易所) —— 覆盖 6 / 9(900沪B) / 920 / 0 / 2(深B) / 3 / 4 / 8 各段
_SEGMENTS = [
    ("600000", "sh"),   # 沪主板
    ("688008", "sh"),   # 科创板
    ("900901", "sh"),   # 900xxx 沪B —— **仍归沪市**,不能被 920 规则带走
    ("920002", "bj"),   # 北交所**现行**段 —— 必须先于 9→sh
    ("920819", "bj"),
    ("430047", "bj"),   # 北交所历史段 43
    ("830799", "bj"),   # 北交所历史段 83
    ("870199", "bj"),   # 北交所历史段 87
    ("000001", "sz"),   # 深主板
    ("002156", "sz"),   # 深中小
    ("200011", "sz"),   # 200xxx 深B
    ("300750", "sz"),   # 创业板
]

# 判不出的输入 —— 真源必须返回 None,**绝不猜一个前缀**
_UNKNOWN = ["", "1", "100000", "00700", "5", "abcdef", "6000AB", "60000", "6000000", "700"]


@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_真源_各代码段路由(code, ex):
    assert exchange.exchange_of(code) == ex


@pytest.mark.parametrize("code", _UNKNOWN)
def test_真源_判不出一律None不猜前缀(code):
    # 为什么必须是 None:错前缀不是"取不到数",而是可能**静默拿到别人的数据**
    # (实测新浪 sz920002 返 HTTP 200 + 40 条全站泛资讯)。兜底策略是调用方的口径,不藏在真源里。
    assert exchange.exchange_of(code) is None
    assert exchange.prefixed(code) is None
    assert exchange.upper_prefixed(code) is None
    assert exchange.dotted(code) is None
    assert exchange.is_a_code(code) is False
    assert exchange.is_bj(code) is False


def test_真源_920必须赢过9_而900沪B仍归沪():
    """整组断言的核心:判据必须按前缀长度精确匹配,不能只看首位。"""
    assert exchange.exchange_of("920002") == "bj", "920 段是北交所,判成 sh 会整段(333只)静默丢数"
    assert exchange.exchange_of("900901") == "sh", "900 段是沪市B股,不能被 920 规则带走"
    assert exchange.is_bj("920002") is True
    assert exchange.is_bj("900901") is False


@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_真源_派生形式全由一处判定生成(code, ex):
    assert exchange.prefixed(code) == f"{ex}{code}"
    assert exchange.upper_prefixed(code) == f"{ex.upper()}{code}"
    # dotted 是 baostock 协议,北交所在该源不可用(见第三节实测)
    assert exchange.dotted(code) == (None if ex == "bj" else f"{ex}.{code}")


def test_真源_非字符串输入也吃():
    assert exchange.exchange_of(920002) == "bj"
    assert exchange.prefixed(920002) == "bj920002"
    assert exchange.exchange_of("  600000  ") == "sh"


# ═══════════════════════════ 二、各源薄委托 + 兜底口径 ═══════════════════════════
#
# 各源"判不出时"的兜底**彼此不同**,是各自的接口口径,所以写在调用方、这里逐个锁住。

@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_委托_gtimg裸前缀(code, ex):
    assert gtimg_quote.market_prefix(code) == ex


@pytest.mark.parametrize("code", _UNKNOWN)
def test_委托_gtimg兜底回落sz(code):
    """gtimg 口径:判不出回落 sz(历史行为;gtimg 对不存在的 symbol 返空行,上层按缺失处理)。"""
    assert gtimg_quote.market_prefix(code) == "sz"


@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_委托_market带前缀符号(code, ex):
    assert market.market_prefix(code) == f"{ex}{code}"


@pytest.mark.parametrize("code", _UNKNOWN)
def test_委托_market兜底原样透传(code):
    """market 口径:判不出**原样透传**,让下游接口自己报错,不硬贴可能错的前缀。"""
    assert market.market_prefix(code) == code


@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_委托_financial大写前缀(code, ex):
    assert financial._em_symbol(code) == f"{ex.upper()}{code}"


def test_委托_financial_900沪B归SH_迁真源顺带修的潜在错():
    """原实现只把首位 6 判 SH,900xxx 沪B 落到 SZ 兜底 → `SZ900901`。

    实测(2026-09-03)`ak.stock_balance_sheet_by_report_em`:
        SH900901 → 121 期,SECURITY_NAME_ABBR='云赛B股'  ✅
        SZ900901 → 直接报错(返回 None)                  ❌
    当前主档 5539 只里 9 开头**全是 920 段(333 只)、无 900 沪B**,故真实票池零影响。
    """
    assert financial._em_symbol("900901") == "SH900901"


def test_委托_financial_zfill与兜底SZ():
    """financial 口径:入参先 zfill(6)(历史行为),判不出回落 SZ。"""
    assert financial._em_symbol("1") == "SZ000001"
    assert financial._em_symbol(2156) == "SZ002156"
    assert financial._em_symbol("00700") == "SZ000700"   # zfill 后变 6 位深市码,非港股路径


# ═══════════════════ 三、新浪个股新闻:北交所走 bj,判不出显式降级 ═══════════════════

@pytest.mark.parametrize("code,ex", _SEGMENTS)
def test_新浪symbol_全段走真源(code, ex):
    assert news._sina_sym(code) == f"{ex}{code}"


def test_新浪symbol_北交所必须bj_实测锁():
    """2026-09-03 真调 `vip.stock.finance.sina.com.cn/corp/view/vCB_AllNewsStock.php`:

        bj920002  200 / datelist 有 / 40 条 → ✅ 万达轴承本股新闻
        bj430047  200 / datelist 有 / 40 条 → ✅ 诺思兰德本股新闻
        sz920002  200 / datelist 有 / 40 条 → ❌ 新浪**全站泛资讯流**
        sh920002  200 / datelist 有 / 40 条 → ❌ 同一批,与 sz920002 前三条完全相同
        sz430047  200 / datelist 有 / 40 条 → ❌ 同一批默认流
        920002    200 / datelist 无 / 0 条  → 空

    **新浪覆盖北交所,正确形式是 `bj{code}`。** 而错前缀不是取不到数,是 HTTP 200 +
    40 条别人的资讯 —— 上层从状态码/条数/非空完全看不出异常,北交所票会被挂上无关
    新闻再喂进情绪 LLM,比拉不到糟得多。所以这条断言在的意义是:**任何时候都不许
    用兜底前缀去拉新浪个股新闻页**。

    另注:`<title>` 由 symbol 里的数字段解析,前缀错了仍显示"万达轴承(920002)",
    **不能用 title 匹配来验真**。
    """
    assert news._sina_sym("920002") == "bj920002"
    assert news._sina_sym("430047") == "bj430047"
    assert news._sina_sym("830799") == "bj830799"
    assert news._sina_sym("870199") == "bj870199"


@pytest.mark.parametrize("code", _UNKNOWN)
def test_新浪symbol_判不出返None不兜底(code):
    """新浪源**没有可用兜底前缀**(见上一条),判不出只能返 None 让调用方跳过。"""
    assert news._sina_sym(code) is None


def test_新浪抓取_判不出交易所时显式降级且不触网(monkeypatch, caplog):
    """语义锁:sym=None → 记降级日志 + 返回 [],**绝不带错前缀去请求**。"""
    def _boom(sym, page):                       # 一旦真去请求就炸,证明没触网
        raise AssertionError(f"不该请求新浪:sym={sym!r}")
    monkeypatch.setattr(news, "_fetch_sina_html", _boom)
    with caplog.at_level("WARNING", logger="collectors.news"):
        assert news._fetch_sina("100000", "2026-08-27") == []
    assert any("降级" in r.getMessage() for r in caplog.records), "必须记降级日志,不能静默返空"


def test_新浪抓取_北交所正常走bj前缀(monkeypatch):
    """北交所票会真的用 bj 前缀去请求(而不是被跳过)。"""
    seen: list[str] = []

    def _fake(sym, page):
        seen.append(sym)
        if page > 1:                            # 第 2 页起返空 → 翻页早停(否则同内容会被重复计数)
            return ""
        return ('<div class="datelist"><ul>2026-09-02 10:00'
                '<a href="http://x/1">万达轴承获融资买入</a></ul>')
    monkeypatch.setattr(news, "_fetch_sina_html", _fake)
    items = news._fetch_sina("920002", "2026-08-27")
    assert seen and all(s == "bj920002" for s in seen), f"必须用 bj920002,实际 {seen}"
    assert len(items) == 1 and "万达轴承" in items[0]["title"]


# ═════════════════ 四、baostock 不覆盖北交所:显式不可用 + 记降级 ═════════════════

def test_baostock_北交所显式不可用_实测锁():
    """2026-09-03 真调 `bs.query_history_k_data_plus`:

        sh.600000  error_code 0 success       11 行 ✅
        bj.920002  error_code 10004011「股票代码未标识sh或sz」  0 行
        bj.430047  error_code 10004011        0 行
        sz.920002  error_code 0 **success**   **0 行** ← 静默空
        sh.920002  error_code 0 **success**   **0 行** ← 静默空

    baostock 协议只认 `sh.`/`sz.`,**根本不覆盖北交所**。原实现把 920 按"9 开头"映到
    `sh.920002` → success + 0 行,调用方看到的是"这只票没有历史数据",而不是"这个源
    不支持北交所"。故显式返回 None 表达"源不支持"。
    """
    assert baostock_src.bs_code("920002") is None
    assert baostock_src.bs_code("430047") is None
    assert baostock_src.bs_code("830799") is None
    assert baostock_src.bs_code("600000") == "sh.600000"
    assert baostock_src.bs_code("900901") == "sh.900901"
    assert baostock_src.bs_code("000001") == "sz.000001"


def test_baostock_fetch_one_北交所抛错而非静默空(caplog):
    """语义锁:北交所 → 记降级 + 抛 ValueError,不去请求也不返空 df。"""
    pytest.importorskip("baostock")
    with caplog.at_level("WARNING", logger="collectors.baostock"):
        with pytest.raises(ValueError, match="不支持"):
            baostock_src.fetch_one("920002", "2026-08-01", "2026-09-03")
    assert any("降级" in r.getMessage() for r in caplog.records)


# ═══════════════════════ 五、防复发闸:不许再新增一处自己判前缀 ═══════════════════════

# 匹配"拿代码前缀跟数字字面量比"的写法:
#   .startswith("920") / .startswith(("6","9"))
#   code[0] in ("6","9") / code[0] == "6"
#   code[:2] in ("92","83") / code[:3] == "688"
_PREFIX_JUDGE_RE = re.compile(
    r'\.startswith\(\s*\(?\s*["\']\d{1,3}["\']'
    r'|\[\s*0\s*\]\s*(?:==|in)\s*[\(\["\']\s*["\']?\d'
    r'|\[\s*:\s*[123]\s*\]\s*(?:==|in)\s*[\(\["\']\s*["\']?\d'
)

# 白名单:**每加一行都必须写清为什么它不是"代码→交易所"**。
# 加新条目 = 明确承认又开了一处独立判据,请先想清楚能不能改成委托 `tools.config.exchange`。
_ALLOWED: dict[str, str] = {
    # —— 真源本体 ——
    "tools/config/exchange.py":
        "单一真源本体,判据就该只在这里",
    # —— 指数代码段,与个股所属交易所不是同一件事 ——
    "tools/collectors/index.py":
        "399/899 是指数代码段(深证系/北证系指数),判的是指数归属不是个股交易所",
    # —— 板块 / 涨跌停线 / 策略排除口径:输出是板块名或布尔,不是交易所 ——
    "tools/analysis/market_forecast/breadth.py":
        "board_of 判创业板/科创板/北交所以取涨停线,900 沪B 归沪市但没有 10% 涨停线,与交易所不同构",
    "tools/backtest/run_s01_backtest.py":
        "_board 是回测分档标签(沪主板/深主板/创业板/科创板/北交所);已知漏 920(归'其他'),口径变更另开任务",
    "tools/backtest/backtest_newhigh.py":
        "_is_20cm 判 20cm 涨跌幅板(300/301/688/689),是涨跌停口径不是交易所",
    "tools/strategy/reversal_turnover.py":
        "_code_head_excluded 策略排除口径(剥离创业/科创/北交/B股);含 '9' 故 920 与 900 都被排除",
    "tools/strategy/small_cap.py":
        "_code_head_excluded 同上,与 reversal_turnover 同口径",
    "tools/collectors/universe.py":
        "_is_bj 是票池排除谓词;已知漏 920(exclude_bj=True 时排不掉 920 段),口径变更另开任务",
    "tools/pipeline/intraday_snapshot.py":
        "_looks_like_a_code 是'长得像A股代码吗'的兜底谓词(优先走 code_name.json);"
        "已知漏 9,口径变更另开任务",
    # —— 东财 secid:市场编号不是 sh/sz/bj,且本机墙住无法实证 ——
    "tools/collectors/fundflow.py":
        "_secid 输出东财市场编号(1./0.)不是交易所前缀;920 段疑判成沪市但本机被东财指纹墙"
        "拦住无法实证(curl 56),不据猜改",
}


def _scan_prefix_judges() -> dict[str, list[tuple[int, str]]]:
    """扫 tools/ web/ 下所有 .py,返回 {相对路径: [(行号, 行内容)]}(已剔行尾注释)。"""
    root = Path(__file__).resolve().parents[1]
    hits: dict[str, list[tuple[int, str]]] = {}
    for sub in ("tools", "web"):
        base = root / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*.py")):
            src = io.open(p, encoding="utf-8").read()
            for i, ln in enumerate(src.splitlines(), 1):
                ln_nc = re.sub(r"#.*$", "", ln)          # 去行尾注释(docstring 里的示例表格不算实现)
                if _PREFIX_JUDGE_RE.search(ln_nc):
                    hits.setdefault(str(p.relative_to(root)), []).append((i, ln.strip()))
    return hits


def test_防复发_不得新增自己判前缀的实现():
    """`tools/`、`web/` 下"拿代码前缀比数字字面量"的文件必须在白名单内。

    这条闸门的意义:920 段能同时错 3 处,根因不是那 3 处写错,而是**同一条规则被独立
    实现了 12 遍**、各自演化。真源到位后,任何新增的独立判据都应该先问一句"能不能
    改成委托 tools.config.exchange"。
    """
    unexpected = {f: ls for f, ls in _scan_prefix_judges().items() if f not in _ALLOWED}
    assert not unexpected, (
        "以下文件自己判代码前缀,但不在白名单里 —— 请改成委托 tools.config.exchange,"
        "确实不是'代码→交易所'的话再加白名单并写明理由:\n"
        + "\n".join(f"  {f}\n" + "\n".join(f"      {i}: {ln}" for i, ln in ls)
                    for f, ls in sorted(unexpected.items())))


def test_防复发_采集层交易所路由已全部委托真源():
    """A 类 5 处(真正做"代码→交易所路由"的)必须都不在白名单里 = 已改薄委托。"""
    routed = ["tools/collectors/gtimg_quote.py", "tools/collectors/market.py",
              "tools/collectors/financial.py", "tools/collectors/news.py",
              "tools/collectors/baostock_src.py"]
    hits = _scan_prefix_judges()
    still_judging = [f for f in routed if f in hits]
    assert not still_judging, f"这些源本该只做薄委托,却还在自己判前缀: {still_judging}"
    assert all(f not in _ALLOWED for f in routed), "交易所路由源不该出现在白名单里"


def test_防复发_白名单没有过期条目():
    """白名单里的文件若已不再自己判前缀(被顺手清理了),就该把条目删掉,别留腐化项。"""
    hits = _scan_prefix_judges()
    stale = [f for f in _ALLOWED if f != "tools/config/exchange.py" and f not in hits]
    assert not stale, f"白名单条目已无对应实现,请删除: {stale}"
