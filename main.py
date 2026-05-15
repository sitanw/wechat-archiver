"""
微信投研归档助手 — 主入口

职责:
- 维持企微智能机器人长连接(连接 / 订阅 / 心跳 / 重连)
- 接收 aibot_msg_callback 帧后,按 msgtype 分流到 handlers/

连接骨架 fork 自已验证的 wecom_bot_hello.py(保留为 reference,不再维护)。
当前阶段 handler 只做样本采集 + 回执,不做归档逻辑。
"""
import asyncio
import json
import os
import uuid

import websockets
from dotenv import load_dotenv

from handlers import HANDLERS, handle_unknown

load_dotenv()
BOT_ID = os.getenv("WECOM_BOT_ID")
SECRET = os.getenv("WECOM_BOT_SECRET")
WS_URL = "wss://openws.work.weixin.qq.com"


def make_frame(cmd: str, body: dict, req_id: str = None) -> str:
    return json.dumps({
        "cmd": cmd,
        "headers": {"req_id": req_id or str(uuid.uuid4())},
        "body": body,
    }, ensure_ascii=False)


async def heartbeat(ws):
    """每 30 秒发心跳保活"""
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(make_frame("ping", {}))
            print("[heartbeat] ping")
        except Exception as e:
            print(f"[heartbeat] error: {e}")
            break


async def dispatch_message(body: dict) -> None:
    """
    aibot_msg_callback 业务分流入口。
    按 msgtype 查 HANDLERS,未注册的走 handle_unknown(也会落盘)。
    单个 handler 出异常不影响连接——异常被本函数吃掉,只打印。
    """
    msgtype = body.get("msgtype")
    sender = body.get("from", {}).get("userid")
    msgid = body.get("msgid")
    print(f"\n{'='*60}")
    print(f"[消息] from={sender} type={msgtype} msgid={msgid}")
    print(f"{'='*60}")

    handler = HANDLERS.get(msgtype, handle_unknown)
    try:
        await handler(body)
    except Exception as e:
        # handler 异常绝不上抛——保护连接 + 心跳的主循环
        print(f"[dispatch_message] handler 异常,已隔离: {type(e).__name__}: {e}")


async def handle_frame(ws, frame: dict) -> None:
    """
    传输层 cmd 分流:
    - aibot_msg_callback → 业务路由 dispatch_message
    - aibot_event_callback → 暂打印事件类型
    - pong / ack / 空 cmd 的 ack → 静默
    - 其他 → 打印未识别
    """
    cmd = frame.get("cmd")
    body = frame.get("body", {})

    if cmd == "aibot_msg_callback":
        await dispatch_message(body)

    elif cmd == "aibot_event_callback":
        event = body.get("event", {})
        print(f"\n[事件] {event.get('eventtype')}")

    elif cmd in ("pong", "ack"):
        pass  # 心跳 / 订阅 ack,安静处理

    else:
        # 心跳响应或其他空 cmd 的 ack 帧,静默处理
        if cmd is None and frame.get("errcode") == 0:
            return
        print(f"\n[未识别] cmd={cmd}: {json.dumps(frame, ensure_ascii=False)}")


async def run():
    if not BOT_ID or not SECRET:
        raise RuntimeError("请在 .env 中配置 WECOM_BOT_ID 和 WECOM_BOT_SECRET")

    print(f"[连接] {WS_URL}")
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        # 鉴权订阅
        await ws.send(make_frame("aibot_subscribe", {
            "bot_id": BOT_ID,
            "secret": SECRET,
        }))
        ack = await ws.recv()
        print(f"[订阅响应] {ack}")

        # 心跳并发任务
        hb = asyncio.create_task(heartbeat(ws))

        try:
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[原始] {raw[:200]}")
                    continue
                await handle_frame(ws, frame)
        finally:
            hb.cancel()


async def main():
    """带自动重连的外层 wrapper"""
    while True:
        try:
            await run()
        except websockets.ConnectionClosed as e:
            print(f"\n[连接断开] {e} - 5 秒后重连")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"\n[异常] {type(e).__name__}: {e} - 10 秒后重连")
            await asyncio.sleep(10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[退出]")
