"""
"刚保存的归档,等用户发 tag 重命名"的状态注册表(FIFO 队列版)。

机制:
- 任何 handler 把文件落盘后,调 register(userid, path, source_hint=...) 登记
- text handler 收到短文本时,先查 get_pending(userid),命中且在窗口内就当 tag 用
- tag 配对成功后调 consume(userid) 弹出**队列头**(最老的 pending)

FIFO 设计动机:
- 如果用户连续发文件 C → 文件 D 中间没打 tag,旧实现(覆盖式)会让 C 永久失去
  重命名机会。现在改成每个文件都进队列,user 发的 tag 按发文件的顺序消费。
  C 先发就先被 tag,D 后发就后被 tag。
- 缺点:user 必须"按发文件的顺序发对应 tag"。如果记不清顺序可能 tag 串味,
  Phase 3 引用机制可以解。

实现细节:
- 内存 dict[userid → deque[PendingArchive]],进程退出即丢(窗口本来就 5 分钟)
- 每用户最多 _MAX_QUEUE 条,防 memory 失控(实际不会触发)
- get_pending 时顺手清掉队首的过期项
- asyncio 单线程访问无需锁
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


# 默认配对窗口 5 分钟(可被 .env TAG_WINDOW_SECONDS 覆盖)
_DEFAULT_WINDOW = 300

# 单用户队列上限,防异常情况下内存溢出
_MAX_QUEUE = 20


def get_window_seconds() -> int:
    """读 .env 里的 TAG_WINDOW_SECONDS,非法或缺失则用默认值"""
    val = os.getenv("TAG_WINDOW_SECONDS", "").strip()
    if val.isdigit() and int(val) > 0:
        return int(val)
    return _DEFAULT_WINDOW


@dataclass
class PendingArchive:
    userid: str
    path: Path
    saved_at: float           # time.time()
    source_hint: str | None = None  # handler 可填默认 source(如公众号 → "公众号")


# userid → 该 userid 的 pending 队列
_PENDING: dict[str, deque[PendingArchive]] = {}


def register(userid: str, path: Path, source_hint: str | None = None) -> None:
    """
    Handler 把文件落盘后调用,把这条归档 append 到该 userid 的 FIFO 队列。

    source_hint:可选,handler 给一个"如果用户 tag 里没指定 source 就用这个"的提示。
    例如 wechat_mp_fetcher 会传 source_hint="公众号"。
    """
    if not userid:
        return  # 没 userid 兜底:不参与 tag 机制
    q = _PENDING.setdefault(userid, deque())
    q.append(PendingArchive(userid=userid, path=path, saved_at=time.time(),
                            source_hint=source_hint))
    # 防溢出:队列太长丢最老的
    while len(q) > _MAX_QUEUE:
        q.popleft()


def _purge_expired_head(q: deque[PendingArchive]) -> None:
    """从队首弹出过期 pending,内部 helper"""
    window = get_window_seconds()
    now = time.time()
    while q and now - q[0].saved_at > window:
        q.popleft()


def get_pending(userid: str) -> PendingArchive | None:
    """
    peek 队首(最老的)pending。
    顺手清队首过期项,清完空队列就返回 None。
    NOTE:不消费,只 peek。要真正用掉得调 consume()。
    """
    if not userid:
        return None
    q = _PENDING.get(userid)
    if not q:
        return None
    _purge_expired_head(q)
    if not q:
        # 全过期了,清掉空 deque
        del _PENDING[userid]
        return None
    return q[0]


def consume(userid: str) -> PendingArchive | None:
    """
    Tag 配对成功后,popleft 队首并返回。
    如果队列空 / 不存在,返回 None。
    """
    if not userid:
        return None
    q = _PENDING.get(userid)
    if not q:
        return None
    _purge_expired_head(q)
    if not q:
        del _PENDING[userid]
        return None
    entry = q.popleft()
    if not q:
        del _PENDING[userid]
    return entry


def queue_depth(userid: str) -> int:
    """返回该 userid 当前 pending 队列长度(过期项不算)。回执时可用来提示用户"""
    if not userid:
        return 0
    q = _PENDING.get(userid)
    if not q:
        return 0
    _purge_expired_head(q)
    return len(q)
