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

from lib.downloader import get_archive_dir, humanize_size
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
    parse_structured_filename,
    parse_tag,
)
from lib.text_note_saver import get_min_length, is_long_enough, save_as_docx
from lib.url_detect import classify_url, extract_title, find_url, SOURCE_LABEL
from lib.wechat_mp_fetcher import LinkFetchError, fetch_and_save_as_docx


# 常规 tag(非引用)文字最低字符数,避免"ok"/"好的"被错误消费
_TAG_MIN_CHARS = 3

# 引用模式的最低字符数 — 比常规更宽,因为用户主动 引用 已经是强意图信号,
# 而且 2 字 CJK 公司名(腾讯/阿里/字节)合法
_QUOTE_TAG_MIN_CHARS = 2


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
        quote = body.get("quote")

        # 3a. 引用 + tag 意图:优先走"引用模式"
        # 条件比常规 tag 宽松:不要求 has_type_keyword(因为引用已经是强意图信号),
        # 最低 2 字以容纳 2 字 CJK 公司名(腾讯/阿里);最终是否处理由 _handle_quoted_tag
        # 内部根据"显式 tag 字段(type/source/date)"判定
        if (
            quote
            and _QUOTE_TAG_MIN_CHARS <= len(text) < get_min_length()
        ):
            handled = await _handle_quoted_tag(body, text, quote)
            if handled:
                return
            # 没抠到目标 / 没显式字段 → fall through 走常规 tag / 文本消息

        # 3b. 常规 Tag 配对:含 type 关键词 + 长度合理
        if has_type_keyword(text) and _TAG_MIN_CHARS <= len(text) < get_min_length():
            await _handle_tag_attempt(body, userid, text)
            return

        # 3c. 长文本笔记
        if is_long_enough(content):
            await _handle_long_text_note(body, content)
            return

        # 3d. 短文本(非 tag),仅回执
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
#  引用模式 tag(Phase 3)
# ────────────────────────────────────────────────────────────
# 文件扩展名,用于在引用消息内容里识别文件名 token
_FILE_EXT_PATTERN = r"pdf|docx|xlsx|pptx|doc|xls|ppt|jpg|jpeg|png|gif|webp|txt|bin|zip|rar|7z|mp3|mp4"
_BACKTICK_FILENAME_RE = re.compile(rf"`([^`]+\.(?:{_FILE_EXT_PATTERN}))`", re.IGNORECASE)
_PLAIN_FILENAME_RE = re.compile(rf"(\S+\.(?:{_FILE_EXT_PATTERN}))", re.IGNORECASE)


def _extract_filename_from_quote(quote: dict) -> str | None:
    """
    从被引用消息的内容里抠出文件名。
    quote 可能形如:
      {"msgtype": "text",     "text":     {"content": "..."}}
      {"msgtype": "markdown", "markdown": {"content": "..."}}
    优先取 backtick-wrapped 文件名(bot 回执里都是这格式),fallback 任意带扩展名的 token。
    取最后一个匹配——bot 的 rename 回执里"新名"在后,这是当前的最新状态。
    """
    if not isinstance(quote, dict):
        return None

    # 兼容 quote 字段不同形态,把所有 content 串拼一起再搜
    content_parts = []
    for sub_key in ("text", "markdown"):
        sub = quote.get(sub_key)
        if isinstance(sub, dict):
            c = sub.get("content")
            if c:
                content_parts.append(c)
    content = " ".join(content_parts)
    if not content:
        return None

    matches = _BACKTICK_FILENAME_RE.findall(content)
    if matches:
        return matches[-1]
    matches = _PLAIN_FILENAME_RE.findall(content)
    if matches:
        return matches[-1]
    return None


def _fuzzy_normalize(name: str) -> str:
    """
    强力归一化:strip 掉所有下划线 / 连字符 / 空白。
    WeCom 在 quote 里会把 markdown 下划线吃掉(`_text_` 误识别为 italic),
    所以精确匹配会失败。strip 后比对就能找到原文件。
    """
    return re.sub(r"[_\-\s]+", "", name)


def _find_file_fuzzy(archive_dir, target_name: str):
    """
    先精确匹配 target_name,匹配不到就 fuzzy 匹配
    (strip _ / - / 空白 后比对 stem)。返回 Path 或 None。
    """
    exact = archive_dir / target_name
    if exact.exists():
        return exact

    norm_target = _fuzzy_normalize(target_name)
    if not norm_target:
        return None
    for cand in archive_dir.iterdir():
        if not cand.is_file():
            continue
        if _fuzzy_normalize(cand.name) == norm_target:
            return cand
    return None


async def _handle_quoted_tag(body: dict, tag_text: str, quote: dict) -> bool:
    """
    引用模式:用户引用了之前 bot 的回执 + 发一条带 type 关键词的 tag。
    我们从 quote 里抠出文件名 → 直接 rename 那个文件(不走 pending 队列)。

    Returns:
        True  = 已处理(无论成功失败,都不要再走常规 pending 流程)
        False = 没识别出文件名 / 不该归我们管,fall through 给常规流程
    """
    target_filename = _extract_filename_from_quote(quote)
    if not target_filename:
        return False

    archive_dir = get_archive_dir()
    # 先精确再 fuzzy 匹配(WeCom quote 会吃 markdown 下划线)
    target_path = _find_file_fuzzy(archive_dir, target_filename)
    if target_path is None:
        await reply_markdown(
            body,
            f"⚠️ 引用的文件在归档目录里不存在(精确 + 模糊匹配都失败)\n\n"
            f"提取到: `{target_filename}`\n\n"
            f"目录: `{archive_dir}`",
        )
        return True

    parsed = parse_tag(tag_text)

    # 显式 tag 意图判定:必须命中以下任一才确认是 tag 操作
    # - 含 type / source 白名单关键词
    # - 含 date 正则 pattern
    # - 解析出 company,且引用的是已结构化文件(此时纯 company 修改有意义)
    explicit_signal = bool(parsed.get("type") or parsed.get("source") or parsed.get("date"))
    structured = parse_structured_filename(target_path.stem)
    if not explicit_signal and not (parsed.get("company") and structured):
        # 没显式信号,也不是"company-only + 结构化文件"的合理修正场景
        # → 不算 tag 意图,fall through 让外层走"文本消息"/pending
        return False

    # 引用 + 已结构化文件 → 允许部分 tag:缺的字段从老文件名继承
    if structured:
        # 用户至少要指定一个有效字段才走重命名(理论上前面 explicit_signal 检查已经过滤了,
        # 但 company-only 路径还要确认有 company)
        has_any_update = any(
            (parsed.get(k) or "").strip() if isinstance(parsed.get(k), str) else parsed.get(k)
            for k in ("type", "company", "source", "title", "date")
        )
        if not has_any_update:
            await reply_markdown(
                body,
                "⚠️ 引用模式:tag 解析后没有任何可用字段\n\n"
                f"原文: `{tag_text}`\n\n"
                f"想改某个字段就直接打它的值,例如 `腾讯`(改 company)、`2026-05-10`(改 date)、`公司交流`(改 type)。",
            )
            return True
        # Merge:parsed 字段优先,空则继承 structured(老的)
        merged_parsed = {
            "type":    parsed.get("type")    or structured["type"],
            "company": parsed.get("company") or structured["company"],
            "source":  parsed.get("source")  or structured["source"],
            "title":   parsed.get("title")   or structured["title"],
            "date":    parsed.get("date")    or structured["date"],
        }
        new_name = build_renamed_filename(target_path, merged_parsed, source_hint=None)
        is_partial = True
    else:
        # 引用 + 初次保存的非结构化文件 → 仍要求 type + company 完整
        if not is_valid_tag(parsed):
            missing = []
            if not parsed.get("type"):
                missing.append(f"**type**(可选: {', '.join(TYPE_WHITELIST[:5])} 等共 {len(TYPE_WHITELIST)} 类)")
            if not parsed.get("company"):
                missing.append("**company**(需 ≥2 个汉字 或 ≥3 个全大写字母)")
            await reply_markdown(
                body,
                "⚠️ tag 不完整,未重命名文件(引用模式)\n\n"
                f"目标: `{target_filename}`(还未 tag 过,需要完整 type + company)\n\n"
                f"tag 原文: `{tag_text}`\n\n"
                f"缺: {' / '.join(missing)}",
            )
            return True
        new_name = build_renamed_filename(target_path, parsed, source_hint=None)
        is_partial = False

    new_path = target_path.with_name(new_name)

    # 冲突处理
    if new_path.exists() and new_path != target_path:
        stem = new_path.stem
        for i in range(1, 100):
            cand = new_path.with_name(f"{stem}_{i}{new_path.suffix}")
            if not cand.exists():
                new_path = cand
                break

    if new_path == target_path:
        # 新旧名一样,no-op
        await reply_markdown(
            body,
            f"ℹ️ 引用模式:新名与原名一致,无需重命名\n\n文件: `{target_filename}`",
        )
        return True

    try:
        target_path.rename(new_path)
    except OSError as e:
        await reply_markdown(body, f"❌ 引用模式重命名失败\n\n`{type(e).__name__}: {e}`")
        return True

    print(f"[tag/quote] {target_path.name} → {new_path.name}")
    mode_label = "引用 + 部分更新" if is_partial else "引用模式"
    reply_lines = [
        f"✅ 已用 tag 重命名({mode_label})",
        f"原名: `{target_path.name}`",
        f"新名: `{new_path.name}`",
    ]
    # 解析显示:只展示 user 真正指定的字段(structured 继承的不重复列)
    parts = []
    for key in ("type", "company", "source", "title", "date"):
        v = parsed.get(key)
        if v:
            parts.append(f"{key}={v}")
    if parts:
        reply_lines.append("user 指定: " + ", ".join(parts))
    if is_partial:
        reply_lines.append("(未指定字段从老文件名继承)")
    await reply_markdown(body, "\n\n".join(reply_lines))
    return True


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
