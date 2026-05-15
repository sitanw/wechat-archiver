"""
企微智能机器人长连接 — 最小验证脚本

用途: 验证 wss://openws.work.weixin.qq.com 长连接通路
      接收测试消息并 echo 回复
依赖: pip install -r requirements.txt
运行: python wecom_bot_hello.py
"""
import asyncio
import json
import os
import uuid
from dotenv import load_dotenv
import httpx
import websockets

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


async def reply_via_url(response_url: str, payload: dict):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(response_url, json=payload)
        print(f"[回复响应] HTTP {resp.status_code}: {resp.text[:200]}")


async def handle_frame(ws, frame: dict):
    cmd = frame.get("cmd")
    body = frame.get("body", {})

    if cmd == "aibot_msg_callback":
        msgtype = body.get("msgtype")
        sender = body.get("from", {}).get("userid")
        msgid = body.get("msgid")
        print(f"\n{'='*60}")
        print(f"[消息] from={sender} type={msgtype}")
        print(json.dumps(body, ensure_ascii=False, indent=2))
        print(f"{'='*60}")

        # 简单 echo - 验证回复链路通不通
        if msgtype == "text":
            content = body.get("text", {}).get("content", "")
            response_url = body.get("response_url")
            if response_url:
                await reply_via_url(response_url, {
                    "msgtype": "markdown",
                    "markdown": {"content": f"✅ 已收到: `{content}`"},
                })
            else:
                print("[警告] 消息中没有 response_url, 无法回复")

    elif cmd == "aibot_event_callback":
        event = body.get("event", {})
        print(f"\n[事件] {event.get('eventtype')}")

    elif cmd in ("pong", "ack"):
        pass  # 心跳/订阅 ack，安静处理

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
