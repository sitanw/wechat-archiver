"""
文本消息 handler。

text msgtype 实际承载了三类内容,按优先级分流:
  1. **Tag 文字**(用户刚保存完文件,这条短文字给文件打 type/company/source/title)
     - 触发:无 URL + 文本含 type 白名单关键词 + 长度 [3, NOTE_MIN_LENGTH-1)
     - 动作:parse_tag → 用最老的 pending 重命名文件,然后 consume
  2. 链接分享(URL 出现在 content 里,公众号走抓取,其他类型回执 stub)
  3. 纯文本(无 URL 也非 tag):长则落盘成 DOCX 笔记,短则只回执
"""
from __future__ import annotations

import json
import re

from lib.downloader import humanize_size
from lib.pending_tag import (
    PendingArchive,
    consume,
    get_pending,
    get_window_seconds,
    queue_depth,
    register as register_pending,
)
from lib.reply import dump_body, reply_markdown
from lib.tag_parser import (
    TYPE_WHITELIST,
    build_renamed_filename,
    has_type_keyword,
    is_valid_tag,
    parse_tag,
)
from lib.text_note_saver import get_min_length, is_long_enough, save_as_docx
from lib.url_detect import classify_url, extract_title, find_url, SOURCE_LABEL
from lib.wechat_mp_fetcher import LinkFetchError, fetch_and_save_as_docx


# tag 文字的最低字符数,避免"ok"/"好的"被错误消费
_TAG_MIN_CHARS = 3


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

    # 2. 抠 content
    content = (body.get("text") or {}).get("content", "")
    url = find_url(content)
    userid = (body.get("from") or {}).get("userid", "")

    # 3. 无 URL 分支
    if not url:
        text = content.strip()
        # 3a. Tag 意图:包含 type 关键词 + 长度合理
        if has_type_keyword(text) and _TAG_MIN_CHARS <= len(text) < get_min_length():
            await _handle_tag_attempt(body, userid, text)
            return

        # 3b. 长文本笔记
        if is_long_enough(content):
            await _handle_long_text_note(body, content)
            return

        # 3c. 短文本(非 tag),仅回执
        await reply_markdown(body, "✅ 已识别为 **文本消息**")
        return

    # 4. 链接分支
    source_type = classify_url(url)
    label = SOURCE_LABEL.get(source_type, "未知来源")
    user_title_hint = extract_title(content, url)

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


# ────────────────────────────────────────────────────────────
#  Tag 配对
# ────────────────────────────────────────────────────────────
async def _handle_tag_attempt(body: dict, userid: str, tag_text: str) -> None:
    """
    用户发了一条含 type 关键词的短文本——意图是给最老的 pending 文件打 tag。
    parse → 校验 → rename → consume。
    """
    parsed = parse_tag(tag_text)

    # 1. 校验:type 和 company 都得有
    if not is_valid_tag(parsed):
        missing = []
        if not parsed.get("type"):
            missing.append(f"**type**(可选: {', '.join(TYPE_WHITELIST[:5])} 等共 {len(TYPE_WHITELIST)} 类)")
        if not parsed.get("company"):
            missing.append("**company**(需 ≥2 个汉字 或 ≥3 个全大写字母)")
        await reply_markdown(
            body,
            "⚠️ tag 不完整,未重命名文件\n\n"
            f"原文: `{tag_text}`\n\n"
            f"缺: {' / '.join(missing)}",
        )
        return

    # 2. 取最老的 pending
    pending = get_pending(userid)
    if pending is None:
        # 没 pending 但 tag 看起来很完整 → 提示 user 文件先发再 tag
        await reply_markdown(
            body,
            "📝 看起来这是一条 tag,但当前没有待重命名的文件。\n\n"
            "请先发送文件 / 链接 / 长文本,然后在 "
            f"{get_window_seconds() // 60} 分钟内发该 tag。",
        )
        return

    # 3. rename
    old_path = pending.path
    if not old_path.exists():
        # 文件被外部移走 / 删了 — 清队首,告诉用户
        consume(userid)
        await reply_markdown(body, f"⚠️ pending 文件已不存在,跳过: `{old_path.name}`")
        return

    new_name = build_renamed_filename(old_path, parsed, source_hint=pending.source_hint)
    new_path = old_path.with_name(new_name)

    # 冲突处理
    if new_path.exists():
        stem = new_path.stem
        for i in range(1, 100):
            cand = new_path.with_name(f"{stem}_{i}{new_path.suffix}")
            if not cand.exists():
                new_path = cand
                break

    try:
        old_path.rename(new_path)
    except OSError as e:
        await reply_markdown(body, f"❌ 重命名失败\n\n`{type(e).__name__}: {e}`")
        return

    # 4. consume pending
    consume(userid)
    remaining = queue_depth(userid)

    print(f"[tag] {old_path.name} → {new_path.name}")
    reply_lines = [
        "✅ 已用 tag 重命名",
        f"原名: `{old_path.name}`",
        f"新名: `{new_path.name}`",
    ]
    # 解析结果展示,让用户直观看到字段拆解
    parts = [f"type={parsed['type']}", f"company={parsed['company']}"]
    if parsed.get("source") or pending.source_hint:
        src = parsed.get("source") or pending.source_hint
        parts.append(f"source={src}")
    if parsed.get("title"):
        parts.append(f"title={parsed['title']}")
    reply_lines.append("解析: " + ", ".join(parts))

    if remaining > 0:
        reply_lines.append(f"⏳ 还有 {remaining} 个文件等待 tag(按发文件顺序消费)")
    await reply_markdown(body, "\n\n".join(reply_lines))


# ────────────────────────────────────────────────────────────
#  公众号文章抓取
# ────────────────────────────────────────────────────────────
async def _handle_wechat_mp(body: dict, url: str, user_title_hint: str | None, label: str) -> None:
    """
    公众号文章:启动 Playwright 抓取 → DOCX 落盘 → 一次性回执最终结果。

    response_url 一次性,不发"正在抓取..."提示。
    """
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

    # 落盘成功:登记 pending,source_hint = "公众号"
    userid = (body.get("from") or {}).get("userid", "")
    register_pending(userid, path, source_hint="公众号")
    print(f"[text handler / wechat_mp] 已保存 {path} ({size} bytes), pending tag for userid={userid}")

    window_min = get_window_seconds() // 60
    reply_lines = [f"✅ 已抓取并保存 **公众号文章**"]
    if fetched_title:
        reply_lines.append(f"标题: {fetched_title}")
    elif user_title_hint:
        reply_lines.append(f"标题: {user_title_hint}")
    reply_lines.append(f"文件: `{path.name}` ({humanize_size(size)})")
    reply_lines.append(f"💡 {window_min} 分钟内发 tag(含 type 关键词)可重命名")
    await reply_markdown(body, "\n\n".join(reply_lines))


# ────────────────────────────────────────────────────────────
#  长文本笔记落盘
# ────────────────────────────────────────────────────────────
async def _handle_long_text_note(body: dict, content: str) -> None:
    """长文本(笔记 / 调研纪要类) → 落盘 DOCX 到 ARCHIVE_DIR"""
    print(f"[text handler / note] 长文本 {len(content)} 字符,落盘 DOCX...")
    try:
        path, size, title = save_as_docx(content, body)
    except Exception as e:
        print(f"[text handler / note] 落盘失败: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 文本落盘失败\n\n`{type(e).__name__}: {e}`")
        return

    # 落盘成功:登记 pending(source_hint 无默认)
    userid = (body.get("from") or {}).get("userid", "")
    register_pending(userid, path)
    print(f"[text handler / note] 已保存 {path} ({size} bytes), pending tag for userid={userid}")

    window_min = get_window_seconds() // 60
    reply_lines = [f"✅ 已保存为 **文本笔记** ({len(content)} 字)"]
    if title:
        reply_lines.append(f"标题: {title}")
    reply_lines.append(f"文件: `{path.name}` ({humanize_size(size)})")
    reply_lines.append(f"💡 {window_min} 分钟内发 tag(含 type 关键词)可重命名")
    await reply_markdown(body, "\n\n".join(reply_lines))
