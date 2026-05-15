"""链接消息 handler(公众号 / 腾讯文档 / 普通网页) — 当前阶段只做样本采集"""
import json

from lib.reply import dump_body, reply_markdown


async def handle(body: dict) -> None:
    dump_body(body)
    print(json.dumps(body, ensure_ascii=False, indent=2))
    await reply_markdown(body, "✅ 已识别为 **链接消息**")
