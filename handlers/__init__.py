"""
msgtype → handler 分流表

每个 handler 签名统一为 `async def handle(body: dict) -> None`,
当前阶段只做样本采集(dump + print)和回执,不做实际归档逻辑。

新增 msgtype 只需:
1. 在 handlers/ 下新建一个 xxx.py,实现 handle()
2. 把 "xxx": xxx.handle 加进 HANDLERS dict
"""
import json

from lib.reply import dump_body, reply_markdown

from . import file as _file
from . import image as _image
from . import link as _link
from . import text as _text

HANDLERS = {
    "text": _text.handle,
    "image": _image.handle,
    "file": _file.handle,
    "link": _link.handle,
}


async def handle_unknown(body: dict) -> None:
    """
    未注册的 msgtype 兜底。
    这是我们发现新类型的入口——dump 出完整 body 后,对照字段补 handler。
    """
    msgtype = body.get("msgtype", "unknown")
    dump_body(body)
    print(f"[handle_unknown] 未支持的 msgtype: {msgtype}")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    await reply_markdown(body, f"⚠️ 已收到暂不支持的消息类型: `{msgtype}`")
