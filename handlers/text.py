"""
文本消息 handler。

text msgtype 实际承载了两类内容:
  1. 真正的纯文本(可能很长,如转发的会议纪要)
  2. 含 URL 的链接分享 —— 微信 / 企微把"复制链接 → 粘贴发送"统一打成 text msgtype,
     URL 直接出现在 text.content 里,有时前面带【有道云笔记】之类的标题前缀

所以这里要做一次子分流:有 URL 走链接逻辑,没 URL 才是纯文本。
当前阶段只做识别 + 回执,不下载正文(后续接 WeSpy 等)。
"""
from __future__ import annotations

import json

from lib.reply import dump_body, reply_markdown
from lib.url_detect import classify_url, extract_title, find_url, SOURCE_LABEL


# 各来源类型对应的下一步处理提示,纯展示用,不影响逻辑
_NEXT_STEP_HINT = {
    "wechat_mp":     "(后续将通过 WeSpy 抓取正文 -> Markdown + 元数据)",
    "youdao_note":   "(暂仅记录链接,待人工补抓)",
    "tencent_docs":  "(暂仅记录链接,待人工补抓)",
    "tencent_wedoc": "(暂仅记录链接,待人工补抓)",
    "web_other":     "(普通网页,暂仅记录链接)",
}


async def handle(body: dict) -> None:
    # 1. 落盘 + 打印,样本采集主路径(永远先做)
    dump_body(body)
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 2. 抠 content,看是不是含 URL 的链接分享
    content = (body.get("text") or {}).get("content", "")
    url = find_url(content)

    # 3a. 纯文本分支
    if not url:
        await reply_markdown(body, "✅ 已识别为 **文本消息**")
        return

    # 3b. 链接分支
    source_type = classify_url(url)
    label = SOURCE_LABEL.get(source_type, "未知来源")
    title = extract_title(content, url)
    hint = _NEXT_STEP_HINT.get(source_type, "")

    reply_lines = [f"✅ 已识别为 **链接 / {label}**"]
    if title:
        reply_lines.append(f"标题: {title}")
    reply_lines.append(f"URL: {url}")
    if hint:
        reply_lines.append(hint)

    await reply_markdown(body, "\n\n".join(reply_lines))
