"""
Tag 文字 → 结构化字段(type / source / company / title)。

设计:
- type / source 各有一份硬编码白名单,采用"最长匹配优先 + 不区分大小写"的子串扫描
  (这样"Alpine周度汇报"会优先于"Alpine"被匹配)
- company 没有白名单,用启发式判定:≥2 个 CJK 汉字,或 ≥3 个全大写字母
- title 是剩余 token 用 _ 连接;sanitize 掉 Windows 文件名非法字符
- 调用方应该用 has_type_keyword() 先判一下"这段文字看起来像不像 tag",
  不像就直接走"文本消息"分支,避免误把闲聊当 tag

要改白名单只改本文件的两个 list,代码改动零。
"""
from __future__ import annotations

import re
from pathlib import Path


# ============================================================
#  白名单(user-provided 2026-05-16)
# ============================================================

# 9 类 invest-kb 已有 + 3 类新增,实际 user 给了 10 项
TYPE_WHITELIST: list[str] = [
    "专家访谈",
    "付费专家",
    "公司交流",
    "卖方汇报",
    "媒体新闻",
    "Alpine周度汇报",   # ⚠ 比 Alpine 更具体,需要 longest-match 优先
    "同行交流",
    "新闻",
    "传闻",
    "Alpine",
]

SOURCE_WHITELIST: list[str] = [
    # 专家网络 / 平台
    "Acecamp",
    "Thirdbridge",
    "AlphaEngine",
    "公众号",
    # 卖方研究所
    "Citi",
    "UBS",
    "GS",
    "MS",
    "JPM",
    "CICC",
    "CLSA",
    "Macquarie",
    "Barclays",
    "BofA",
    "HSBC",
    "Nomura",
    "Jefferies",
    "Deutsche",
    "Bernstein",
    "Daiwa",
]


# ============================================================
#  内部 helper
# ============================================================
def _sanitize_part(s: str) -> str:
    """文件名片段安全化:去 Windows 非法字符 + 所有空白折叠为单 _"""
    s = re.sub(r'[\\/*?:"<>|]', "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s


def _is_company_like(token: str) -> bool:
    """
    Heuristic:这个 token 看起来像不像公司名?
    - ≥2 个 CJK 汉字(覆盖中文公司名,如阿里 / 阿里巴巴 / 比亚迪)
    - 或 ≥3 个全大写字母(覆盖英文 ticker,如 BABA / TSLA / NVDA)
    """
    if not token:
        return False
    cjk_count = sum(1 for c in token if "一" <= c <= "鿿")
    if cjk_count >= 2:
        return True
    if len(token) >= 3 and token.isalpha() and token.isupper():
        return True
    return False


def _find_and_remove(text: str, whitelist: list[str]) -> tuple[str | None, str]:
    """
    在 text 里用最长匹配优先 + case-insensitive 找白名单中的第一项。
    返回 (canonical 名 | None, 剥掉匹配子串后的剩余文本)。
    """
    text_lower = text.lower()
    # 按长度降序排,longest-match 优先
    sorted_entries = sorted(whitelist, key=len, reverse=True)
    for canonical in sorted_entries:
        lc = canonical.lower()
        idx = text_lower.find(lc)
        if idx >= 0:
            # 用空格替换原位置,保持 token 边界,避免相邻字符粘连
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
        "type":    str | None,   # 匹配到的白名单 type(canonical 形式)
        "source":  str | None,   # 同上,可选
        "company": str | None,   # 第一个 company-like token
        "title":   str,          # 剩余 token 用 _ 连接,可能空字符串
        "raw":     str,          # 原始文字,用于错误回执
    }
    """
    result = {"type": None, "source": None, "company": None, "title": "", "raw": text}
    if not text:
        return result

    # 1. 抠 type
    type_val, working = _find_and_remove(text, TYPE_WHITELIST)
    result["type"] = type_val

    # 2. 抠 source
    source_val, working = _find_and_remove(working, SOURCE_WHITELIST)
    result["source"] = source_val

    # 3. tokenize 剩余,挑 company + 余下做 title
    tokens = working.split()
    company = None
    title_tokens = []
    for tok in tokens:
        if company is None and _is_company_like(tok):
            company = tok
        else:
            title_tokens.append(tok)

    result["company"] = _sanitize_part(company) if company else None
    result["title"] = "_".join(_sanitize_part(t) for t in title_tokens if _sanitize_part(t))
    return result


def has_type_keyword(text: str) -> bool:
    """
    粗判:这段文字是否含某个 type 白名单关键词?
    用来区分"这是 tag 意图" vs "这是闲聊"。
    不区分大小写,最长匹配不重要(只是 bool)。
    """
    if not text:
        return False
    text_lower = text.lower()
    return any(t.lower() in text_lower for t in TYPE_WHITELIST)


def is_valid_tag(parsed: dict) -> bool:
    """type 和 company 都识别到才算合法 tag,可以重命名"""
    return bool(parsed.get("type")) and bool(parsed.get("company"))


# ============================================================
#  老文件名解析(用于重命名时保留 auto_title)
# ============================================================
_OLD_FILENAME_RE = re.compile(r"^(\d{8})_(\d{6})(?:_(.+))?$")


def parse_old_filename(stem: str) -> dict:
    """
    解析已落盘文件的 stem(无扩展名)。当前命名规范:
      {YYYYMMDD}_{HHMMSS}                       例:20260516_143020
      {YYYYMMDD}_{HHMMSS}_{auto_title}          例:20260516_143020_阿里健康2HFY26_callback
    返回:{date, time, auto_title}。不匹配返回 {None, None, None}。
    """
    m = _OLD_FILENAME_RE.match(stem)
    if not m:
        return {"date": None, "time": None, "auto_title": None}
    return {"date": m.group(1), "time": m.group(2), "auto_title": m.group(3)}


# ============================================================
#  构造新文件名
# ============================================================
def build_renamed_filename(
    old_path: Path,
    parsed: dict,
    source_hint: str | None = None,
) -> str:
    """
    用 parsed tag 字段 + 老文件名信息构造新文件名(只 stem,不含目录)。

    Args:
        old_path: 原文件路径,从中抠 date / time / auto_title
        parsed:   parse_tag 的返回值,要求 type 和 company 都非空(调用方先用
                  is_valid_tag 检查)
        source_hint: handler 注册 pending 时给的默认 source(比如公众号 → "公众号")
                     仅在 parsed.source 为空时启用

    Returns:
        新文件名字符串,含扩展名。
        格式:{date}_{type}_{company}[_{source}]_{title}.{ext}
        title 优先级:user 给的 > 老文件的 auto_title > 老文件的 time > 当前时间
    """
    import datetime as _dt

    old_info = parse_old_filename(old_path.stem)
    date_str = old_info["date"] or _dt.datetime.now().strftime("%Y%m%d")

    source = parsed.get("source") or source_hint
    # title fallback chain
    user_title = parsed.get("title", "").strip()
    fallback_title = old_info["auto_title"] or old_info["time"] or _dt.datetime.now().strftime("%H%M%S")
    title = user_title or fallback_title

    parts = [
        date_str,
        _sanitize_part(parsed["type"]),
        _sanitize_part(parsed["company"]),
    ]
    if source:
        parts.append(_sanitize_part(source))
    parts.append(_sanitize_part(title))

    return "_".join(parts) + old_path.suffix
