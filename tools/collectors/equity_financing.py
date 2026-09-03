"""存量融资与解禁采集(存续可转债 / 定增 / 限售解禁)—— 逐票分析「固定一问」的数据底座。

需求:docs/计划/09-03复盘反哺排期.md §5(D · 存续可转债／定增／解禁 固定一问)。
动机:09-03 复盘发现 603270 金帝股份有**在存续可转债**(金帝转债 113706),转股摊薄与
强赎/回售条款是中期变量,而 09-02 的逐票深度分析完全没纳入这一项。这类「存量融资/解禁
事项」不能靠分析师临场想起,必须落成可采集字段,让逐票分析固定回答一问:
**「这票有无存续可转债 / 在推进的定增 / 临近的限售解禁?」**

===== 数据源(全部本机实调过,见 docs 报告)=====
可转债(可得):
  · ak.bond_zh_cov()                      —— 全市场可转债一览(1000+ 行),含 正股代码/债券代码/
                                             转股价/发行规模/申购日期/上市时间/信用评级。**市场级一次拉**。
  · ak.bond_zh_cov_info(symbol=债券代码,
                        indicator="基本信息") —— 单债详情:LISTING_DATE / DELIST_DATE / EXPIRE_DATE /
                                             TRANSFER_START_DATE / REDEEM_TRIG_PRICE(强赎触发价)/
                                             RESALE_TRIG_PRICE(回售触发价)/ ACTUAL_ISSUE_SCALE。
                                             **只对「正股在名单里且确有转债」的票拉**(极少)。
定增(部分可得):
  · ak.stock_qbzf_em()                    —— 全市场**已实施**增发明细(5000+ 行),含 发行方式/
                                             发行价格/发行总数/发行日期/增发上市日期/锁定期。**市场级一次拉**。
  · ak.stock_zh_a_disclosure_report_cninfo(symbol, keyword=..., start_date, end_date)
                                          —— 巨潮公告标题检索,自带**公告时间**(天然披露日锚点);
                                             用于识别「**在推进中**的定增」(预案/董事会/受理/注册/发行)。
  ⚠ 东财 datacenter 的「增发预案」结构化报表(RPT_SEO_PLAN 等一族)实调**全部 success=false**,
    即**结构化的定增预案进度表源不可得**;本模块用公告标题检索作为替代口径,并在
    `源状态.定增` 里标 "qbzf+公告检索",让消费方知道「推进中」是标题级证据、不是结构化进度。
  ⚠ **公告条数 ≠ 定增笔数**:一笔定增从预案到发行结果会出十几条过程公告。故公告先归一成
    带 `阶段信号`(进行/完成/终止)的记录,再由 `aggregate_plan_rounds` 按时序**聚合成笔**,
    `定增.推进中` 报的是**笔数**。笔级状态区分 **已实施**(股份已发出→摊薄已发生、通常带锁定期)
    与 **已终止**(股份从未发出→摊薄不会发生)——两者对摊薄的含义相反,不可同桶。
解禁(可得,双源互补):
  · ak.stock_restricted_release_queue_sina(symbol) —— 有 **公告日期**(防未来函数的唯一权威锚点),
                                                     但字段粗(解禁日期/解禁数量万股/解禁股流通市值亿)。
  · ak.stock_restricted_release_queue_em(symbol)   —— 字段丰富(占总市值比例/占流通市值比例/
                                                     限售股类型/未解禁数量),但**无公告日期**。
  → 以 sina 为主(带披露日),em 按 |解禁日期差| ≤ 7 自然日就近匹配做增强。

===== 防未来函数(硬红线)=====
解禁时间表天然含**未来日期**,这是合法的:它是「已披露的未来安排」,不是未来价格。
但**披露日晚于 as_of 的记录必须剔除** —— 只能用「分析时点及之前已经披露」的信息。
本模块的实现:
  1. 每条归一记录都带 `披露日`(可转债=min(申购日期, 上市日期);定增已实施=发行日期;
     定增推进中=公告时间;解禁=sina 公告日期)。
  2. `summarize_asof()` 里 `_visible()` 做**唯一闸门**:`披露日 is None or 披露日 > as_of` → 剔除,
     并计入 `剔除` 计数(**显式降级,不静默**)。
  3. em 解禁表的 `解禁后20日涨跌幅` 是**前瞻收益**(未来函数),归一阶段**直接丢弃**,不落盘。
  4. em 解禁表的 `解禁前一交易日收盘价` / `实际解禁数量市值` 对未来解禁行是「按今价折算」,
     归一时改名 `折算市值_按采集日价` 并标 `市值口径="采集日价折算"`,消费方不得当历史价用。
  5. 读缓存走 `store.get_raw_resolved(date=as_of)` → 只回退到 **≤as_of** 的采集分区,绝不读未来分区。

===== 落盘契约 =====
- raw kind = "equity_financing"(json),按 code + 采集日分区(走 store 层)。
- 市场级共享名单(可转债一览 / 已实施增发)落同 kind 的伪 code `"_market"`,
  进程内 memo + 落盘复用 → 一次运行只拉一次,不按票重复拉全市场表。
- **skip-if-cached**:`FINANCING_STALE_DAYS`(默认 30 天)内已有缓存 → 跳过不重采(幂等)。
  理由见文末「刷新粒度」。`force=True` 或环境变量可强刷。
- **优雅降级**:任一维度源失败 → 该维度 payload 里写 `源状态[维度]="源不可得"` + 进 `降级[]`,
  **可判定的空**(不是静默的 [] 冒充「确实没有」),整批不中断。

⚠ 非投资建议;本模块只搬运公开披露数据。

===== 刷新粒度建议 =====
可转债存续/条款、定增进度、解禁时间表都是**低频**变量(月级),日内不会变。故:
  · 逐票缓存 30 天新鲜(`FINANCING_STALE_DAYS`),超期才重采 → 全A 一个月只采一轮。
  · 市场级两张表(bond_zh_cov / stock_qbzf_em)按 `FINANCING_MARKET_STALE_DAYS`(默认 7 天)
    刷新,一次拉取即覆盖全部票,成本 2 个请求;每票只留最近 3 次已实施增发(更早锁定期必已到期);逐票只拉 sina/em 解禁队列 + 公告检索(+ 有转债的票才拉单债详情)。
  · 例外强刷:票上出现「可转债/定增/解禁」相关公告时,由上层传 force=True 定点刷新。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import date as _date
from datetime import timedelta

from tools.collectors._retry import retry_call
from tools.store import repo as store

logger = logging.getLogger("collectors.equity_financing")

_KIND = "equity_financing"
_MARKET_CODE = "_market"                # 市场级共享名单的伪 code(可转债一览 / 已实施增发)

# 门控/窗口(env 可覆盖,不改 settings.py,保持文件归属边界)
FINANCING_STALE_DAYS = float(os.getenv("FINANCING_STALE_DAYS", "30"))   # 逐票缓存新鲜期(天)
MARKET_STALE_DAYS = float(os.getenv("FINANCING_MARKET_STALE_DAYS", "7"))  # 市场级名单新鲜期(天)
_SEO_KEEP_PER_CODE = int(os.getenv("FINANCING_SEO_KEEP", "3"))   # 每票只留最近 N 次已实施增发
                                                                # (更早的锁定期必已到期,留着只增大落盘)
_FETCH_SLEEP = float(os.getenv("FINANCING_FETCH_SLEEP", "0.4"))
_PLAN_LOOKBACK_DAYS = int(os.getenv("FINANCING_PLAN_LOOKBACK_DAYS", "540"))  # 定增公告检索回看
_UNLOCK_MATCH_TOL_DAYS = 7              # sina↔em 解禁日就近匹配容差(自然日)
NEAR_UNLOCK_DAYS = int(os.getenv("FINANCING_NEAR_UNLOCK_DAYS", "90"))    # 「临近解禁」窗口

# 「在推进中的定增」标题正则:必须是**向特定对象/非公开发行股票**类,排除可转债类公告
_PLAN_KEYWORDS = ("向特定对象发行", "非公开发行", "定向增发")
_PLAN_TITLE_POS = re.compile(r"(向特定对象发行|非公开发行|定向增发)")
_PLAN_TITLE_NEG = re.compile(r"(可转换公司债券|可转债|向不特定对象发行|公司债券|优先股)")
# 阶段信号三分类(判定顺序 = 已实施 → 终止 → 进行,**先看终局信号**;顺序理由见 plan_stage_signal):
#   · 终止:这笔定增死了(终止/撤回/失效/不予注册)→ 股份**从未发出**,摊薄永不发生。
#   · 已实施:这笔定增**已经发行落地**。除「发行结果/发行情况报告书/上市公告书」这类直接结果公告外,
#     必须覆盖**发行完成后的配套/程序文件**:发行过程和认购对象合规性(律师/保荐核查报告)、验资报告、
#     募集资金专户三方监管协议、股本变动/变更注册资本。这些文件只可能出现在发行完成之后,
#     标题里却带「审核报告」「方案」字样 → 只按 LIVE 白名单判会把**已完成**的定增误判成推进中
#     (实测 603161 科华控股:定增 2026-08-07 完成登记托管、08-11 公告发行结果,却报「推进中 12 条」)。
#   · 进行:预案/受理/问询回复/同意注册等在途过程词。
# ⚠ 终止 vs 已实施 **不可同桶**:对摊薄的含义完全相反(已实施=摊薄已发生+通常带锁定期;
#   终止=摊薄永不发生)。笔级分类必须分开,否则下游问不出「这票有没有既成的摊薄」。
# ⚠ 有意**不**把「中止」放进终止:审核中止是可恢复的暂停态,不是终局(宁可当在途,不灭真信号)。
_PLAN_STAGE_TERMINATED = re.compile(r"(终止|撤回|失效|到期失效|不予)")
_PLAN_STAGE_DONE = re.compile(
    r"(发行结果|发行情况报告书|上市公告书|新增股份.*上市|"
    r"发行过程(和|及)认购对象|认购对象(的)?合规性|验资报告|"
    r"募集资金.*(专户|专项账户).*监管协议|三方监管协议|"
    r"股本变动|变更注册资本|注册资本变更|新增注册资本)")
_PLAN_STAGE_LIVE = re.compile(r"(预案|方案|议案|申请|受理|问询|回复|审核|过会|同意注册|批复|核准|募集说明书|发行方案|反馈意见)")

# 公告级 `阶段` 取值:"推进中"(在途过程公告)/ "终态公告"(该笔已走到终局的证据公告)。
# **有意用中性名**「终态公告」:它只表示「这条公告是关笔证据」,不表达摊薄方向;
# 方向由 `阶段信号`("完成"/"终止")与**笔级** `状态`("已实施"/"已终止")表达。
_STAGE_ANN_LIVE = "推进中"
_STAGE_ANN_FINAL = "终态公告"
_SIGNAL_TO_STAGE = {"进行": _STAGE_ANN_LIVE, "完成": _STAGE_ANN_FINAL, "终止": _STAGE_ANN_FINAL}
# 笔级状态(聚合后的「一笔定增」的状态)
_ROUND_LIVE, _ROUND_DONE, _ROUND_TERM = "推进中", "已实施", "已终止"
# 一笔定增关帐后,后续这么多天内**再来的终局类公告**算它的收尾文件串,不另开一笔。
# 实测 603161:发行结果那几天连出 7 条终局类文件(发行情况报告书/上市公告书/发行结果暨股本变动/
# 律师与保荐的合规性意见…),不合并就会把**一笔**报成「已实施 7 笔」——和「推进中 12」同一种荒谬。
# 取 180 天:配套文件通常数日至数周出齐,而同一票的下一笔定增必然先出预案(进行信号)才开帐,
# 不会以终局公告开头,故窗口取宽不会把两笔粘成一笔。
_ROUND_TAIL_DAYS = int(os.getenv("FINANCING_ROUND_TAIL_DAYS", "180"))

_MEM: dict[str, object] = {}            # 进程内 memo(市场级名单)
_MISSING_CACHE: list[dict] = []          # build_financing_block 无缓存时的显式降级记录(见文末)


# ————————————————————————————————————————————————
# 小工具
# ————————————————————————————————————————————————
def _to_float(v):
    """宽松转 float;空/NaN/不可解析 → None。"""
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
        return None if f != f else f            # NaN
    except (TypeError, ValueError):
        return None


def _norm_date(v) -> str | None:
    """任意日期表示 → 'YYYY-MM-DD';空/NaT/不可解析 → None。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none", "-"):
        return None
    s = s.split(" ")[0].split("T")[0]
    d = s.replace("-", "").replace("/", "").replace(".", "")
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    return None


def _norm_code(v) -> str:
    """代码归一为 6 位零填充字符串(sina 解禁表会把 002811 返成 '2811')。"""
    s = re.sub(r"\D", "", str(v or ""))
    return s.zfill(6) if s else ""


def _strip_em(s) -> str:
    """去掉巨潮检索返回标题里的 <em> 高亮标记。"""
    return re.sub(r"</?em>", "", str(s or "")).strip()


def _days_between(a: str | None, b: str | None) -> int | None:
    """b - a 的自然日差;任一为空 → None。"""
    if not a or not b:
        return None
    try:
        ya, ma, da = (int(x) for x in a.split("-"))
        yb, mb, db = (int(x) for x in b.split("-"))
        return (_date(yb, mb, db) - _date(ya, ma, da)).days
    except (ValueError, TypeError):
        return None


def _gap_days(ref: str, d: str | None, default: int = 10 ** 6) -> int:
    """d − ref 的自然日差;不可解析 → default。**注意 0 是合法值**(同日),不可用 `or` 兜底。"""
    v = _days_between(ref, d)
    return default if v is None else v


def _lock_months(s) -> int | None:
    """锁定期文本 → 月数。'3年'→36,'12个月'→12,'1年'→12;不可解析 → None。"""
    t = str(s or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*年", t)
    if m:
        return int(round(float(m.group(1)) * 12))
    m = re.search(r"(\d+)\s*个?月", t)
    if m:
        return int(m.group(1))
    return None


def _add_months(d: str | None, months: int | None) -> str | None:
    """日期 + n 月(按同日,月末回退到该月最后一天)。任一为空 → None。"""
    if not d or months is None:
        return None
    try:
        y, mo, dd = (int(x) for x in d.split("-"))
    except (ValueError, TypeError):
        return None
    total = (y * 12 + (mo - 1)) + months
    y2, mo2 = total // 12, total % 12 + 1
    for back in range(4):                       # 3.31 + 1月 → 4.30
        try:
            return _date(y2, mo2, dd - back).isoformat()
        except ValueError:
            continue
    return None


# ————————————————————————————————————————————————
# 层 1:原始拉取(每个函数一个源,便于单测 mock)
# ————————————————————————————————————————————————
def _fetch_cb_universe_raw() -> list[dict]:
    """ak.bond_zh_cov() 全市场可转债一览 → records。空 → 抛 ValueError(可判定)。"""
    import akshare as ak
    df = ak.bond_zh_cov()
    if df is None or df.empty:
        raise ValueError("bond_zh_cov 返回空")
    return df.to_dict("records")


def _fetch_seo_universe_raw() -> list[dict]:
    """ak.stock_qbzf_em() 全市场已实施增发明细 → records。空 → 抛 ValueError。"""
    import akshare as ak
    df = ak.stock_qbzf_em()
    if df is None or df.empty:
        raise ValueError("stock_qbzf_em 返回空")
    return df.to_dict("records")


def _fetch_cb_detail_raw(bond_code: str) -> dict:
    """ak.bond_zh_cov_info(基本信息) 单债详情 → dict。空 → 抛 ValueError。"""
    import akshare as ak
    df = ak.bond_zh_cov_info(symbol=str(bond_code), indicator="基本信息")
    if df is None or df.empty:
        raise ValueError(f"bond_zh_cov_info({bond_code}) 返回空")
    return df.iloc[0].to_dict()


def _fetch_unlock_sina_raw(code: str) -> list[dict]:
    """ak.stock_restricted_release_queue_sina 解禁队列(**带公告日期**)→ records。

    该票无解禁安排是**正常业务态**(如从未有限售股)→ 返回 [](不抛)。
    """
    import akshare as ak
    df = ak.stock_restricted_release_queue_sina(symbol=str(code))
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def _fetch_unlock_em_raw(code: str) -> list[dict]:
    """ak.stock_restricted_release_queue_em 解禁队列(字段丰富,**无公告日期**)→ records。"""
    import akshare as ak
    df = ak.stock_restricted_release_queue_em(symbol=str(code))
    if df is None or df.empty:
        return []
    return df.to_dict("records")


def _fetch_plan_disclosures_raw(code: str, start_date: str, end_date: str) -> list[dict]:
    """巨潮公告标题检索(定增相关关键词)→ records(含 公告标题/公告时间)。

    多关键词分别检索后按 (标题, 时间) 去重;单关键词失败不影响其余(逐词降级)。
    全部关键词都失败 → 抛最后一次异常(交上层标源不可得)。
    """
    import akshare as ak
    seen: set = set()
    out: list[dict] = []
    last: Exception | None = None
    hit_any = False
    for kw in _PLAN_KEYWORDS:
        try:
            df = ak.stock_zh_a_disclosure_report_cninfo(
                symbol=str(code), market="沪深京", keyword=kw,
                start_date=start_date, end_date=end_date)
            hit_any = True
        except Exception as exc:                    # noqa: BLE001
            last = exc
            continue
        if df is None or df.empty:
            continue
        for r in df.to_dict("records"):
            key = (_strip_em(r.get("公告标题")), _norm_date(r.get("公告时间")))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
        time.sleep(_FETCH_SLEEP)
    if not hit_any and last is not None:
        raise last
    return out


# ————————————————————————————————————————————————
# 层 2:归一(纯函数,输入原始 records,输出带「披露日」的记录列表)
# ————————————————————————————————————————————————
def normalize_cb(uni_row: dict, detail: dict | None) -> dict:
    """归一单只可转债。`披露日` = min(申购日期, 上市时间/LISTING_DATE) —— 最早的公开锚点。

    状态由消费侧按 as_of 判(见 `_cb_state_asof`);此处只搬运时点与条款。
    """
    bond_code = str(uni_row.get("债券代码") or "").strip()
    listing = _norm_date(uni_row.get("上市时间"))
    apply_d = _norm_date(uni_row.get("申购日期"))
    d = detail or {}
    listing = _norm_date(d.get("LISTING_DATE")) or listing
    anchors = [x for x in (apply_d, listing) if x]
    return {
        "债券代码": bond_code,
        "债券简称": str(uni_row.get("债券简称") or d.get("SECURITY_NAME_ABBR") or "") or None,
        "正股代码": _norm_code(uni_row.get("正股代码") or d.get("CONVERT_STOCK_CODE")),
        "披露日": min(anchors) if anchors else None,
        "申购日期": apply_d,
        "上市日": listing,
        "到期日": _norm_date(d.get("EXPIRE_DATE")),
        "摘牌日": _norm_date(d.get("DELIST_DATE")),
        "转股起始日": _norm_date(d.get("TRANSFER_START_DATE")),
        "转股结束日": _norm_date(d.get("TRANSFER_END_DATE")),
        "发行规模_亿": _to_float(d.get("ACTUAL_ISSUE_SCALE")) or _to_float(uni_row.get("发行规模")),
        "转股价": _to_float(d.get("TRANSFER_PRICE")) or _to_float(uni_row.get("转股价")),
        "初始转股价": _to_float(d.get("INITIAL_TRANSFER_PRICE")),
        "强赎触发价": _to_float(d.get("REDEEM_TRIG_PRICE")),
        "回售触发价": _to_float(d.get("RESALE_TRIG_PRICE")),
        "信用评级": str(uni_row.get("信用评级") or d.get("RATING") or "") or None,
        "债现价": _to_float(d.get("CURRENT_BOND_PRICE")) or _to_float(uni_row.get("债现价")),
        "转股溢价率": _to_float(d.get("TRANSFER_PREMIUM_RATIO")) or _to_float(uni_row.get("转股溢价率")),
        "有条款_强赎": (str(d.get("IS_REDEEM") or "") == "是") if d else None,
        "有条款_回售": (str(d.get("IS_SELLBACK") or "") == "是") if d else None,
        "详情已取": bool(d),
    }


def normalize_seo_done(row: dict) -> dict:
    """归一单条**已实施**增发。`披露日` = 发行日期(发行即公告);解锁日 = 增发上市日 + 锁定期。"""
    issue_d = _norm_date(row.get("发行日期"))
    list_d = _norm_date(row.get("增发上市日期"))
    months = _lock_months(row.get("锁定期"))
    return {
        "阶段": "已实施",
        "发行方式": str(row.get("发行方式") or "") or None,
        "披露日": issue_d,
        "发行日期": issue_d,
        "上市日期": list_d,
        "锁定期": str(row.get("锁定期") or "") or None,
        "锁定月数": months,
        "解锁日": _add_months(list_d, months),
        "发行价格": _to_float(row.get("发行价格")),
        "发行总数": _to_float(row.get("发行总数")),
        "标题": None,
    }


def plan_stage_signal(title: str | None) -> str | None:
    """定增公告标题 → 阶段信号:"终止" / "完成" / "进行";无阶段词 → None。

    **判定顺序是语义的一部分**:已实施 → 终止 → 进行。
    · 为什么 LIVE 排最后:发行完成后的配套文件(合规性审核报告 / 验资报告 / 三方监管协议 /
      股本变动)标题里普遍带「审核」「方案」等在途词,先判 LIVE 会把**已经发完**的定增
      当成在途(603161 实测)。
    · 为什么「已实施」排在「终止」之前:**已实施的证据强于终止词**。发行落地后的收尾公告
      会出现「募集资金专户三方监管协议**终止**」这类标题——那是协议终止、不是发行终止;
      反过来,一笔真被撤回的定增**永远不会**产出发行结果/验资报告/股本变动这类文件。
      若先判终止,会把已经发完的定增说成「摊薄不会发生」,方向同样是反的。
    """
    t = str(title or "")
    if not t:
        return None
    if _PLAN_STAGE_DONE.search(t):
        return "完成"
    if _PLAN_STAGE_TERMINATED.search(t):
        return "终止"
    if _PLAN_STAGE_LIVE.search(t):
        return "进行"
    return None


def normalize_seo_plan(row: dict) -> dict | None:
    """归一单条定增**过程公告**(标题级证据)。非定增类 / 无阶段词 → None(过滤掉)。

    `披露日` = 公告时间(巨潮原生披露日,天然满足防未来函数闸门)。

    ⚠ 与旧版的关键差别:**终局公告(发行结果/终止)不再直接丢弃**,而是保留成
    `阶段="终态公告"` + `阶段信号∈{完成,终止}` 的记录。理由:要判断「这笔定增到底还在不在推进」
    必须看得见它的**终局证据**;丢掉终局公告就只剩一串在途公告,于是**已完成的定增被永久判为推进中**。
    终局公告留下后,`aggregate_plan_rounds` 才能按笔关帐(且关帐要过 as-of 闸门——
    在终局公告披露之前的时点,该笔确实还算推进中)。
    """
    title = _strip_em(row.get("公告标题"))
    if not title or not _PLAN_TITLE_POS.search(title) or _PLAN_TITLE_NEG.search(title):
        return None
    sig = plan_stage_signal(title)
    if sig is None:
        return None
    return {
        "阶段": _SIGNAL_TO_STAGE[sig],
        "阶段信号": sig,
        "发行方式": "定向增发",
        "披露日": _norm_date(row.get("公告时间")),
        "发行日期": None, "上市日期": None, "锁定期": None, "锁定月数": None,
        "解锁日": None, "发行价格": None, "发行总数": None,
        "标题": title,
        "链接": str(row.get("公告链接") or "") or None,
    }


def aggregate_plan_rounds(plan_recs: list[dict] | None,
                          done_recs: list[dict] | None = None) -> list[dict]:
    """把「同一笔定增的多条过程公告」聚合成**笔**。返回每笔一条摘要(按起始披露日升序)。

    **为什么必须聚合**:一笔定增从预案到发行结果会出十几条公告(预案/受理/问询回复/同意注册/
    募集说明书/发行结果/验资…)。按公告**条数**计数会得出「推进中 = 12」这种荒谬结果
    (603161 实测),而真相是**同一笔、且已经发完**。`推进中` 必须回答「有几**笔**在推进」。

    **状态机**(按披露日时序):
      · "进行"信号 → 没有开着的笔就开一笔,否则并入当前笔(**不新开**);
      · "完成"/"终止"信号 → 关掉当前笔(状态置 已实施 / 已终止);若此前没开着的笔,
        就补记一笔(只看到尾巴:预案早于回看窗口 540 天);
      · 已实施(qbzf 结构化)记录的发行日期 ≥ 某笔起始披露日 → 那笔也判**已实施**
        (兜住「发行结果公告标题没被正则命中、但结构化源已收录发行」的情况)。
    同一时点最多保持**一笔**开着(A股实务:同一时点不会有两笔并行的向特定对象发行);
    因此本函数的 `推进中` 笔数天然 ∈ {0,1},这是有意的保守口径,不是漏算。

    调用方须先做 as-of 闸门:传进来的记录都应是「as_of 当天已披露」的,
    否则会用**未来**的终局公告去关掉一笔在当时确实还在推进的定增(未来函数)。

    副作用(有意):给每条入参公告写回 `笔序号` / `笔状态`,让直读 `明细` 的消费方
    也能看见「这条在途公告属于哪一笔、那笔现在是什么状态」——否则单看公告级
    `阶段="推进中"` 会重犯同一个反向误判。
    """
    anns = sorted([r for r in (plan_recs or []) if r.get("披露日")],
                  key=lambda r: (str(r["披露日"]), str(r.get("标题") or "")))
    rounds: list[dict] = []
    cur: dict | None = None
    for a in anns:
        sig = a.get("阶段信号") or ("进行" if a.get("阶段") == _STAGE_ANN_LIVE else None)
        d = str(a["披露日"])
        if cur is None:
            prev = rounds[-1] if rounds else None
            tail_of_prev = (
                sig in ("完成", "终止") and prev is not None
                and prev["状态"] in (_ROUND_DONE, _ROUND_TERM)
                and _gap_days(str(prev["终结披露日"] or prev["最新披露日"]), d)
                <= _ROUND_TAIL_DAYS)
            if tail_of_prev:
                cur = prev              # 收尾文件串:归入上一笔,**不是新的一笔**
            else:
                cur = {"状态": _ROUND_LIVE, "起始披露日": d, "首个标题": a.get("标题"),
                       "最新披露日": d, "最新标题": a.get("标题"), "公告条数": 0,
                       "终结披露日": None, "终结标题": None, "终结依据": None}
                rounds.append(cur)
        cur["公告条数"] += 1
        cur["最新披露日"], cur["最新标题"] = d, a.get("标题")
        a["笔序号"] = rounds.index(cur) + 1          # 1-based
        if sig in ("完成", "终止"):
            new_state = _ROUND_DONE if sig == "完成" else _ROUND_TERM
            # 「已实施」的证据强于「已终止」(理由同 plan_stage_signal):一笔股份已经发出去的定增,
            # 不会因为后面又来一条「募集资金专户/监管协议终止」这类收尾公告就变回「没发出」。
            cur["状态"] = (_ROUND_DONE if _ROUND_DONE in (cur["状态"], new_state)
                           else new_state)
            cur["终结披露日"], cur["终结标题"] = d, a.get("标题")
            cur["终结依据"] = "公告标题"
            cur = None                              # 关帐:后续「进行」信号属于新的一笔
    # 结构化已实施增发也能关帐(发行日期落在该笔开始之后 → 这笔就是它发出去的那笔)
    for s in sorted(done_recs or [], key=lambda x: str(x.get("披露日") or "")):
        d = s.get("发行日期") or s.get("披露日")
        if not d:
            continue
        for r in rounds:
            if r["状态"] == _ROUND_LIVE and str(d) >= str(r["起始披露日"]):
                r.update({"状态": _ROUND_DONE, "终结披露日": str(d), "终结标题": None,
                          "终结依据": "已实施增发(qbzf 发行日期)"})
    for i, r in enumerate(rounds):
        r["序号"] = i + 1
    for a in anns:                                  # 笔状态回写(必须在关帐全部结束后)
        i = a.get("笔序号")
        a["笔状态"] = rounds[i - 1]["状态"] if isinstance(i, int) and 0 < i <= len(rounds) else None
    return rounds


def normalize_unlocks(sina_rows: list[dict], em_rows: list[dict]) -> tuple[list[dict], dict]:
    """归一解禁时间表:**sina 为主**(带公告日期=披露日),em 就近匹配做字段增强。

    返回 (记录列表, 统计)。统计含 `em_未匹配`(em 有、sina 没有 → **无披露日,不予采纳**,
    显式计数而不是静默塞进去)。

    ⚠ 前瞻字段处理:em 的 `解禁后20日涨跌幅` = 前瞻收益 → **丢弃**(未来函数);
      `解禁前一交易日收盘价` / `实际解禁数量市值` 对未来行是按采集日价折算 → 改名 + 标口径。
    """
    em_norm = []
    for r in em_rows or []:
        d = _norm_date(r.get("解禁时间"))
        if not d:
            continue
        em_norm.append({
            "_d": d,
            "解禁数量_股": _to_float(r.get("实际解禁数量")) or _to_float(r.get("解禁数量")),
            "未解禁数量_股": _to_float(r.get("未解禁数量")),
            "占总市值_pct": _to_float(r.get("占总市值比例")),
            "占流通市值_pct": _to_float(r.get("占流通市值比例")),
            "限售股类型": str(r.get("限售股类型") or "") or None,
            "折算市值_按采集日价_元": _to_float(r.get("实际解禁数量市值")),
            "折算参考价": _to_float(r.get("解禁前一交易日收盘价")),
            "市值口径": "采集日价折算",
            # 有意不搬运:解禁后20日涨跌幅(前瞻收益=未来函数)、解禁前20日涨跌幅(区间跨解禁日)
        })

    used: set[int] = set()
    out: list[dict] = []
    for r in sina_rows or []:
        d = _norm_date(r.get("解禁日期"))
        if not d:
            continue
        rec = {
            "解禁日": d,
            "披露日": _norm_date(r.get("公告日期")),
            "解禁数量_股": (_to_float(r.get("解禁数量")) or 0) * 1e4 or None,   # sina 单位:万股
            "解禁市值_亿_sina": _to_float(r.get("解禁股流通市值")),
            "上市批次": _to_float(r.get("上市批次")),
            "占总市值_pct": None, "占流通市值_pct": None, "限售股类型": None,
            "未解禁数量_股": None, "折算市值_按采集日价_元": None,
            "折算参考价": None, "市值口径": None, "增强源": None,
        }
        best_i, best_gap = None, None
        for i, e in enumerate(em_norm):
            if i in used:
                continue
            raw_gap = _days_between(d, e["_d"])
            if raw_gap is None:                     # 不可解析 → 不匹配(注意 gap==0 是完全匹配,不是「无」)
                continue
            gap = abs(raw_gap)
            if gap <= _UNLOCK_MATCH_TOL_DAYS and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap
        if best_i is not None:
            used.add(best_i)
            e = em_norm[best_i]
            rec.update({k: v for k, v in e.items() if k != "_d"})
            rec["解禁日_em"] = e["_d"]
            rec["增强源"] = "em"
        out.append(rec)

    out.sort(key=lambda x: x["解禁日"])
    return out, {"sina_条数": len(out), "em_条数": len(em_norm),
                 "em_未匹配": len(em_norm) - len(used)}


# ————————————————————————————————————————————————
# 层 3:市场级共享名单(一次运行拉一次,进程 memo + 落盘复用)
# ————————————————————————————————————————————————
def load_market_lists(force: bool = False) -> dict:
    """取市场级共享名单 {cb:[...], seo:[...], 源状态:{...}, 降级:[...]}。

    优先进程 memo → 再看落盘缓存(≤MARKET_STALE_DAYS 新鲜)→ 都无才真拉。
    某张表拉失败 → 该维度写 "源不可得" + 进 降级[],另一张表照常(优雅降级)。
    """
    if not force and "market" in _MEM:
        return _MEM["market"]                       # type: ignore[return-value]
    if not force and not store.is_stale(_KIND, _MARKET_CODE, MARKET_STALE_DAYS):
        try:
            payload = store.get_raw(_KIND, _MARKET_CODE)
            if isinstance(payload, dict) and payload.get("cb"):
                _MEM["market"] = payload
                logger.info("存量融资:市场级名单命中缓存(cb=%d, seo=%d)",
                            len(payload.get("cb") or []), len(payload.get("seo") or []))
                return payload
        except FileNotFoundError:
            pass

    status: dict[str, str] = {}
    degraded: list[str] = []
    try:
        cb = retry_call(_fetch_cb_universe_raw, label="可转债一览")
        status["可转债一览"] = "ok"
    except Exception as exc:                        # noqa: BLE001
        cb, status["可转债一览"] = [], "源不可得"
        degraded.append(f"bond_zh_cov 失败: {str(exc)[:120]}")
        logger.warning("存量融资:可转债一览拉取失败(降级): %s", str(exc)[:120])
    try:
        seo = retry_call(_fetch_seo_universe_raw, label="已实施增发")
        status["已实施增发"] = "ok"
    except Exception as exc:                        # noqa: BLE001
        seo, status["已实施增发"] = [], "源不可得"
        degraded.append(f"stock_qbzf_em 失败: {str(exc)[:120]}")
        logger.warning("存量融资:已实施增发拉取失败(降级): %s", str(exc)[:120])

    # 按正股代码建索引(只留必要列,避免落盘膨胀)
    cb_by_code: dict[str, list[dict]] = {}
    for r in cb:
        c = _norm_code(r.get("正股代码"))
        if c:
            cb_by_code.setdefault(c, []).append(
                {k: (str(v) if k in ("申购日期", "上市时间") else v)
                 for k, v in r.items()
                 if k in ("债券代码", "债券简称", "申购日期", "上市时间", "正股代码",
                          "转股价", "债现价", "转股溢价率", "发行规模", "信用评级")})
    seo_by_code: dict[str, list[dict]] = {}
    for r in seo:
        c = _norm_code(r.get("股票代码"))
        if c:
            seo_by_code.setdefault(c, []).append(
                {k: (str(v) if k in ("发行日期", "增发上市日期") else v)
                 for k, v in r.items()
                 if k in ("发行方式", "发行总数", "发行价格", "发行日期",
                          "增发上市日期", "锁定期")})
    # 只留每票最近 N 次已实施增发:更早的锁定期必已到期(最长 3 年),对「锁定中」判断无贡献,
    # 却让市场级落盘膨胀数倍(全市场 5800+ 条)。按发行日期倒序截断。
    for c, rows in seo_by_code.items():
        rows.sort(key=lambda x: str(_norm_date(x.get("发行日期")) or ""), reverse=True)
        seo_by_code[c] = rows[:_SEO_KEEP_PER_CODE]

    payload = {"cb": cb_by_code, "seo": seo_by_code, "源状态": status, "降级": degraded}
    try:
        store.put_raw(_KIND, _MARKET_CODE, payload,
                      meta={"source": "akshare", "kind_detail": "存量融资市场级名单",
                            "rows": len(cb_by_code)})
    except Exception as exc:                        # noqa: BLE001
        logger.warning("存量融资:市场级名单落盘失败(不阻断): %s", str(exc)[:120])
    _MEM["market"] = payload
    logger.info("存量融资:市场级名单已取(有转债正股 %d 只 / 有增发正股 %d 只)",
                len(cb_by_code), len(seo_by_code))
    return payload


def reset_market_cache() -> None:
    """清进程内 memo(单测/长跑重刷用)。"""
    _MEM.pop("market", None)


# ————————————————————————————————————————————————
# 层 4:逐票采集(落盘)
# ————————————————————————————————————————————————
def fetch_one(code: str, market: dict | None = None) -> dict:
    """采单票存量融资 payload(**不做 as_of 过滤**,过滤在 summarize_asof)。

    payload 结构:
      {code, 可转债:[...], 定增:[...], 解禁:[...], 解禁统计:{...},
       源状态:{可转债,定增,解禁}, 降级:[...]}
    每条明细都带 `披露日`(可能为 None → 消费侧闸门会剔并计数)。
    """
    code = _norm_code(code)
    mk = market if market is not None else load_market_lists()
    status: dict[str, str] = {}
    degraded: list[str] = []

    # —— 可转债:市场名单命中 → 逐债拉详情(条款/到期/转股起始)——
    cb_rows = (mk.get("cb") or {}).get(code) or []
    cbs: list[dict] = []
    if (mk.get("源状态") or {}).get("可转债一览") != "ok":
        status["可转债"] = "源不可得"
        degraded.append("可转债一览源不可得,无法判定有无存续转债")
    else:
        status["可转债"] = "ok" if cb_rows else "ok_无转债"
        for r in cb_rows:
            bond_code = str(r.get("债券代码") or "").strip()
            detail = None
            if bond_code:
                try:
                    detail = retry_call(_fetch_cb_detail_raw, bond_code,
                                        label=f"转债详情{bond_code}")
                except Exception as exc:            # noqa: BLE001
                    degraded.append(f"转债 {bond_code} 详情缺失: {str(exc)[:100]}")
                    status["可转债"] = "ok_详情降级"
                    logger.warning("存量融资 %s 转债 %s 详情失败(降级为一览字段): %s",
                                   code, bond_code, str(exc)[:100])
                time.sleep(_FETCH_SLEEP)
            cbs.append(normalize_cb(r, detail))

    # —— 定增:已实施(市场名单)+ 推进中(公告标题检索)——
    seo: list[dict] = []
    if (mk.get("源状态") or {}).get("已实施增发") != "ok":
        status["定增"] = "源不可得"
        degraded.append("已实施增发源不可得")
    else:
        status["定增"] = "qbzf"
        for r in (mk.get("seo") or {}).get(code) or []:
            seo.append(normalize_seo_done(r))
    end = _date.today()
    start = end - timedelta(days=_PLAN_LOOKBACK_DAYS)
    try:
        plans = retry_call(_fetch_plan_disclosures_raw, code,
                           start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
                           label=f"定增公告{code}")
        for r in plans:
            rec = normalize_seo_plan(r)
            if rec:
                seo.append(rec)
        status["定增"] = (status.get("定增", "") + "+公告检索").lstrip("+")
    except Exception as exc:                        # noqa: BLE001
        degraded.append(f"定增公告检索失败(推进中定增不可判): {str(exc)[:100]}")
        status["定增"] = (status.get("定增", "") + "+公告检索不可得").lstrip("+")
        logger.warning("存量融资 %s 定增公告检索失败(降级): %s", code, str(exc)[:100])
    time.sleep(_FETCH_SLEEP)

    # —— 解禁:sina 主(带公告日期)+ em 增强 ——
    sina_rows, em_rows = [], []
    sina_ok = em_ok = False
    try:
        sina_rows = retry_call(_fetch_unlock_sina_raw, code, label=f"解禁sina{code}")
        sina_ok = True
    except Exception as exc:                        # noqa: BLE001
        degraded.append(f"解禁 sina 失败(无披露日锚点→解禁维度不可判): {str(exc)[:100]}")
        logger.warning("存量融资 %s 解禁 sina 失败(降级): %s", code, str(exc)[:100])
    time.sleep(_FETCH_SLEEP)
    try:
        em_rows = retry_call(_fetch_unlock_em_raw, code, label=f"解禁em{code}")
        em_ok = True
    except Exception as exc:                        # noqa: BLE001
        degraded.append(f"解禁 em 增强失败(占比/类型缺失): {str(exc)[:100]}")
        logger.warning("存量融资 %s 解禁 em 失败(仅降级增强字段): %s", code, str(exc)[:100])
    unlocks, ustat = normalize_unlocks(sina_rows, em_rows)
    if sina_ok and em_ok:
        status["解禁"] = "sina+em"
    elif sina_ok:
        status["解禁"] = "sina_only"
    else:
        status["解禁"] = "源不可得"                  # em-only 无披露日 → 不予采纳
        if em_ok and ustat.get("em_条数"):
            degraded.append(f"em 有 {ustat['em_条数']} 条解禁但无披露日锚点,全部剔除(防未来函数)")

    return {
        "code": code,
        "可转债": cbs,
        "定增": seo,
        "解禁": unlocks,
        "解禁统计": ustat,
        "源状态": status,
        "降级": degraded,
    }


def fetch_financing(codes: list[str], force: bool = False) -> dict[str, dict]:
    """批量采集存量融资并按票落盘。**skip-if-cached**(≤FINANCING_STALE_DAYS 不重采)。

    单票失败 → log + 跳过,不中断整批(优雅降级)。返回 {code: payload}(含跳过的读回值)。
    """
    from tools.config import settings
    settings.ensure_dirs()

    out: dict[str, dict] = {}
    mk = load_market_lists(force=force)
    skipped = 0
    for code in codes or []:
        c = _norm_code(code)
        if not c:
            continue
        if not force and not store.is_stale(_KIND, c, FINANCING_STALE_DAYS):
            try:
                out[c] = store.get_raw(_KIND, c)     # 幂等:命中新鲜缓存直接复用
                skipped += 1
                continue
            except FileNotFoundError:
                pass
        try:
            payload = fetch_one(c, market=mk)
            store.put_raw(_KIND, c, payload,
                          meta={"source": "akshare+cninfo",
                                "kind_detail": "存量融资与解禁"})
            out[c] = payload
        except Exception as exc:                     # noqa: BLE001
            logger.error("存量融资 %s 采集失败(跳过): %s", c, str(exc)[:150])
    logger.info("存量融资采集完成:%d 票(其中缓存命中跳过 %d)", len(out), skipped)
    return out


# ————————————————————————————————————————————————
# 层 5:消费(as-of 读取 + 防未来函数闸门 + 分析师直读摘要)
# ————————————————————————————————————————————————
def load_financing(code: str, as_of: str | None = None) -> dict:
    """读单票存量融资 raw。缺失抛 FileNotFoundError。

    as_of=None → 全局最新分区(当日跑);as_of 指定 → date-pin 到 **≤as_of** 的最新分区
    (`get_raw_resolved`,绝不读未来分区)。分区内的**逐条披露日闸门**在 summarize_asof。
    """
    code = _norm_code(code)
    if as_of is None:
        return store.get_raw(_KIND, code)
    payload, _resolved, _fetched = store.get_raw_resolved(_KIND, code, date=as_of)
    return payload


def _visible(rec: dict, as_of: str | None) -> bool:
    """**防未来函数唯一闸门**:该条记录在 as_of 当天是否已公开披露。

    无披露日 → 不可见(宁缺勿滥,不猜);披露日 > as_of → 不可见(未来披露)。
    as_of=None(生产当日)→ 只要求有披露日。
    """
    d = rec.get("披露日")
    if not d:
        return False
    return True if as_of is None else str(d) <= str(as_of)


def _cb_state_asof(cb: dict, as_of: str | None) -> str:
    """as_of 当天该转债的状态:未上市 / 存续 / 已摘牌 / 已到期。"""
    ref = str(as_of) if as_of else _date.today().isoformat()
    listing, delist, expire = cb.get("上市日"), cb.get("摘牌日"), cb.get("到期日")
    if delist and str(delist) <= ref:
        return "已摘牌"
    if expire and str(expire) <= ref:
        return "已到期"
    if not listing or str(listing) > ref:
        return "未上市"
    return "存续"


def summarize_asof(payload: dict | None, as_of: str | None = None,
                   总股本: float | None = None) -> dict | None:
    """把 raw payload 收敛成分析师直读摘要 —— record 里 `financing` 块的内容。

    **只用「as_of 及之前已披露」的记录**(逐条过 `_visible`),被剔除的计入 `剔除`(显式,不静默)。
    `总股本`(股)给了才算摊薄/占比百分比,缺则相应字段 None(不猜)。

    产出的 `固定一问` 就是 §5 要求的那一问:有无存续可转债 / 在推进的定增 / 临近解禁。
    """
    if not isinstance(payload, dict):
        return None

    dropped = {"披露日晚于as_of": 0, "无披露日": 0}

    def _keep(rows):
        kept = []
        for r in rows or []:
            if _visible(r, as_of):
                kept.append(r)
            elif not r.get("披露日"):
                dropped["无披露日"] += 1
            else:
                dropped["披露日晚于as_of"] += 1
        return kept

    cbs = _keep(payload.get("可转债"))
    seos = _keep(payload.get("定增"))
    unlocks = _keep(payload.get("解禁"))
    ref = str(as_of) if as_of else _date.today().isoformat()

    # —— 可转债 ——
    live_cbs = []
    for cb in cbs:
        c = dict(cb)
        c["状态"] = _cb_state_asof(cb, as_of)
        c["已进入转股期"] = (bool(cb.get("转股起始日")) and str(cb["转股起始日"]) <= ref)
        scale, cp = cb.get("发行规模_亿"), cb.get("转股价")
        c["转股潜在股数"] = (scale * 1e8 / cp) if (scale and cp) else None
        c["潜在摊薄_pct"] = (round(c["转股潜在股数"] / 总股本 * 100, 2)
                            if (c["转股潜在股数"] and 总股本) else None)
        live_cbs.append(c)
    outstanding = [c for c in live_cbs if c["状态"] in ("存续", "未上市")]
    cb_block = {
        "只数": len(outstanding),
        "存续规模_亿": round(sum(c.get("发行规模_亿") or 0 for c in outstanding), 4) or None,
        "潜在摊薄_pct": (round(sum(c["潜在摊薄_pct"] or 0 for c in outstanding), 2)
                        if any(c.get("潜在摊薄_pct") for c in outstanding) else None),
        "明细": live_cbs,
    }

    # —— 定增 ——
    # **按笔聚合,不按公告条数**:同一笔定增的十几条过程公告先聚合成「笔」,`推进中` 报笔数。
    # (旧口径按条数,603161 一笔已完成的定增被报成「推进中 12」,消费侧据此打「再融资摊薄压力」
    #  标签会完全反向——实质是摊薄**已经发生**、锁定 36 个月。)
    done_seos = [s for s in seos if s.get("阶段") == "已实施"]
    plan_anns = [s for s in seos if s.get("阶段") in (_STAGE_ANN_LIVE, _STAGE_ANN_FINAL)]
    rounds = aggregate_plan_rounds(plan_anns, done_seos)
    live_rounds = [r for r in rounds if r["状态"] == _ROUND_LIVE]
    locked = [s for s in done_seos if s.get("解锁日") and str(s["解锁日"]) > ref]
    seo_block = {
        "推进中": len(live_rounds),                  # ← **笔数**(不是公告条数)
        "已实施_锁定中": len(locked),                # 结构化已实施 + 锁定期未过(摊薄已发生)
        "笔数": {_ROUND_LIVE: len(live_rounds),
                 _ROUND_DONE: len([r for r in rounds if r["状态"] == _ROUND_DONE]),
                 _ROUND_TERM: len([r for r in rounds if r["状态"] == _ROUND_TERM])},
        "推进中公告条数": sum(r["公告条数"] for r in live_rounds),
        "计数口径": ("推进中/已实施/已终止 = 定增**笔数**(同一笔的多条过程公告已按时序聚合);"
                    "公告条数见 推进中公告条数 与 笔[].公告条数。"
                    "已实施=股份已发出(摊薄已发生);已终止=股份从未发出(摊薄不会发生),二者不同桶"),
        "最近推进中披露日": max((r["最新披露日"] for r in live_rounds), default=None),
        "笔": rounds,
        "明细": sorted(seos, key=lambda x: str(x.get("披露日") or ""), reverse=True)[:12],
    }

    # —— 解禁 ——
    # em 增强缺失时(em 队列不含该批次,如科创板部分批次),用 解禁数量/总股本 本地折算
    # 「占总股本_pct」补上**规模**维度(与 em 的「占流通市值_pct」口径不同,故另起字段名不混淆)。
    for u in unlocks:
        n = u.get("解禁数量_股")
        u["占总股本_pct"] = (round(n / 总股本 * 100, 4) if (n and 总股本) else None)
    future = [u for u in unlocks if str(u["解禁日"]) > ref]
    near = [u for u in future if _gap_days(ref, u["解禁日"]) <= NEAR_UNLOCK_DAYS]
    nxt = future[0] if future else None
    near_ratio = sum((u.get("占流通市值_pct") if u.get("占流通市值_pct") is not None
                      else (u.get("占总股本_pct") or 0)) for u in near)
    unlock_block = {
        "未来次数": len(future),
        f"未来{NEAR_UNLOCK_DAYS}日次数": len(near),
        f"未来{NEAR_UNLOCK_DAYS}日占流通_pct": round(near_ratio, 4) if near_ratio else None,
        "占比口径": "优先 em 占流通市值_pct;em 缺该批次时退回 占总股本_pct(本地按总股本折算)",
        "下一次": ({"解禁日": nxt["解禁日"], "距今日": _days_between(ref, nxt["解禁日"]),
                   "解禁数量_股": nxt.get("解禁数量_股"),
                   "占流通市值_pct": nxt.get("占流通市值_pct"),
                   "占总市值_pct": nxt.get("占总市值_pct"),
                   "占总股本_pct": nxt.get("占总股本_pct"),
                   "限售股类型": nxt.get("限售股类型"),
                   "披露日": nxt.get("披露日")} if nxt else None),
        "明细": future[:12],
    }

    # —— 约束提示(分析师直读的「约束」维度)——
    notes: list[str] = []
    for c in outstanding:
        nm = c.get("债券简称") or c.get("债券代码")
        if not c["已进入转股期"] and c.get("转股起始日"):
            notes.append(f"{nm} 未进入转股期({c['转股起始日']} 起),摊薄暂不发生")
        elif c["已进入转股期"]:
            notes.append(f"{nm} 已进入转股期,转股价 {c.get('转股价')},摊薄可随时发生")
        if c.get("强赎触发价"):
            notes.append(f"{nm} 强赎触发价 {c['强赎触发价']}(正股价触及即可能促转股)")
        if c.get("回售触发价"):
            notes.append(f"{nm} 回售触发价 {c['回售触发价']}(正股跌破构成现金压力)")
    if nxt and _gap_days(ref, nxt["解禁日"]) <= NEAR_UNLOCK_DAYS:
        scale = (f"占流通 {round(nxt['占流通市值_pct'], 3)}%"
                 if nxt.get("占流通市值_pct") is not None else
                 (f"占总股本 {nxt['占总股本_pct']}%" if nxt.get("占总股本_pct") is not None
                  else f"{nxt.get('解禁数量_股')} 股(占比源缺)"))
        notes.append(f"{nxt['解禁日']} 有解禁({nxt.get('限售股类型') or '类型未知'}),{scale}")
    # 定增:把「摊薄已经发生」与「摊薄还没发生」明确写成两句不同的话——
    # 这正是本轮 bug 的消费侧后果所在(把已完成的定增当成「待摊薄压力」会完全反向)。
    for r in live_rounds:
        notes.append(f"定增在推进(起 {r['起始披露日']},最新 {r['最新披露日']}:"
                     f"{(r.get('最新标题') or '')[:40]}),摊薄尚未发生")
    for s in locked:
        notes.append(f"定增已实施({s.get('发行日期')} 发行,{s.get('锁定期') or '锁定期未知'}),"
                     f"锁定至 {s.get('解锁日')} —— 摊薄**已发生**,不是待摊薄压力")
    for r in rounds:
        if r["状态"] == _ROUND_DONE and not locked:
            notes.append(f"定增已发行落地(依据:{r['终结依据']},{r['终结披露日']}),"
                         f"摊薄已发生;结构化锁定期明细暂缺")
        elif r["状态"] == _ROUND_TERM:
            notes.append(f"定增已终止/撤回({r['终结披露日']}),该笔摊薄不会发生")

    return {
        "as_of": as_of,
        "固定一问": {
            "有存续可转债": bool(outstanding),
            "有推进中定增": bool(live_rounds),        # 笔级「推进中」才算 true(已终止/已实施都不算)
            f"有临近解禁_{NEAR_UNLOCK_DAYS}日": bool(near),
        },
        "可转债": cb_block,
        "定增": seo_block,
        "解禁": unlock_block,
        "约束提示": notes,
        "剔除": dropped,
        "源状态": payload.get("源状态") or {},
        "降级": payload.get("降级") or [],
    }


def build_financing_block(code: str, as_of: str | None = None,
                          总股本: float | None = None) -> dict | None:
    """record 用的一站式入口:读缓存 → as-of 闸门 → 摘要。**无缓存 → None + 显式 WARN**。

    ⚠ 本函数只**读缓存**(`load_financing`),不采集:采集在批量入口 `fetch_financing`
    (run.py 调度)。因此对「从未采过的票」它必然拿不到数据。
    **为什么不在这里触发采集**:① 本函数跑在 record 序列化路径上(逐票),内嵌网络 I/O 会把
    分析层变成采集层、逐票串行拉取;② 回测按历史 as_of 构记录时,采集只会拉到**今天**的数据,
    等于给历史时点注入未来信息(防未来函数硬红线);③ 采集失败会阻断记录生成。
    **为什么也不返回「带 源不可得 标记的空块」**:`serialize.py` 用 `bool(financing_block)`
    定 `provenance.financing`,非空 dict 是真值 → 会从「静默缺失」升级成「明确谎报有数据」。
    故本轮保持返回假值(None),只把**静默**变成**有声**:WARN 日志 + 模块内降级台账
    (`missing_financing()`),批量跑完可直接读出「哪些票的 financing 维度其实没采」。
    provenance 要真正如实,需 serialize 侧改成按数据存在性判定(见完工回执,归 B 线)。
    """
    code = _norm_code(code)
    try:
        payload = load_financing(code, as_of=as_of)
    except FileNotFoundError:
        rec = {"code": code, "as_of": as_of, "原因": "无 ≤as_of 的 equity_financing 缓存分区",
               "补救": "跑 fetch_financing([code])(run.py 的存量融资采集)后重建记录"}
        _MISSING_CACHE.append(rec)
        logger.warning("存量融资 %s:无缓存(as_of=%s)→ financing 维度整块缺失(**非「确实没有」**);"
                       "先跑 fetch_financing 再重建记录", code, as_of)
        return None
    return summarize_asof(payload, as_of=as_of, 总股本=总股本)


def missing_financing() -> list[dict]:
    """本进程内 `build_financing_block` 因**无缓存**而返回空的台账(显式降级,不静默)。

    批量跑完读它就知道「有几只票的 financing 维度其实是没采到、而不是确实没有」。
    """
    return list(_MISSING_CACHE)


def reset_missing_financing() -> None:
    """清空无缓存降级台账(单测/长跑分段统计用)。"""
    _MISSING_CACHE.clear()
