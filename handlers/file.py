"""
文件消息 handler — 下载 + AES 解密 + 落盘到 ARCHIVE_DIR。

时序关键:URL 5 分钟过期,handler 收到后立刻下载,不能拖延。
落盘后登记 pending tag,允许用户 5 分钟内发一条短文字给文件加 tag。
"""
from __future__ import annotations

import json

from lib.aes import AESDecryptError
from lib.downloader import DownloadError, download_and_save, humanize_size
from lib.pending_tag import get_window_seconds, register as register_pending
from lib.reply import dump_body, reply_markdown


async def handle(body: dict) -> None:
    # 1. 落盘样本 + 打印(永远先做,确保 body 字段被记录)
    dump_body(body)
    print(json.dumps(body, ensure_ascii=False, indent=2))

    # 2. 下载 + 解密 + 落盘
    media = body.get("file") or {}
    try:
        path, size = await download_and_save(media, body)
    except DownloadError as e:
        print(f"[file handler] 下载失败: {e}")
        await reply_markdown(body, f"❌ 文件下载失败\n\n`{e}`")
        return
    except AESDecryptError as e:
        print(f"[file handler] 解密失败: {e}")
        await reply_markdown(body, f"❌ 文件解密失败\n\n`{e}`")
        return
    except Exception as e:
        print(f"[file handler] 未预期异常: {type(e).__name__}: {e}")
        await reply_markdown(body, f"❌ 文件处理失败\n\n`{type(e).__name__}: {e}`")
        return

    # 3. 登记 pending tag,然后回执
    userid = (body.get("from") or {}).get("userid", "")
    register_pending(userid, path)
    print(f"[file handler] 已保存 {path} ({size} bytes), pending tag for userid={userid}")

    window_min = get_window_seconds() // 60
    await reply_markdown(
        body,
        f"✅ 已识别为 **文件消息**\n\n"
        f"已保存: `{path.name}` ({humanize_size(size)})\n\n"
        f"💡 {window_min} 分钟内发一条短文字(例:`阿里 callback 26Q1`)可重命名该文件",
    )
