"""
图片消息 handler — 下载 + AES 解密 + 落盘到 ARCHIVE_DIR。

逻辑和 file handler 一致,只是 media 字段是 body['image'] 而不是 body['file']。
落盘后登记 pending tag,允许用户 5 分钟内发一条短文字给图片加 tag。
"""
from __future__ import annotations

import json

from lib.aes import AESDecryptError
from lib.downloader import DownloadError, download_and_save, humanize_size
from lib.pending_tag import get_window_seconds, register as register_pending
from lib.reply import dump_body, reply_markdown


async def handle(body: dict) -> None:
    # 1. 落盘样本 + 打印
    dump_body(body)
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 2. 下载 + 解密 + 落盘
    media = body.get("image") or {}
    try:
        path, size = await download_and_save(media, body)
    except DownloadError as e:
        print(f"[image handler] 下载失败: {e}")
        await reply_markdown(body, f"❌ 图片下载失败\n\n`{e}`")
        return
    except AESDecryptError as e:
        print(f"[image handler] 解密失败: {e}")
        await reply_markdown(body, f"❌ 图片解密失败\n\n`{e}`")
        return
    except Exception as e:
        print(f"[image handler] 未预期异常: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 图片处理失败\n\n`{type(e).__name__}: {e}`")
        return

    # 3. 登记 pending tag,然后回执
    userid = (body.get("from") or {}).get("userid", "")
    register_pending(userid, path)
    print(f"[image handler] 已保存 {path} ({size} bytes), pending tag for userid={userid}")

    window_min = get_window_seconds() // 60
    await reply_markdown(
        body,
        f"✅ 已识别为 **图片消息**\n\n"
        f"已保存: `{path.name}` ({humanize_size(size)})\n\n"
        f"💡 {window_min} 分钟内发一条短文字可重命名该图片",
    )
