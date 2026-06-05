"""
下载 + 解密 + 落盘的一站式流程。

时序约束:URL 5 分钟过期,handler 收到消息就要立即调本模块,不能拖。

文件命名约定:
- 默认 {YYYYMMDD}_{HHMMSS}.{ext}(原始文件名不可得时)
- 如果 COS 返回的 Content-Disposition 给了原始文件名,则用
  {YYYYMMDD}_{HHMMSS}_{原文件名 stem}.{ext},把原名作为 auto_title 拼上,
  这样 tag handler 重命名时若用户没打 title,会自动接上这个原名作 title。
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from urllib.parse import unquote, unquote_plus

import httpx

from lib.aes import AESDecryptError, decrypt
from lib.filetype import detect_extension


class DownloadError(Exception):
    """下载阶段失败(网络 / HTTP 状态 / 超时等)"""


_DEFAULT_INBOX = Path(__file__).resolve().parent.parent / "inbox"

# 33MB PDF 国内网络通常 10-30 秒,留 60 秒余量
DEFAULT_TIMEOUT = 60


def get_archive_dir() -> Path:
    """
    优先 .env 的 ARCHIVE_DIR,缺省 fallback 到项目根下的 inbox/。
    expandvars 展开 Windows 环境变量(如 %OneDrive%),目录不存在自动创建。
    """
    env_val = os.getenv("ARCHIVE_DIR", "").strip()
    expanded = os.path.expandvars(env_val) if env_val else ""
    path = Path(expanded) if expanded else _DEFAULT_INBOX
    path.mkdir(parents=True, exist_ok=True)
    return path


# ───────────────────────────────────────────────
#  Content-Disposition 解析
# ───────────────────────────────────────────────
def _extract_filename_from_disposition(cd: str) -> str | None:
    """
    解析 RFC 6266 Content-Disposition,返回原始文件名(含扩展名)。

    覆盖两种格式:
    - 普通:`attachment; filename="xxx.pdf"` 或 `attachment; filename=xxx.pdf`
    - RFC 5987(支持非 ASCII):`attachment; filename*=UTF-8''xxx%E4%B8%AD%E6%96%87.pdf`

    优先 RFC 5987 因为它能正确传 UTF-8(中文文件名场景)。
    """
    if not cd:
        return None

    # 1. RFC 5987:filename*=ENCODING''ENCODED-VALUE
    #    encoding 通常是 UTF-8,value 是 percent-encoded
    m = re.search(
        r"filename\*\s*=\s*([^']+)'[^']*'([^;]+)",
        cd,
        re.IGNORECASE,
    )
    if m:
        encoding = (m.group(1).strip() or "utf-8").lower()
        try:
            return unquote(m.group(2).strip(), encoding=encoding)
        except Exception:
            pass  # 解码失败就回退到普通格式

    # 2. 普通带引号:filename="xxx"
    # 企微实测会用 application/x-www-form-urlencoded 风格(空格 → '+',中文 → %XX),
    # unquote_plus 同时处理这两种编码
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE)
    if m:
        return _safe_unquote_plus(m.group(1))

    # 3. 普通无引号:filename=xxx
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE)
    if m:
        return _safe_unquote_plus(m.group(1).strip())

    return None


def _safe_unquote_plus(s: str) -> str:
    """unquote_plus(UTF-8),解码失败兜底返回原字符串"""
    try:
        return unquote_plus(s, encoding="utf-8")
    except Exception:
        return s


def _sanitize_stem(name: str) -> str:
    """
    把原始文件名 stem(不含扩展名)清洗成文件名安全形式。
    - 去 Windows 非法字符
    - 折叠空白为单 _
    - 截到合理长度(避免 OS 路径长度限制)
    """
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name[:120]


# ───────────────────────────────────────────────
#  文件名构造
# ───────────────────────────────────────────────
def _build_filename(ext: str, title: str = "") -> str:
    """
    {date}_{HHMMSS}[_{title_clean}].{ext}

    - 如果 title 以日期开头(原文件名带日期,如 "2026-05-07_Acecamp_xxx"),
      使用这个日期作为 prefix,title 用剩余部分(避免双日期)
    - 否则用今天的日期
    """
    # 延迟 import 避免循环
    from lib.tag_parser import extract_leading_date

    embedded_date, title = (None, title) if not title else extract_leading_date(title)
    date_str = embedded_date or _dt.datetime.now().strftime("%Y-%m-%d")
    time_str = _dt.datetime.now().strftime("%H%M%S")
    base = f"{date_str}_{time_str}"
    if title:
        return f"{base}_{title}.{ext}"
    return f"{base}.{ext}"


# ───────────────────────────────────────────────
#  主流程
# ───────────────────────────────────────────────
async def download_and_save(media: dict, body: dict) -> tuple[Path, int]:
    """
    URL 下载 → AES 解密 → magic-bytes 识别扩展名 → 落盘到 ARCHIVE_DIR。

    Returns:
        (落盘路径, 字节数)

    Raises:
        DownloadError: 下载阶段失败
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

    # 2. 解密
    plaintext = decrypt(ciphertext, aeskey)

    # 3. 识别扩展名
    ext = detect_extension(plaintext)

    # 4. 探测 Content-Disposition,看 COS 有没有给我们原始文件名
    cd = resp.headers.get("content-disposition", "")
    original_name = _extract_filename_from_disposition(cd)
    print(f"[downloader] Content-Disposition: {cd!r}")
    if original_name:
        print(f"[downloader] 抠出原始文件名: {original_name}")

    # 5. 构造目标文件名
    title_suffix = ""
    if original_name:
        # 去原文件扩展名,再 sanitize(我们用自己 magic-bytes 识别出来的扩展名,
        # 更可靠;原扩展名可能错,比如 .docx 实际是 PDF)
        original_stem = Path(original_name).stem
        title_suffix = _sanitize_stem(original_stem)

    archive_dir = get_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / _build_filename(ext, title_suffix)

    # 同名冲突(罕见)处理
    if path.exists():
        for i in range(1, 100):
            candidate = path.with_suffix(f".{i}{path.suffix}")
            if not candidate.exists():
                path = candidate
                break

    path.write_bytes(plaintext)
    return path, len(plaintext)


def humanize_size(n: int) -> str:
    """字节数 → 人类可读"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
