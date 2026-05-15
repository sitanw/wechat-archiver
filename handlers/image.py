"""
图片消息 handler — 下载 + AES 解密 + 落盘到 inbox/。

逻辑和 file handler 一致,只是 media 字段是 body['image'] 而不是 body['file']。
"""
from __future__ import annotations

import json

from lib.aes import AESDecryptError
from lib.downloader import DownloadError, download_and_save, humanize_size
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

    # 3. 成功回执
    print(f"[image handler] 已保存 {path} ({size} bytes)")
    await reply_markdown(
        body,
        f"✅ 已识别为 **图片消息**\n\n"
        f"已保存: `{path.name}` ({humanize_size(size)})",
    )
