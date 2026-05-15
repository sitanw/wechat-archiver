"""
共享 helper:回执 + 样本落盘

设计契约:
- dump_body 是主路径,**绝不抛异常**——handler 的核心目标是"留下样本",落盘失败也只 warn
- reply_markdown 也是 best-effort——response_url 缺失 / httpx 失败都只 warn,不影响 dump
"""
import json
import os
import re
import uuid
from pathlib import Path

import httpx

# samples/ 相对项目根目录;reply.py 在 lib/ 下,父目录是项目根
SAMPLES_DIR = Path(__file__).resolve().parent.parent / "samples"


def _safe_name(s: str) -> str:
    """文件名 sanitize:非 [\\w-] 的字符全替换为 _"""
    return re.sub(r"[^\w-]", "_", s) if s else ""


def dump_body(body: dict) -> Path | None:
    """
    把消息 body 整段写到 samples/{msgtype}/{msgid}.json。
    msgtype 缺失兜底为 'unknown',msgid 缺失兜底为 uuid。
    返回写入的路径;失败返回 None(只 warn,不抛)。
    """
    try:
        msgtype = _safe_name(body.get("msgtype") or "unknown")
        msgid = _safe_name(body.get("msgid") or "") or f"nomsgid_{uuid.uuid4().hex[:8]}"

        target_dir = SAMPLES_DIR / msgtype
        target_dir.mkdir(parents=True, exist_ok=True)

        path = target_dir / f"{msgid}.json"
        # 同 msgid 重复时加 uuid 后缀,避免覆盖(理论上 msgid 唯一,这是保险)
        if path.exists():
            path = target_dir / f"{msgid}_{uuid.uuid4().hex[:6]}.json"

        path.write_text(
            json.dumps(body, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except Exception as e:
        print(f"[dump_body] 落盘失败,忽略: {type(e).__name__}: {e}")
        return None


async def reply_markdown(body: dict, content: str) -> None:
    """
    通过 body 里的 response_url 发 markdown 回执。
    没有 response_url / httpx 失败 / 对方 4xx 都只 warn,不抛异常——
    回复失败不应该污染主路径(dump + 后续 handler)。
    """
    response_url = body.get("response_url")
    if not response_url:
        print("[reply_markdown] body 里没有 response_url,跳过回复")
        return

    payload = {
        "msgtype": "markdown",
        "markdown": {"content": content},
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(response_url, json=payload)
            print(f"[reply_markdown] HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[reply_markdown] 回复失败,忽略: {type(e).__name__}: {e}")
