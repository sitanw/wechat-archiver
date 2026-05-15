"""
下载 + 解密 + 落盘的一站式流程。

时序约束:URL 5 分钟过期,handler 收到消息就要立即调本模块,不能拖。
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

import httpx

from lib.aes import AESDecryptError, decrypt
from lib.filetype import detect_extension


class DownloadError(Exception):
    """下载阶段失败(网络 / HTTP 状态 / 超时等)"""


# 默认 fallback:项目根下的 inbox/(适合开发测试,不污染用户真实归档目录)
_DEFAULT_INBOX = Path(__file__).resolve().parent.parent / "inbox"

# 默认下载超时:33MB 的 PDF 在国内网络 10-30 秒,留 60 秒余量
DEFAULT_TIMEOUT = 60


def get_archive_dir() -> Path:
    """
    返回当前归档目录:优先 .env 里的 ARCHIVE_DIR,缺省回退到 inbox/。
    每次调用现读,方便 .env 改了重启 main.py 立刻生效。
    """
    env_val = os.getenv("ARCHIVE_DIR", "").strip()
    return Path(env_val) if env_val else _DEFAULT_INBOX


def _build_filename(ext: str) -> str:
    """
    生成归档目录里的文件名:{YYYYMMDD}_{HHMMSS}.{ext}

    日期 / 时间用收到消息那一刻的本地时间。
    其他元信息(类型 / 公司 / source / 标题)留给下游脚本或手动重命名。
    """
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S") + f".{ext}"


async def download_and_save(media: dict, body: dict) -> tuple[Path, int]:
    """
    主流程:URL 下载 → AES 解密 → 识别扩展名 → 落盘到 ARCHIVE_DIR(或 inbox/)

    Args:
        media: body['file'] 或 body['image'],必须含 'url' 和 'aeskey'
        body: 完整 body,目前未在文件名中使用,保留参数用于未来扩展(如日志关联)

    Returns:
        (落盘路径, 字节数)

    Raises:
        DownloadError: 下载失败
        AESDecryptError: 解密失败(向上透传)
    """
    url = media.get("url")
    aeskey = media.get("aeskey")
    if not url or not aeskey:
        raise DownloadError(f"media 字段不完整: url={bool(url)}, aeskey={bool(aeskey)}")

    # 1. 下载密文
    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
    except Exception as e:
        raise DownloadError(f"HTTP 请求失败: {type(e).__name__}: {e}") from e

    if resp.status_code != 200:
        raise DownloadError(
            f"HTTP {resp.status_code}: {resp.text[:200]} (URL 可能已过期)"
        )

    ciphertext = resp.content

    # 2. 解密(失败抛 AESDecryptError,handler 上层捕获)
    plaintext = decrypt(ciphertext, aeskey)

    # 3. 识别扩展名
    ext = detect_extension(plaintext)

    # 4. 落盘
    archive_dir = get_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / _build_filename(ext)
    # 极端边界:同一秒内来两个文件(基本不会发生),用 .1 / .2 后缀避免覆盖
    if path.exists():
        for i in range(1, 100):
            candidate = path.with_suffix(f".{i}{path.suffix}")
            if not candidate.exists():
                path = candidate
                break
    path.write_bytes(plaintext)
    return path, len(plaintext)


def humanize_size(n: int) -> str:
    """字节数 → 人类可读 (1.2 MB)"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
