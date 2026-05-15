"""
基于 magic bytes 的文件类型识别。

WeCom 智能机器人 body 不带原始文件名 / 扩展名,需要解密后看头部字节自己判断。
这里只覆盖投研场景常见的:PDF / Office (docx/xlsx/pptx 以及老格式 doc/xls/ppt) /
常见图片 / 常见压缩包 / 视频 / 纯文本。
"""
from __future__ import annotations

# 简单 magic bytes 表:(头部签名, 偏移, 扩展名)
# 顺序有意义:更具体的放前面(比如 docx 这种 ZIP 容器要在 zip 之前判断)
_MAGIC_TABLE: list[tuple[bytes, int, str]] = [
    (b"%PDF",                       0, "pdf"),
    (b"\xFF\xD8\xFF",               0, "jpg"),
    (b"\x89PNG\r\n\x1a\n",          0, "png"),
    (b"GIF87a",                     0, "gif"),
    (b"GIF89a",                     0, "gif"),
    (b"RIFF",                       0, "webp"),   # WebP 是 RIFF 容器,简化按前缀判
    (b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1", 0, "doc"),   # 老 Office (doc/xls/ppt) OLE
    (b"\x1F\x8B",                   0, "gz"),
    (b"7z\xBC\xAF\x27\x1C",         0, "7z"),
    (b"Rar!\x1A\x07",               0, "rar"),
    (b"\x00\x00\x00\x20ftypmp4",    0, "mp4"),    # 简化判定
    (b"\x00\x00\x00\x18ftyp",       0, "mp4"),
    (b"ID3",                        0, "mp3"),
    (b"\xFF\xFB",                   0, "mp3"),
]


def detect_extension(blob: bytes) -> str:
    """
    返回不带点的扩展名(例如 'pdf' / 'jpg' / 'docx'),识别不出返回 'bin'。
    """
    if not blob:
        return "bin"

    # 1. ZIP 容器特殊处理:Office 2007+ 都是 ZIP 包 (PK\x03\x04 开头),
    #    需要看包内有没有特征文件来区分 docx/xlsx/pptx,否则就当 zip
    if blob[:4] == b"PK\x03\x04":
        return _classify_zip_container(blob)

    # 2. 其他按 magic table 顺序匹配
    for sig, offset, ext in _MAGIC_TABLE:
        if blob[offset:offset + len(sig)] == sig:
            return ext

    # 3. ASCII 文本启发式:全部都是可打印 ASCII / 中文 utf-8 也算,认作 txt
    if _looks_like_text(blob):
        return "txt"

    return "bin"


def _classify_zip_container(blob: bytes) -> str:
    """
    PK\\x03\\x04 开头的 ZIP 容器,看里面有没有 Office 特征条目。
    用 zipfile 列条目即可,不需要全量解压。
    """
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        return "zip"

    # Office 2007+ 容器特征:
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        return "pptx"
    # epub
    if "META-INF/container.xml" in names and any(n.endswith(".opf") for n in names):
        return "epub"
    return "zip"


def _looks_like_text(blob: bytes, sample_size: int = 4096) -> bool:
    """启发式:抽样前 sample_size 字节,如果能 utf-8 解码且控制字符比例低,认作文本"""
    sample = blob[:sample_size]
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # 控制字符比例 (排除常见的 \t \n \r)
    control = sum(1 for c in decoded if ord(c) < 32 and c not in "\t\n\r")
    return control / max(len(decoded), 1) < 0.02
