"""
文本消息 handler。

text msgtype 实际承载了两类内容:
  1. 真正的纯文本(可能很长,如转发的会议纪要)
  2. 含 URL 的链接分享 —— 微信 / 企微把"复制链接 → 粘贴发送"统一打成 text msgtype,
     URL 直接出现在 text.content 里,有时前面带【有道云笔记】之类的标题前缀

所以这里要做一次子分流:
- 无 URL → 纯文本回执
- URL 是 wechat_mp → 调 lib/wechat_mp_fetcher 抓正文 + 图片做成 DOCX 落盘
- URL 是其他类型(有道云 / 腾讯文档 / 微信文档 / 普通网页)→ 仅回执链接信息,待人工或半自动补抓
"""
from __future__ import annotations

import json

from lib.downloader import humanize_size
from lib.reply import dump_body, reply_markdown
from lib.text_note_saver import is_long_enough, save_as_docx
from lib.url_detect import classify_url, extract_title, find_url, SOURCE_LABEL
from lib.wechat_mp_fetcher import LinkFetchError, fetch_and_save_as_docx


# 非公众号 URL 的"下一步"提示,纯展示用
_NEXT_STEP_HINT = {
    "youdao_note":   "(暂仅记录链接,需手动保存到 ARCHIVE_DIR)",
    "tencent_docs":  "(暂仅记录链接,需手动保存到 ARCHIVE_DIR)",
    "tencent_wedoc": "(暂仅记录链接,需手动保存到 ARCHIVE_DIR)",
    "web_other":     "(普通网页,暂仅记录链接)",
}


async def handle(body: dict) -> None:
    # 1. 落盘 + 打印,样本采集主路径(永远先做)
    dump_body(body)
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 2. 抠 content,看是不是含 URL 的链接分享
    content = (body.get("text") or {}).get("content", "")
    url = find_url(content)

    # 3a. 纯文本分支:够长就落盘 DOCX,否则只回执
    if not url:
        if is_long_enough(content):
            await _handle_long_text_note(body, content)
        else:
            await reply_markdown(body, "✅ 已识别为 **文本消息**")
        return

    # 3b. 链接分支
    source_type = classify_url(url)
    label = SOURCE_LABEL.get(source_type, "未知来源")
    user_title_hint = extract_title(content, url)

    # 公众号 URL:实际抓取
    if source_type == "wechat_mp":
        await _handle_wechat_mp(body, url, user_title_hint, label)
        return

    # 其他类型:仅回执,等人工补抓
    reply_lines = [f"✅ 已识别为 **链接 / {label}**"]
    if user_title_hint:
        reply_lines.append(f"标题: {user_title_hint}")
    reply_lines.append(f"URL: {url}")
    hint = _NEXT_STEP_HINT.get(source_type, "")
    if hint:
        reply_lines.append(hint)
    await reply_markdown(body, "\n\n".join(reply_lines))


async def _handle_wechat_mp(body: dict, url: str, user_title_hint: str | None, label: str) -> None:
    """
    公众号文章:启动 Playwright 抓取 → DOCX 落盘 → 一次性回执最终结果

    注意:不要在抓取开始时发"正在抓取..."提示。企微 response_url 是一次性的,
    POST 完一次就消耗,后续 ✅ 成功消息会静默失败,用户看不到最终结果。
    """
    # 终端打印让本地能看到抓取进度,但不发回执
    print(f"[text handler / wechat_mp] 开始抓取 {url} ...")

    try:
        path, size, fetched_title = await fetch_and_save_as_docx(url, body)
    except LinkFetchError as e:
        print(f"[text handler / wechat_mp] 抓取失败: {e}")
        await reply_markdown(body, f"❌ 公众号文章抓取失败\n\n`{e}`")
        return
    except Exception as e:
        print(f"[text handler / wechat_mp] 未预期异常: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 公众号文章抓取失败\n\n`{type(e).__name__}: {e}`")
        return

    print(f"[text handler / wechat_mp] 已保存 {path} ({size} bytes)")
    reply_lines = [f"✅ 已抓取并保存 **公众号文章**"]
    if fetched_title:
        reply_lines.append(f"标题: {fetched_title}")
    elif user_title_hint:
        reply_lines.append(f"标题: {user_title_hint}")
    reply_lines.append(f"文件: `{path.name}` ({humanize_size(size)})")
    await reply_markdown(body, "\n\n".join(reply_lines))


async def _handle_long_text_note(body: dict, content: str) -> None:
    """长文本(笔记 / 调研纪要类) → 落盘 DOCX 到 ARCHIVE_DIR"""
    print(f"[text handler / note] 长文本 {len(content)} 字符,落盘 DOCX...")
    try:
        path, size, title = save_as_docx(content, body)
    except Exception as e:
        print(f"[text handler / note] 落盘失败: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 文本落盘失败\n\n`{type(e).__name__}: {e}`")
        return

    print(f"[text handler / note] 已保存 {path} ({size} bytes)")
    reply_lines = [f"✅ 已保存为 **文本笔记** ({len(content)} 字)"]
    if title:
        reply_lines.append(f"标题: {title}")
    reply_lines.append(f"文件: `{path.name}` ({humanize_size(size)})")
    await reply_markdown(body, "\n\n".join(reply_lines))
