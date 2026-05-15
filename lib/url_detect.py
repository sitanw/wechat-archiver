"""
URL 检测与来源分类。

设计原则:纯函数、无 IO、无副作用——可以被 handler 直接调,也能被未来的批量
sample 处理脚本调。
"""
from __future__ import annotations

import re
from typing import Optional

# 抓 http(s):// 开头到下一个空白字符前的所有内容
# 故意宽松:URL 末尾可能有逗号 / 句号被误抓,但目前阶段优先 recall over precision
URL_RE = re.compile(r"https?://[^\s]+")

# 域名后缀 → 来源 type 的查找表(按定义顺序优先级,前面的更具体)
# 匹配规则:host 完全相等,或以 ".<suffix>" 结尾
DOMAIN_MAP = [
    ("mp.weixin.qq.com",     "wechat_mp"),       # 微信公众号文章
    ("share.note.youdao.com", "youdao_note"),    # 有道云笔记(分享链接)
    ("note.youdao.com",      "youdao_note"),     # 有道云笔记(其他形式)
    ("docs.qq.com",          "tencent_docs"),    # 腾讯文档
    ("doc.weixin.qq.com",    "tencent_wedoc"),   # 微信文档(腾讯文档内嵌微信版)
]

# type → 中文标签,用于回执
SOURCE_LABEL = {
    "wechat_mp":     "公众号文章",
    "youdao_note":   "有道云笔记",
    "tencent_docs":  "腾讯文档",
    "tencent_wedoc": "微信文档",
    "web_other":     "网页链接",
}


def find_url(text: str) -> Optional[str]:
    """返回 text 中第一个 URL,没找到返回 None。"""
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0) if m else None


def classify_url(url: str) -> str:
    """
    按 host 域名分类,返回 SOURCE_LABEL 里的某个 key。
    匹配不到任何已知后缀返回 'web_other'。
    """
    if not url:
        return "web_other"
    m = re.match(r"https?://([^/?#]+)", url)
    if not m:
        return "web_other"
    host = m.group(1).lower()
    for suffix, type_ in DOMAIN_MAP:
        if host == suffix or host.endswith("." + suffix):
            return type_
    return "web_other"


def extract_title(text: str, url: str) -> Optional[str]:
    """
    尝试从文本中抠出 URL 的标题。
    常见模式(实测样本):
      【有道云笔记】腾讯26Q1业绩会记录
      https://share.note.youdao.com/s/Zvfp1qv6

    策略:
    1. 把 URL 从文本里抠掉
    2. 取剩余内容第一行非空字符串
    3. 去掉前面的【xxx】方括号前缀
    抠不出来返回 None。
    """
    if not text or not url:
        return None
    remainder = text.replace(url, "").strip()
    if not remainder:
        return None
    first_line = remainder.split("\n")[0].strip()
    if not first_line:
        return None
    cleaned = re.sub(r"^【[^】]+】\s*", "", first_line).strip()
    return cleaned or first_line
