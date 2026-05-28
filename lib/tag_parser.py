"""
Tag 文字 → 结构化字段(type / source / company / title / date)。

设计:
- type / source 各有一份硬编码白名单,采用"最长匹配优先 + 不区分大小写"的子串扫描
  (这样"Alpine周度汇报"会优先于"Alpine"被匹配)
- company 没有白名单,用启发式判定 + 优先级评分:
    * ≥2 个汉字  → 100 分(中文名,最高)
    * ≥3 字母含至少 1 大写 → 50 分(Minimax / BABA / ByteDance)
    * ≥3 字母全小写 → 10 分(minimax / deepseek)
  多候选时取最高分,同分取最早出现
- date 可选:YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD 都识别,统一归一化为 YYYY-MM-DD
  如果 tag 里有日期就优先用它,否则用老文件名 date,再 fallback 到今天
- title 是剩余 token 用 _ 连接;sanitize 掉 Windows 文件名非法字符
- 调用方应该用 has_type_keyword() 先判一下"这段文字看起来像不像 tag",
  不像就直接走"文本消息"分支,避免误把闲聊当 tag

要改白名单只改本文件的两个 list,代码改动零。
"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path


# ============================================================
#  白名单(user-provided 2026-05-16)
# ============================================================
TYPE_WHITELIST: list[str] = [
    "专家访谈",
    "付费专家",
    "公司交流",
    "卖方汇报",
    "媒体新闻",
    "Alpine周度汇报",
    "同行交流",
    "新闻",
    "传闻",
    "Alpine",
]

SOURCE_WHITELIST: list[str] = [
    "Acecamp",
    "Thirdbridge",
    "AlphaEngine",
    "公众号",
    "Citi", "UBS", "GS", "MS", "JPM", "CICC", "CLSA",
    "Macquarie", "Barclays", "BofA", "HSBC", "Nomura",
    "Jefferies", "Deutsche", "Bernstein", "Daiwa",
    "海豚研究",
]


# Industry 白名单:当资料是对应"行业"而非"特定公司"时,industry 可替代 company。
# 起手 30 项,user 可自由增删;tag 解析采用最长匹配优先(新能源车 > 新能源)。
INDUSTRY_WHITELIST: list[str] = [
    # 互联网 / 科技
    "AI", "互联网", "电商", "本地生活", "短视频", "游戏",
    "招聘", "在线教育", "出行", "SaaS", "半导体", "云计算",
    # 消费
    "消费", "食品饮料", "美妆", "餐饮", "家电",
    # 新能源 / 制造
    "新能源车", "新能源", "光伏", "储能", "锂电",
    # 医药
    "创新药", "医药", "医疗器械", "医美", "CXO",
    # 金融
    "金融", "银行", "券商", "保险",
    # 物流 / 地产
    "快递", "物流", "航运", "地产", "物业",
    # 其他
    "教育", "旅游", "传媒",
]


# ============================================================
#  内部 helper
# ============================================================
def _sanitize_part(s: str) -> str:
    """文件名片段安全化:去 Windows 非法字符 + 所有空白折叠为单 _"""
    s = re.sub(r'[\\/*?:"<>|]', "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s


# 期间 token(Q1 / 26Q1 / FY25 / 1H25 等),不应被当 company 也不应当 date
_PERIOD_RE = re.compile(
    r"^(?:"
    r"\d{1,2}Q\d{1,4}"     # 1Q26 / 12Q26
    r"|Q\d{1,2}"           # Q1 / Q12(罕见)
    r"|\d{1,2}H\d{1,4}"    # 1H25
    r"|H\d{1,2}"           # H1
    r"|FY\d{2,4}"          # FY25 / FY2025
    r")$",
    re.IGNORECASE,
)


def _is_period(token: str) -> bool:
    return bool(_PERIOD_RE.match(token))


# 日期 token:YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD(纯 8 位数字)
_DATE_DASH_RE  = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATE_PLAIN_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _extract_date(token: str) -> str | None:
    """token 是日期就返回 YYYY-MM-DD canonical,否则 None"""
    m = _DATE_DASH_RE.match(token)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    m = _DATE_PLAIN_RE.match(token)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{mo}-{d}"
    return None


# 任意位置的日期 pattern,用来从 auto_title 里挖原文件内嵌日期
_DATE_SEARCH_DASH_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")
_DATE_SEARCH_PLAIN_RE = re.compile(r"\b(\d{4})(\d{2})(\d{2})\b")


def _find_first_date(text: str) -> str | None:
    """
    在 text 任意位置找第一个看起来像日期的 pattern。返回 YYYY-MM-DD canonical。
    用于从 auto_title(如原文件名"2026-05-07_Acecamp_xxx")里抠出原文件日期。
    """
    if not text:
        return None
    m = _DATE_SEARCH_DASH_RE.search(text)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            pass
    m = _DATE_SEARCH_PLAIN_RE.search(text)
    if m:
        y, mo, d = m.groups()
        # sanity: 合理的年月日范围
        try:
            iy, imo, id_ = int(y), int(mo), int(d)
            if 2000 <= iy <= 2100 and 1 <= imo <= 12 and 1 <= id_ <= 31:
                return f"{y}-{mo}-{d}"
        except ValueError:
            pass
    return None


def extract_leading_date(text: str) -> tuple[str | None, str]:
    """
    如果 text 以日期 pattern 开头(YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD,
    后跟可选分隔符 _ - / . 空格),返回 (canonical date YYYY-MM-DD, 剥掉日期+分隔符后的 text)。
    没匹配返回 (None, text)。

    供 handler 在初次保存时用:文件 Content-Disposition 名称如 "2026-05-07_Acecamp_xxx.docx",
    我们把日期作为 prefix,title 用剩余部分,避免出现 "2026-05-16_HHMMSS_2026-05-07_xxx" 这种
    双日期文件名。
    """
    if not text:
        return None, text
    # YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[_\-/. ]*", text)
    if m:
        y, mo, d = m.groups()
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}", text[m.end():]
        except ValueError:
            pass
    # YYYYMMDD(8 位连续数字,要带分隔符或行尾)
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?=[_\-/. ]|$)[_\-/. ]*", text)
    if m:
        y, mo, d = m.groups()
        try:
            iy, imo, id_ = int(y), int(mo), int(d)
            if 2000 <= iy <= 2100 and 1 <= imo <= 12 and 1 <= id_ <= 31:
                return f"{y}-{mo}-{d}", text[m.end():]
        except ValueError:
            pass
    return None, text


def _strip_leading_date(text: str, date_str: str) -> str:
    """
    如果 text 开头是给定的 date(各种格式),strip 掉它和后面的分隔符。
    用于避免 rename 后文件名出现 "2026-05-07_..._2026-05-07_xxx" 这种重复。
    """
    if not text or not date_str:
        return text
    # date_str 是 YYYY-MM-DD 格式;但 text 里可能是 YYYY-MM-DD / YYYY/MM/DD / YYYYMMDD
    y, mo, d = date_str.split("-")
    patterns = [
        rf"^{y}[-/.]{int(mo):d}[-/.]{int(d):d}",          # 2026-5-7
        rf"^{y}[-/.]{mo}[-/.]{d}",                         # 2026-05-07
        rf"^{y}{mo}{d}",                                   # 20260507
    ]
    for p in patterns:
        new = re.sub(p + r"[_\-/. ]*", "", text, count=1)
        if new != text:
            return new
    return text


# 主题词黑名单:CJK 描述性词不应当 company candidate
_TOPIC_CJK_PREFIXES = ("行业", "市场", "板块", "概念", "主题", "赛道")
_TOPIC_CJK_SUFFIXES = (
    "更新", "增速", "复盘", "总结", "回顾", "趋势", "展望",
    "盘点", "洞察", "梳理", "分析", "点评", "解读", "放缓",
    "研究", "策略", "投资", "纪要",
)
# 英文常见投研主题词,单独出现时不是 company
_TOPIC_EN_WORDS = frozenset({
    "callback", "update", "recap", "preview", "earnings", "outlook",
    "research", "report", "note", "memo", "review", "summary",
})


def _looks_topical(token: str) -> bool:
    """token 像不像主题描述词(不是 company name)?"""
    if not token:
        return False
    if token.lower() in _TOPIC_EN_WORDS:
        return True
    if any(token.startswith(p) for p in _TOPIC_CJK_PREFIXES):
        return True
    if any(token.endswith(s) for s in _TOPIC_CJK_SUFFIXES):
        return True
    return False


def _company_priority(token: str, has_industry: bool = False) -> int:
    """
    返回这个 token 作为 company 的优先级:
      0   = 不像 company(主题描述词 / 不够长)
      10  = 全小写字母 ≥3(minimax / deepseek)
      50  = CJK ≥2 但有 industry 时降级,或字母含大写
      100 = CJK ≥2(中文公司名,默认高优)
      200 = 字母含大写 + 有 industry(强力提升:industry 占语义槽,英文名更可能是 company)
    """
    if not token:
        return 0
    if _looks_topical(token):
        return 0

    cjk_count = sum(1 for c in token if "一" <= c <= "鿿")
    is_alpha_3plus = len(token) >= 3 and token.isalpha()
    has_upper = is_alpha_3plus and any(c.isupper() for c in token)

    if has_industry:
        # 有 industry 时,英文名(BABA / OpenAI)更可能是 company,boost 到最高
        # CJK 描述更可能是 title context,降到 50
        if has_upper:
            return 200
        if cjk_count >= 2:
            return 50
        if is_alpha_3plus:  # 全小写
            return 10
        return 0

    # 无 industry,中文名仍是首选(投研常态)
    if cjk_count >= 2:
        return 100
    if has_upper:
        return 50
    if is_alpha_3plus:
        return 10
    return 0


def _find_and_remove(text: str, whitelist: list[str]) -> tuple[str | None, str]:
    """
    用最长匹配优先 + case-insensitive 在 text 里找白名单的某项。
    返回 (canonical | None, 剥掉匹配后剩余文本)。
    """
    text_lower = text.lower()
    for canonical in sorted(whitelist, key=len, reverse=True):
        lc = canonical.lower()
        idx = text_lower.find(lc)
        if idx >= 0:
            remaining = text[:idx] + " " + text[idx + len(canonical):]
            return canonical, remaining
    return None, text


# ============================================================
#  对外 API
# ============================================================
def parse_tag(text: str) -> dict:
    """
    解析 tag 文字,返回:
    {
      "type":     str | None,  # 白名单 type canonical
      "source":   str | None,  # 白名单 source canonical
      "industry": str | None,  # 白名单 industry canonical(行业,当无 company 时替代)
      "company":  str | None,  # 最佳 company 候选(优先级评分)
      "title":    str,         # 剩余 token 用 _ 连接
      "date":     str | None,  # 第一个识别到的日期(YYYY-MM-DD)
      "raw":      str,         # 原始文字
    }
    """
    result = {"type": None, "source": None, "industry": None, "company": None,
              "title": "", "date": None, "raw": text}
    if not text:
        return result

    # 1. 抠 type
    type_val, working = _find_and_remove(text, TYPE_WHITELIST)
    result["type"] = type_val

    # 2. 抠 source
    source_val, working = _find_and_remove(working, SOURCE_WHITELIST)
    result["source"] = source_val

    # 3. 抠 industry(在 company 之前,避免行业 token 被当 company 误判)
    industry_val, working = _find_and_remove(working, INDUSTRY_WHITELIST)
    result["industry"] = industry_val

    # 4. tokenize 剩余
    tokens = working.split()

    # 5. 抠 date(第一个匹配 token)
    date_val = None
    non_date_tokens = []
    for tok in tokens:
        if date_val is None and not _is_period(tok):
            d = _extract_date(tok)
            if d:
                date_val = d
                continue
        non_date_tokens.append(tok)
    result["date"] = date_val

    # 6. 在剩下的 token 里找 company:跳过 period,按优先级评分
    # has_industry 标志影响评分:industry 已占语义槽位,英文名更可能是 company,CJK 描述词降级
    has_industry = bool(result["industry"])
    company_candidates = []  # (priority, idx, token)
    for i, tok in enumerate(non_date_tokens):
        if _is_period(tok):
            continue
        p = _company_priority(tok, has_industry=has_industry)
        if p > 0:
            company_candidates.append((p, i, tok))
    # 优先级降序,index 升序(同分取最早)
    company_candidates.sort(key=lambda x: (-x[0], x[1]))
    company_tok = company_candidates[0][2] if company_candidates else None
    result["company"] = _sanitize_part(company_tok) if company_tok else None

    # 7. 剩下的 token(除 date 和 company)拼 title
    title_tokens = [tok for tok in non_date_tokens if tok != company_tok]
    result["title"] = "_".join(_sanitize_part(t) for t in title_tokens if _sanitize_part(t))
    return result


def has_type_keyword(text: str) -> bool:
    """这段文字是否含 type 白名单关键词?用来区分'tag 意图' vs '闲聊'"""
    if not text:
        return False
    text_lower = text.lower()
    return any(t.lower() in text_lower for t in TYPE_WHITELIST)


def is_valid_tag(parsed: dict) -> bool:
    """
    合法 tag 需要:type + (company OR industry)。
    industry 是行业白名单的兜底,适用于"对应行业而非特定公司"的资料。
    """
    if not parsed.get("type"):
        return False
    return bool(parsed.get("company") or parsed.get("industry"))


# ============================================================
#  老文件名解析(用于重命名时保留 auto_title 和 date)
# ============================================================
# 严格:bot 自动命名 {YYYYMMDD|YYYY-MM-DD}_{HHMMSS}[_{auto_title}]
_OLD_FILENAME_STRICT_RE = re.compile(r"^(\d{4}-?\d{2}-?\d{2})_(\d{6})(?:_(.+))?$")
# 宽容:已被 rename 过的 {YYYYMMDD|YYYY-MM-DD}_{anything}
_OLD_FILENAME_LENIENT_RE = re.compile(r"^(\d{4}-?\d{2}-?\d{2})_(.+)$")


def _normalize_date_prefix(date_part: str) -> str:
    """把 20260516 / 2026-05-16 / 2026/05/16 都归一为 2026-05-16"""
    digits = re.sub(r"[-/.]", "", date_part)
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return date_part  # 不规范的就原样返回


def parse_old_filename(stem: str) -> dict:
    """
    解析已落盘文件的 stem(无扩展名)。返回 {date, time, auto_title, original_date}。
    支持 YYYYMMDD 和 YYYY-MM-DD 两种日期前缀格式(向后兼容)。

    - date:        文件名首段日期(bot 保存当天)
    - time:        如果严格匹配 {date}_{time}_... 则有,否则 None
    - auto_title:  剩余部分(可能含原文件名、auto_title 等)
    - original_date: 从 auto_title 内挖出来的第一个日期(原文件内嵌日期),用于
                     date 优先级链:user 给的 > 原文件 > 保存日 > 今天
    """
    m = _OLD_FILENAME_STRICT_RE.match(stem)
    if m:
        auto = m.group(3)
        return {
            "date": _normalize_date_prefix(m.group(1)),
            "time": m.group(2),
            "auto_title": auto,
            "original_date": _find_first_date(auto) if auto else None,
        }
    m = _OLD_FILENAME_LENIENT_RE.match(stem)
    if m:
        auto = m.group(2)
        return {
            "date": _normalize_date_prefix(m.group(1)),
            "time": None,
            "auto_title": auto,
            "original_date": _find_first_date(auto) if auto else None,
        }
    return {"date": None, "time": None, "auto_title": None, "original_date": None}


# ============================================================
#  反向解析已结构化(已 rename 过的)文件名
# ============================================================
_STRUCTURED_DATE_RE = re.compile(r"^\d{4}-?\d{2}-?\d{2}$")


def parse_structured_filename(stem: str) -> dict | None:
    """
    反向解析 tag-rename 过的文件名 stem,期望格式:
        {date}_{type}_{company}[_{source}]_{title}

    解析规则:
      - 按 _ 切 tokens
      - tokens[0] 必须是 date(YYYYMMDD / YYYY-MM-DD)
      - tokens[1] 必须严格 in TYPE_WHITELIST(canonical)
      - tokens[2] 当 company
      - tokens[3] 若 in SOURCE_WHITELIST → source,后续都是 title
      - tokens[3] 不在 SOURCE_WHITELIST → 没 source,3 起全是 title

    返回 dict 或 None(不是结构化格式)。
    None 的常见情况:文件还是 {date}_{time}_{auto_title}.ext 初次保存格式
    """
    parts = stem.split("_")
    if len(parts) < 4:  # 至少要有 date + type + company + title
        return None
    if not _STRUCTURED_DATE_RE.match(parts[0]):
        return None
    date_str = _normalize_date_prefix(parts[0])
    # type 必须严格命中白名单(白名单里的 type 都不含下划线,所以单 token 比对就够)
    if parts[1] not in TYPE_WHITELIST:
        return None
    type_val = parts[1]
    company = parts[2]
    rest = parts[3:]
    source = None
    if rest and rest[0] in SOURCE_WHITELIST:
        source = rest[0]
        rest = rest[1:]
    title = "_".join(rest)
    return {
        "date": date_str,
        "type": type_val,
        "company": company,
        "source": source,
        "title": title,
    }


# ============================================================
#  构造新文件名
# ============================================================
def today_date_str() -> str:
    """YYYY-MM-DD 格式的今天"""
    return _dt.datetime.now().strftime("%Y-%m-%d")


def now_time_str() -> str:
    """HHMMSS 格式的当前时间"""
    return _dt.datetime.now().strftime("%H%M%S")


def build_renamed_filename(
    old_path: Path,
    parsed: dict,
    source_hint: str | None = None,
) -> str:
    """
    用 parsed tag 字段 + 老文件名信息构造新文件名(含扩展名,不含目录)。
    格式:{date}_{type}_{subject}[_{source}]_{title}.{ext}

    subject 取舍:
      - 有 company → subject = company
      - 无 company 有 industry → subject = industry(替代 company 占槽位)
      - 二者都有 → subject = company,industry prepend 到 title(信息不丢)

    优先级链:
      date  : parsed.date > old_info.original_date(原文件内嵌)> old_info.date(保存日)> today
      source: parsed.source > source_hint
      title : user tag.title > auto_title(若开头是同 date 则 strip 掉)> old time > 当前时间
    """
    old_info = parse_old_filename(old_path.stem)
    date_str = (
        parsed.get("date")
        or old_info.get("original_date")
        or old_info["date"]
        or today_date_str()
    )

    source = parsed.get("source") or source_hint

    company = parsed.get("company")
    industry = parsed.get("industry")
    subject = company or industry  # 无 company 时 industry 顶上

    user_title = (parsed.get("title") or "").strip()
    if user_title:
        title = user_title
    else:
        auto = old_info["auto_title"] or ""
        # 若 auto_title 开头是同一个 date,strip 掉避免重复
        if auto:
            auto = _strip_leading_date(auto, date_str)
        title = auto or old_info["time"] or now_time_str()

    # 若 company 和 industry 都给了,industry prepend 到 title 不丢失信息
    if company and industry:
        title = f"{industry}_{title}" if title else industry

    parts = [
        date_str,
        _sanitize_part(parsed["type"]),
        _sanitize_part(subject),
    ]
    if source:
        parts.append(_sanitize_part(source))
    parts.append(_sanitize_part(title))

    return "_".join(parts) + old_path.suffix
